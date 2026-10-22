import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from backups import create_backup, restore
from bot import Bot
from reliability import JournalAPI, TaskStore, TelegramError, Worker


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "tasks.sqlite3"
        self.store = TaskStore(self.path)
        self.addCleanup(self.store.close)

    def test_second_instance_is_rejected_without_resetting_active_task(self):
        self.store.ingest([{"update_id": 1}])
        task = self.store.claim(0)
        with self.assertRaisesRegex(RuntimeError, "autre instance"):
            TaskStore(self.path)
        self.assertIsNone(self.store.claim(0))
        self.assertEqual(self.store.db.execute("SELECT status FROM tasks WHERE id=?", (task["id"],)).fetchone()[0], "running")

    def test_lock_is_released_on_close(self):
        self.store.close()
        self.store = TaskStore(self.path)
        self.assertEqual(self.store.offset(), 0)
        # Le nettoyage initial référence l'ancienne instance déjà fermée.
        self.addCleanup(self.store.close)

    def test_optional_callback_failure_does_not_suppress_rules(self):
        self.store.ingest([{"update_id": 1, "callback_query": {"id": "click", "data": "rules",
                           "message": {"chat": {"id": 7, "type": "private"}}}}])
        raw = Mock()
        raw.call.side_effect = [TelegramError(0), {}]
        journal = JournalAPI(raw, self.store)
        bot = Bot(journal, "TestBot", "-42", "url", "Règles")
        Worker(self.store, journal, lambda _, event: bot.handle(event), 0, threading.Event()).process_one()
        self.assertEqual([c.args[0] for c in raw.call.call_args_list], ["answerCallbackQuery", "sendMessage"])
        self.assertEqual(self.store.db.execute("SELECT status FROM tasks").fetchone()[0], "done")

    def test_changed_welcome_during_collective_retry_does_not_duplicate(self):
        self.store.ingest([{"update_id": 1, "message": {"chat": {"id": -42, "type": "supergroup"},
                           "new_chat_members": [{"id": 7}, {"id": 8}]}}])
        raw = Mock()
        raw.call.side_effect = [{}, TelegramError(429, 2), {}]
        journal = JournalAPI(raw, self.store)
        bot = Bot(journal, "TestBot", "-42", "url", "Règles", welcome="Ancien accueil")
        worker = Worker(self.store, journal, lambda _, event: bot.handle(event), 0, threading.Event())
        worker.process_one()
        self.store.close()
        self.store = TaskStore(self.path)
        self.addCleanup(self.store.close)
        journal = JournalAPI(raw, self.store)
        bot = Bot(journal, "TestBot", "-42", "url", "Règles", welcome="Nouvel accueil")
        worker = Worker(self.store, journal, lambda _, event: bot.handle(event), 0, threading.Event())
        with self.store.db:
            self.store.db.execute("UPDATE tasks SET ready=0")
        worker.process_one()
        self.assertEqual([c.kwargs["chat_id"] for c in raw.call.call_args_list], [7, 8, 8])
        self.assertTrue(all(c.kwargs["text"] == "Ancien accueil" for c in raw.call.call_args_list))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM actions WHERE request IS NOT NULL").fetchone()[0], 0)

    def test_legacy_completed_action_is_reused_by_stable_step(self):
        self.store.ingest([{"update_id": 1}])
        task = self.store.claim(0)
        raw = Mock()
        raw.call.return_value = {}
        journal = JournalAPI(raw, self.store)
        journal.local.task_id = task["id"]
        journal.call("sendMessage", chat_id=7, text="Bienvenue")
        journal.action("welcome:7", "sendMessage", chat_id=7, text="Bienvenue")
        self.assertEqual(raw.call.call_count, 1)

    def test_status_is_private_and_owner_only(self):
        raw = Mock()
        bot = Bot(raw, "TestBot", "-42", "url", "Règles", owner_user_id=99)
        bot.background = self.store
        for uid in (7, 99):
            bot.handle({"message": {"chat": {"id": uid, "type": "private"},
                                    "from": {"id": uid}, "text": "/statut"}})
        self.assertIn("réservée", raw.call.call_args_list[0].kwargs["text"])
        self.assertNotIn("En attente", raw.call.call_args_list[0].kwargs["text"])
        self.assertIn("En attente", raw.call.call_args_list[1].kwargs["text"])
        raw.reset_mock()
        bot.handle({"message": {"chat": {"id": -42, "type": "supergroup"},
                                "from": {"id": 99}, "text": "/statut"}})
        raw.call.assert_not_called()


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.tasks = self.root / "tasks.sqlite3"
        self.visitors = self.root / "visitors.sqlite3"
        for path in (self.tasks, self.visitors):
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE sample (value INTEGER)")
                db.execute("INSERT INTO sample VALUES (42)")

    def test_backup_and_restore_keep_data_and_do_not_overwrite(self):
        snapshot = create_backup(self.tasks, self.visitors, self.root / "backups")
        destination = restore(snapshot, self.root / "restored")
        with closing(sqlite3.connect(destination / "tasks.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT value FROM sample").fetchone()[0], 42)
        with self.assertRaises(FileExistsError):
            restore(snapshot, destination)
        metadata = json.loads((self.root / "backups/latest.json").read_text())
        self.assertEqual(metadata["snapshot"], snapshot.name)

    def test_live_wal_data_are_included(self):
        with closing(sqlite3.connect(self.tasks)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT INTO sample VALUES (99)")
            db.commit()
            snapshot = create_backup(self.tasks, self.visitors, self.root / "backups")
            with closing(sqlite3.connect(snapshot / "tasks.sqlite3")) as copy:
                self.assertEqual(copy.execute("SELECT count(*) FROM sample").fetchone()[0], 2)

    def test_tampered_backup_is_refused(self):
        snapshot = create_backup(self.tasks, self.visitors, self.root / "backups")
        with (snapshot / "tasks.sqlite3").open("ab") as file:
            file.write(b"tampered")
        with self.assertRaisesRegex(ValueError, "Empreinte"):
            restore(snapshot, self.root / "restored")

    def test_corrupt_second_database_leaves_no_partial_destination_and_can_retry(self):
        snapshot = create_backup(self.tasks, self.visitors, self.root / "backups")
        original = (snapshot / "visitors.sqlite3").read_bytes()
        (snapshot / "visitors.sqlite3").write_bytes(b"corrupted")
        destination = self.root / "restored"
        with self.assertRaises(ValueError):
            restore(snapshot, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob(".restore-*")), [])
        (snapshot / "visitors.sqlite3").write_bytes(original)
        restore(snapshot, destination)
        self.assertTrue((destination / "visitors.sqlite3").is_file())

    def test_copy_failure_leaves_no_partial_destination(self):
        from backups import copy_database
        snapshot = create_backup(self.tasks, self.visitors, self.root / "backups")
        def failing_copy(source, target):
            copy_database(source, target)
            if source.name == "visitors.sqlite3":
                raise OSError("disk full")
        with patch("backups.copy_database", side_effect=failing_copy):
            with self.assertRaises(OSError):
                restore(snapshot, self.root / "restored")
        self.assertFalse((self.root / "restored").exists())
        self.assertEqual(list(self.root.glob(".restore-*")), [])

    def test_retention_keeps_requested_number(self):
        for _ in range(3):
            create_backup(self.tasks, self.visitors, self.root / "backups", keep=2)
        self.assertEqual(len(list((self.root / "backups").glob("backup-*"))), 2)

    def test_restore_does_not_replay_pending_tasks_from_old_snapshot(self):
        store = TaskStore(self.tasks)
        try:
            store.ingest([{"update_id": 1}])
            snapshot = create_backup(self.tasks, self.visitors, self.root / "backups")
        finally:
            store.close()
        restored = restore(snapshot, self.root / "restored")
        with closing(sqlite3.connect(restored / "tasks.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT status,payload FROM tasks").fetchone(), ("uncertain", "{}"))
