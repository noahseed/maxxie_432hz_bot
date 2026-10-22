import json
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from assistant_ai import AIError, AssistantAI
from bot import Bot


class OpenAITests(unittest.TestCase):
    @patch("assistant_ai.urlopen")
    def test_response_payload_and_text_extraction(self, open_url):
        response = open_url.return_value.__enter__.return_value
        response.read.return_value = json.dumps({"status": "completed", "output": [
            {"type": "reasoning"}, {"type": "message", "content": [
                {"type": "output_text", "text": "Bienvenue !"}]}]}).encode()
        result = AssistantAI("fake-key").answer(7, "Comment rejoindre ?", "Pas de spam", "https://t.me/test")
        self.assertEqual(result, "Bienvenue !")
        request = open_url.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["input"], "Comment rejoindre ?")
        self.assertIn("Pas de spam", payload["instructions"])
        self.assertFalse(payload["store"])
        self.assertNotIn("tools", payload)
        self.assertNotIn("7", payload["input"])

    @patch("assistant_ai.urlopen")
    def test_http_and_network_errors_are_sanitized(self, open_url):
        for error in (HTTPError("url", 401, "secret", {}, None), URLError("secret")):
            open_url.side_effect = error
            with self.assertRaises(AIError) as caught:
                AssistantAI("secret").answer(7, "Question", "Règles", "url")
            self.assertNotIn("secret", str(caught.exception))

    @patch("assistant_ai.urlopen")
    def test_empty_and_incomplete_responses_fail(self, open_url):
        response = open_url.return_value.__enter__.return_value
        for result in ({"status": "completed", "output": []}, {"status": "incomplete"}, []):
            response.read.return_value = json.dumps(result).encode()
            with self.assertRaises(AIError):
                AssistantAI("fake").answer(7, "Question", "Règles", "url")

    @patch("assistant_ai.urlopen")
    def test_limits_block_api_calls(self, open_url):
        assistant = AssistantAI("fake", hourly_limit=1)
        self.assertIn("2000", assistant.answer(7, "x" * 2001, "", ""))
        assistant.requests.append(time.monotonic())
        self.assertIn("limite", assistant.answer(7, "Bonjour", "", ""))
        assistant.requests.clear()
        assistant.last_requests[7] = time.monotonic()
        self.assertIn("Patientez", assistant.answer(7, "Bonjour", "", ""))
        open_url.assert_not_called()


class AIRoutingTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.assistant = Mock()
        self.assistant.answer.return_value = "Voici comment rejoindre le groupe."
        self.bot = Bot(self.api, "MaxxieBot", "-10042", "https://t.me/test", "Règles",
                       assistant=self.assistant)

    def test_private_question_is_answered_in_private(self):
        self.bot.handle({"message": {"chat": {"id": 7, "type": "private"}, "text": "Comment rejoindre ?"}})
        self.assistant.answer.assert_called_once_with(7, "Comment rejoindre ?", "Règles", "https://t.me/test")
        self.api.call.assert_called_once_with("sendMessage", chat_id=7, text=self.assistant.answer.return_value)

    def test_start_keeps_buttons_and_does_not_call_ai(self):
        self.bot.handle({"message": {"chat": {"id": 7, "type": "private"}, "text": "/start"}})
        self.assistant.answer.assert_not_called()
        args = self.api.call.call_args.kwargs
        self.assertIn("OpenAI", args["text"])
        self.assertEqual(len(args["reply_markup"]["inline_keyboard"]), 2)

    def test_group_and_mp3_do_not_call_ai(self):
        self.bot.handle({"message": {"chat": {"id": -10042, "type": "supergroup"}, "text": "Bonjour"}})
        self.api.call.return_value = {"status": "member"}
        self.bot.handle({"message": {"chat": {"id": 7, "type": "private"}, "from": {"id": 7},
                                    "audio": {"file_name": "song.mp3"}}})
        self.assistant.answer.assert_not_called()

    def test_ai_failure_preserves_private_fallback(self):
        self.assistant.answer.side_effect = AIError(429)
        self.bot.handle({"message": {"chat": {"id": 7, "type": "private"}, "text": "Bonjour"}})
        args = self.api.call.call_args.kwargs
        self.assertEqual(args["chat_id"], 7)
        self.assertIn("indisponible", args["text"])


if __name__ == "__main__":
    unittest.main()
