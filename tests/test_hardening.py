from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from backups import create_backup, encrypt_snapshot
from bot import Bot
from offsite_pull import verify_encrypted
from reliability import JournalAPI, TaskStore, TelegramError, Worker


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = TaskStore(self.root / "tasks.sqlite3", command_limit=2, owner_user_id=99)
        self.addCleanup(self.store.close)

    def test_promotion_retry_uses_current_permissions(self):
        raw = Mock()
        raw.call.side_effect = [{"status": "member"}, TelegramError(429, 2), {"status": "member"}, True]
        journal = JournalAPI(raw, self.store)
        bot = Bot(journal, "TestBot", "-42", "url", "Règles", {7}, {7})
        self.store.submit(bot.promote_known, {"id": 7})
        worker = Worker(self.store, journal, lambda _, u: bot.promote_known(u), 1, threading.Event())
        worker.process_one()
        bot.moderator_ids = frozenset()
        with self.store.db:
            self.store.db.execute("UPDATE tasks SET ready=0")
        worker.process_one()
        rights = [call.kwargs for call in raw.call.call_args_list if call.args[0] == "promoteChatMember"]
        self.assertTrue(rights[0]["can_restrict_members"])
        self.assertFalse(rights[1]["can_restrict_members"])
        self.assertFalse(rights[1]["can_delete_messages"])

    def test_changed_destination_is_rejected_on_retry(self):
        task_id = 1
        self.store.prepare_request(task_id, "copy", "copyMessage", {"chat_id": -42})
        with self.assertRaises(PermissionError):
            self.store.prepare_request(task_id, "copy", "copyMessage", {"chat_id": -99})

    def test_private_text_limit_is_persistent_and_owner_and_media_are_exempt(self):
        def event(update_id, uid, **body):
            return {"update_id": update_id, "message": {"chat": {"id": uid, "type": "private"},
                                                        "from": {"id": uid}, **body}}
        self.store.ingest([event(i, 7, text="/start") for i in (1, 2, 3)])
        self.assertEqual(self.store.pending_count(), 2)
        self.assertEqual(self.store.offset(), 4)
        self.store.close()
        self.store = TaskStore(self.root / "tasks.sqlite3", command_limit=2, owner_user_id=99)
        self.addCleanup(self.store.close)
        self.store.ingest([event(4, 7, text="/id"), event(5, 7, audio={"file_name": "music.mp3"})])
        self.store.ingest([event(i, 99, text="/statut") for i in (6, 7, 8)])
        self.assertEqual(self.store.pending_count(), 6)

    def test_command_window_expires(self):
        def event(i):
            return {"update_id": i, "message": {"chat": {"id": 7, "type": "private"}, "text": "/start"}}
        with patch("reliability.time.time", return_value=100):
            self.store.ingest([event(1), event(2), event(3)])
        with patch("reliability.time.time", return_value=111):
            self.store.ingest([event(4)])
        self.assertEqual(self.store.pending_count(), 3)

    def test_stale_backup_is_flagged_in_status(self):
        backup_dir = self.root / "backups"
        backup_dir.mkdir()
        (backup_dir / "snapshot").mkdir()
        (backup_dir / "latest.json").write_text(json.dumps({"snapshot": "snapshot",
            "created_at": (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat()}))
        raw = Mock()
        bot = Bot(raw, "TestBot", "-42", "url", "Règles", owner_user_id=99, backup_dir=backup_dir)
        bot.background = self.store
        bot.show_status(99, 99)
        self.assertIn("plus de 36 heures", raw.call.call_args.kwargs["text"])


class EncryptionAndRotationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in ("tasks.sqlite3", "visitors.sqlite3"):
            with closing(sqlite3.connect(self.root / name)) as db, db:
                db.execute("CREATE TABLE example(value INTEGER)")
                db.execute("INSERT INTO example VALUES (42)")

    def test_same_second_rotation_keeps_latest(self):
        original = tempfile.mkdtemp
        suffixes = iter(("zzzzzzzz", "aaaaaaaa"))
        def make_temp(*args, **kwargs):
            if kwargs.get("prefix") == ".backup-":
                path = Path(kwargs["dir"]) / (".backup-" + next(suffixes))
                path.mkdir()
                return str(path)
            return original(*args, **kwargs)
        with patch("backups.tempfile.mkdtemp", side_effect=make_temp), patch("backups.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 22, tzinfo=timezone.utc)
            create_backup(self.root / "tasks.sqlite3", self.root / "visitors.sqlite3", self.root / "backups", keep=1)
            latest = create_backup(self.root / "tasks.sqlite3", self.root / "visitors.sqlite3", self.root / "backups", keep=1)
        self.assertTrue(latest.exists())
        marker = json.loads((self.root / "backups/latest.json").read_text())
        self.assertTrue((self.root / "backups" / marker["snapshot"]).exists())

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL requis")
    def test_encryption_decryption_restore_and_tamper_rejection(self):
        key, certificate = self.root / "private.pem", self.root / "public.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-noenc",
                        "-keyout", str(key), "-out", str(certificate), "-subj", "/CN=Test", "-days", "1"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        snapshot = create_backup(self.root / "tasks.sqlite3", self.root / "visitors.sqlite3", self.root / "backups")
        encrypted = self.root / "snapshot.enc"
        encrypt_snapshot(snapshot, certificate, encrypted)
        staging = self.root / "check"
        staging.mkdir()
        verify_encrypted(encrypted, key, staging)
        with closing(sqlite3.connect(staging / "restore-check/tasks.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT value FROM example").fetchone()[0], 42)
        damaged = bytearray(encrypted.read_bytes())
        damaged[-1] ^= 1
        encrypted.write_bytes(damaged)
        staging2 = self.root / "damaged"
        staging2.mkdir()
        with self.assertRaises(subprocess.CalledProcessError):
            verify_encrypted(encrypted, key, staging2)
