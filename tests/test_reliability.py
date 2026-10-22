import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from bot import Bot, wait_for_startup
from reliability import JournalAPI, TaskStore, TelegramError, UncertainAction, Worker


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "tasks.sqlite3"
        self.store = TaskStore(self.path)
        self.api = Mock()
        self.api.call.return_value = {"message_id": 90}
        self.journal = JournalAPI(self.api, self.store)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def reopen(self):
        self.store.close()
        self.store = TaskStore(self.path)
        self.journal = JournalAPI(self.api, self.store)

    def ready(self):
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE tasks SET ready=0")

    def worker(self, handler):
        return Worker(self.store, self.journal, handler, 0, threading.Event())

    def test_ingest_and_offset_survive_restart_without_duplicate_tasks(self):
        self.store.ingest([{"update_id": 123}])
        self.reopen()
        self.assertEqual(self.store.offset(), 124)
        self.store.ingest([{"update_id": 123}])
        self.assertEqual(self.store.pending_count(), 1)

    def test_secondary_queue_survives_restart_and_deduplicates(self):
        function = Mock(__name__="notify_new_visitor")
        self.store.submit(function, {"id": 7, "first_name": "Camille", "text": "private"})
        self.store.submit(function, {"id": 7})
        self.reopen()
        self.assertEqual(self.store.pending_count(), 1)
        task = self.store.claim(1)
        self.assertEqual(task["payload"]["first_name"], "Camille")
        self.assertNotIn("text", task["payload"])

    def test_queue_larger_than_previous_limit_loses_no_tasks(self):
        self.store.ingest([{"update_id": i} for i in range(300)])
        self.reopen()
        self.assertEqual(self.store.pending_count(), 300)

    def test_429_retries_only_rejected_action_after_delay(self):
        self.store.ingest([{"update_id": 1}])
        def handle(kind, payload):
            self.journal.call("copyMessage", chat_id=-42, from_chat_id=7, message_id=1)
            self.journal.call("sendMessage", chat_id=7, text="Publié")
        self.api.call.side_effect = [{"message_id": 90}, TelegramError(429, 30), {}]
        worker = self.worker(handle)
        self.assertTrue(worker.process_one())
        self.assertFalse(worker.process_one())  # Délai imposé respecté.
        self.ready()
        self.assertTrue(worker.process_one())
        self.assertEqual([call.args[0] for call in self.api.call.call_args_list],
                         ["copyMessage", "sendMessage", "sendMessage"])
        self.assertEqual(self.store.pending_count(), 0)

    def test_429_delay_survives_restart(self):
        self.store.ingest([{"update_id": 1}])
        self.api.call.side_effect = TelegramError(429, 30)
        self.worker(lambda *_: self.journal.call("copyMessage", chat_id=-42)).process_one()
        self.reopen()
        self.assertIsNone(self.store.claim(0))
        self.ready()
        self.assertIsNotNone(self.store.claim(0))

    def test_confirmed_copy_is_not_repeated_after_worker_crash(self):
        self.store.ingest([{"update_id": 1}])
        task = self.store.claim(0)
        self.journal.local.task_id = task["id"]
        self.journal.call("copyMessage", chat_id=-42, message_id=1)
        # Crash avant finish(), après confirmation de la copie.
        self.reopen()
        self.worker(lambda *_: self.journal.call("copyMessage", chat_id=-42, message_id=1)).process_one()
        self.assertEqual(self.api.call.call_count, 1)

    def test_crash_during_send_becomes_uncertain_and_is_not_replayed(self):
        self.store.ingest([{"update_id": 1}])
        task = self.store.claim(0)
        self.store.begin_action(task["id"], "fingerprint")
        self.reopen()
        with self.assertRaises(UncertainAction):
            self.store.begin_action(task["id"], "fingerprint")

    def test_actual_interrupted_send_is_not_replayed_after_reopen(self):
        self.store.ingest([{"update_id": 1}])
        self.api.call.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.worker(lambda *_: self.journal.call("copyMessage", chat_id=-42)).process_one()
        self.reopen()
        self.api.call.side_effect = None
        self.worker(lambda *_: self.journal.call("copyMessage", chat_id=-42)).process_one()
        self.assertEqual(self.api.call.call_count, 1)
        self.assertEqual(self.store.db.execute("SELECT status FROM tasks").fetchone()[0], "uncertain")

    def test_ingest_failure_rolls_back_offset_and_tasks_together(self):
        with self.assertRaises(TypeError):
            self.store.ingest([{"update_id": 1}, {"update_id": 2, "bad": object()}])
        self.assertEqual(self.store.offset(), 0)
        self.assertEqual(self.store.pending_count(), 0)

    def test_network_failure_on_copy_is_never_automatically_retried(self):
        self.store.ingest([{"update_id": 1}])
        self.api.call.side_effect = TelegramError(0)
        worker = self.worker(lambda *_: self.journal.call("copyMessage", chat_id=-42))
        worker.process_one()
        self.assertFalse(worker.process_one())
        self.assertEqual(self.api.call.call_count, 1)
        row = self.store.db.execute("SELECT status,payload FROM tasks").fetchone()
        self.assertEqual(row, ("uncertain", "{}"))

    def test_read_failures_are_retried_with_bounded_attempts(self):
        self.store.ingest([{"update_id": 1}])
        self.api.call.side_effect = TelegramError(503)
        worker = self.worker(lambda *_: self.journal.call("getChatMember", chat_id=-42, user_id=7))
        for _ in range(8):
            worker.process_one()
            self.ready()
        self.assertFalse(worker.process_one())
        self.assertEqual(self.api.call.call_count, 8)
        self.assertEqual(self.store.pending_count(), 0)

    def test_full_mp3_workflow_retries_429_without_duplicate_copy(self):
        message = {"message_id": 1, "chat": {"id": 7, "type": "private"}, "from": {"id": 7},
                   "audio": {"file_name": "test.mp3"}}
        self.store.ingest([{"update_id": 1, "message": message}])
        self.api.call.side_effect = [{"status": "administrator"}, TelegramError(429, 3),
                                    {"status": "administrator"}, {"message_id": 90}, {}]
        bot = Bot(self.journal, "TestBot", "-42", "url", "Règles")
        worker = self.worker(lambda _, update: bot.handle(update))
        worker.process_one()
        self.ready()
        worker.process_one()
        self.assertEqual(self.store.pending_count(), 0)
        self.assertEqual(sum(call.args[0] == "copyMessage" for call in self.api.call.call_args_list), 2)

    def test_mp3_confirmation_429_after_restart_does_not_recopy(self):
        update = {"update_id": 1, "message": {"message_id": 1,
                  "chat": {"id": 7, "type": "private"}, "from": {"id": 7},
                  "audio": {"file_name": "test.mp3"}}}
        self.store.ingest([update])
        self.api.call.side_effect = [{"status": "administrator"}, {"message_id": 90}, TelegramError(429, 2)]
        bot = Bot(self.journal, "TestBot", "-42", "url", "Règles")
        self.worker(lambda _, event: bot.handle(event)).process_one()
        self.reopen()
        self.ready()
        self.api.call.side_effect = [{"status": "administrator"}, {}]
        bot = Bot(self.journal, "TestBot", "-42", "url", "Règles")
        self.worker(lambda _, event: bot.handle(event)).process_one()
        self.assertEqual(sum(call.args[0] == "copyMessage" for call in self.api.call.call_args_list), 1)
        self.assertEqual(self.store.pending_count(), 0)

    def test_retry_rechecks_admin_rights_before_copy(self):
        update = {"update_id": 1, "message": {"message_id": 1,
                  "chat": {"id": 7, "type": "private"}, "from": {"id": 7},
                  "audio": {"file_name": "test.mp3"}}}
        self.store.ingest([update])
        self.api.call.side_effect = [{"status": "administrator"}, TelegramError(429, 2),
                                    {"status": "member"}, {}]
        bot = Bot(self.journal, "TestBot", "-42", "url", "Règles")
        worker = self.worker(lambda _, event: bot.handle(event))
        worker.process_one()
        self.ready()
        worker.process_one()
        self.assertEqual(sum(call.args[0] == "copyMessage" for call in self.api.call.call_args_list), 1)
        self.assertIn("Seuls les administrateurs", self.api.call.call_args.kwargs["text"])


class StartupTests(unittest.TestCase):
    def test_transient_outage_waits_then_recovers_without_exiting(self):
        api, stop = Mock(), Mock()
        stop.is_set.return_value = False
        api.call.side_effect = [TelegramError(0), TelegramError(503), {"username": "TestBot"}, {"url": ""}]
        self.assertEqual(wait_for_startup(api, stop), "TestBot")
        self.assertEqual([call.args[0] for call in stop.wait.call_args_list], [2, 4])

    def test_bad_token_fails_without_loop(self):
        api, stop = Mock(), Mock()
        stop.is_set.return_value = False
        api.call.side_effect = TelegramError(401)
        with self.assertRaises(SystemExit):
            wait_for_startup(api, stop)
        stop.wait.assert_not_called()
