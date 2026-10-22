import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot import Bot, StartVisitors, Telegram, TelegramError, connect_ipv4
from reliability import JournalAPI, TaskStore, Worker


class TransportTests(unittest.TestCase):
    @patch("bot.HTTPSConnection")
    def test_connection_is_reused_and_timeouts_are_separate(self, factory):
        connection = factory.return_value
        response = connection.getresponse.return_value
        response.read.return_value = b'{"ok": true, "result": []}'
        response.will_close = False
        api = Telegram("private-token")
        api.call("getUpdates", timeout=30)
        api.call("getMe")
        self.assertEqual(factory.call_count, 1)
        self.assertEqual([call.args[0] for call in connection.sock.settimeout.call_args_list], [35, 8])

    @patch("bot.HTTPSConnection")
    def test_stale_read_connection_is_retried_but_writes_are_not(self, factory):
        stale = Mock()
        stale.request.side_effect = OSError("contains-secret")
        fresh = Mock()
        fresh.getresponse.return_value.read.return_value = b'{"ok": true, "result": {}}'
        fresh.getresponse.return_value.will_close = False
        factory.side_effect = [stale, fresh]
        Telegram("secret").call("getMe")
        stale.close.assert_called_once()
        self.assertEqual(factory.call_count, 2)
        factory.reset_mock(side_effect=True)
        factory.return_value = stale
        with self.assertRaises(TelegramError) as caught:
            Telegram("secret").call("copyMessage", chat_id=7)
        self.assertEqual(factory.call_count, 1)
        self.assertNotIn("secret", str(caught.exception))

    @patch("bot.HTTPSConnection")
    def test_rate_limit_preserves_retry_after(self, factory):
        response = factory.return_value.getresponse.return_value
        response.will_close = False
        response.read.return_value = json.dumps({"ok": False, "error_code": 429,
                                                 "parameters": {"retry_after": 12}}).encode()
        with self.assertRaises(TelegramError) as caught:
            Telegram("secret").call("sendMessage")
        self.assertEqual(caught.exception.retry_after, 12)

    @patch("bot.socket.socket")
    @patch("bot.socket.getaddrinfo")
    def test_ipv4_uses_dns_and_keeps_hostname_for_tls(self, resolve, make_socket):
        resolve.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        result = connect_ipv4(("api.telegram.org", 443), 3)
        resolve.assert_called_once_with("api.telegram.org", 443, socket.AF_INET, socket.SOCK_STREAM)
        result.settimeout.assert_called_once_with(3)


class ResponsivenessTests(unittest.TestCase):
    def test_slow_notification_does_not_delay_another_visitor(self):
        with tempfile.TemporaryDirectory() as directory:
            visitors = StartVisitors(Path(directory) / "visitors.sqlite3")
            api = Mock()
            started, release = threading.Event(), threading.Event()
            def call(method, **params):
                if params.get("chat_id") == 99:
                    started.set()
                    if not release.wait(2):
                        raise RuntimeError("Test notification timeout")
                return {}
            api.call.side_effect = call
            store = TaskStore(Path(directory) / "tasks.sqlite3")
            journal = JournalAPI(api, store)
            bot = Bot(journal, "MaxxieBot", "-10042", "url", "Règles", notify_chat_id=99, visitors=visitors)
            bot.background = store
            stop = threading.Event()
            def handle(kind, payload):
                if kind == "update":
                    bot.handle(payload)
                else:
                    getattr(bot, kind)(payload)
            foreground = Worker(store, journal, handle, 0, stop)
            secondary = Worker(store, journal, handle, 1, stop)
            def enqueue(uid):
                store.ingest([{"update_id": uid, "message": {"chat": {"id": uid, "type": "private"},
                               "from": {"id": uid}, "text": "/start"}}])
                foreground.process_one()
            try:
                enqueue(7)
                secondary.thread.start()
                self.assertTrue(started.wait(1))
                enqueue(8)
                self.assertTrue(any(c.kwargs.get("chat_id") == 8 for c in api.call.call_args_list))
            finally:
                release.set()
                stop.set()
                secondary.thread.join(3)
                store.close()
                visitors.db.close()

    def test_promotion_failure_cannot_hide_welcome(self):
        api = Mock()
        def call(method, **params):
            if method == "getChatMember":
                raise TelegramError(400)
            return {}
        api.call.side_effect = call
        bot = Bot(api, "MaxxieBot", "-10042", "url", "Règles", {7})
        bot.handle({"message": {"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "/start"}})
        self.assertEqual(api.call.call_args_list[0].args, ("sendMessage",))

    def test_expired_callback_still_shows_rules(self):
        api = Mock()
        api.call.side_effect = [TelegramError(400), {}]
        bot = Bot(api, "MaxxieBot", "-10042", "url", "Règles")
        bot.handle({"callback_query": {"id": "old", "data": "rules", "message": {
            "chat": {"id": 7, "type": "private"}}}})
        api.call.assert_called_with("sendMessage", chat_id=7, text="Règles")
