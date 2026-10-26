import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock

from bot import Bot, StartVisitors, TelegramError


class VisitorNotificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "visitors.sqlite3"
        self.visitors = StartVisitors(self.path)
        self.addCleanup(self.visitors.db.close)
        self.api = Mock()
        self.bot = Bot(self.api, "MaxxieBot", "-10042", "https://t.me/test", "Règles",
                       notify_chat_id=99, visitors=self.visitors)

    def start(self, text="/start", user_id=7, chat_type="private"):
        self.bot.handle({"message": {"chat": {"id": user_id, "type": chat_type},
                                    "from": {"id": user_id, "first_name": "Camille", "username": "camille"},
                                    "text": text}})

    def test_first_start_notifies_owner_and_welcomes_visitor(self):
        self.start()
        calls = self.api.call.call_args_list
        self.assertEqual(calls[0].kwargs["chat_id"], 7)
        self.assertEqual(calls[1].kwargs["chat_id"], 99)
        self.assertIn("@camille", calls[1].kwargs["text"])
        self.assertIn("7", calls[1].kwargs["text"])
        self.start()
        self.assertEqual(sum(call.kwargs.get("chat_id") == 99 for call in self.api.call.call_args_list), 1)

    def test_persistent_store_survives_reopen(self):
        self.start()
        reopened = StartVisitors(self.path)
        self.addCleanup(reopened.db.close)
        self.bot.visitors = reopened
        self.api.reset_mock()
        self.start()
        self.assertEqual(self.api.call.call_count, 1)
        self.assertEqual(self.api.call.call_args.kwargs["chat_id"], 7)

    def test_regular_text_and_group_start_do_not_notify(self):
        self.start("Bonjour")
        self.start(chat_type="group")
        self.assertFalse(self.visitors.contains(7))
        self.assertFalse(any(call.kwargs.get("chat_id") == 99 for call in self.api.call.call_args_list))

    def test_deep_link_and_addressed_command_notify(self):
        self.start("/start rules")
        self.start("/start@MaxxieBot", user_id=8)
        self.assertTrue(self.visitors.contains(7))
        self.assertTrue(self.visitors.contains(8))

    def test_failed_notification_does_not_block_welcome_and_can_retry(self):
        self.api.call.side_effect = [{}, TelegramError(403)]
        self.start()
        self.assertEqual(self.api.call.call_args_list[0].kwargs["chat_id"], 7)
        self.assertFalse(self.visitors.contains(7))
        self.api.call.side_effect = None
        self.start()
        self.assertTrue(self.visitors.contains(7))


class WelcomeTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.bot = Bot(self.api, "MaxxieBot", "-10042", "https://t.me/Maxxie_432Hz", "Règlement")

    def test_private_start_gets_five_buttons_with_artists_and_random_side_by_side(self):
        self.bot.handle({"message": {"chat": {"id": 7, "type": "private"}, "text": "/start"}})
        args = self.api.call.call_args.kwargs
        self.assertNotIn("Règlement", args["text"])
        self.assertEqual(args["chat_id"], 7)
        keyboard = args["reply_markup"]["inline_keyboard"]
        self.assertEqual(sum(len(row) for row in keyboard), 5)
        self.assertEqual(keyboard[0][0]["callback_data"], "rules")
        self.assertEqual(keyboard[3][0]["callback_data"], "help")
        self.assertEqual(keyboard[1][0]["url"], "https://t.me/Maxxie_432Hz")
        self.assertEqual([button["text"] for button in keyboard[2]], ["Artistes 🎶", "Hasard 🎲"])
        self.assertEqual([button["callback_data"] for button in keyboard[2]], ["artists", "random"])
        self.assertEqual([len(row) for row in keyboard], [1, 1, 2, 1])

    def test_unknown_commands_and_other_private_messages_are_silent(self):
        for body in ({"text": "/inconnue"}, {"text": "/status"},
                     {"text": "/status@MaxxieBot"}, {"text": "/STATUS"},
                     {"text": "/start@AutreBot"},
                     {"text": "/starting"}, {"text": "Bonjour"},
                     {"sticker": {"file_id": "example"}}, {"photo": []},
                     {"voice": {"file_id": "example"}}, {}):
            with self.subTest(body=body):
                self.api.reset_mock()
                self.bot.handle({"message": {"chat": {"id": 7, "type": "private"},
                                              "from": {"id": 7}, **body}})
                self.api.call.assert_not_called()

    def test_help_button_uses_private_role_aware_help(self):
        self.api.call.return_value = {"status": "member"}
        self.bot.handle({"callback_query": {"id": "help-click", "data": "help", "from": {"id": 7},
                        "message": {"chat": {"id": 7, "type": "private"}}}})
        self.assertEqual(self.api.call.call_args.kwargs["chat_id"], 7)
        self.assertIn("/recherche", self.api.call.call_args.kwargs["text"])
        self.assertNotIn("/statut", self.api.call.call_args.kwargs["text"])
        self.api.reset_mock()
        self.bot.handle({"callback_query": {"id": "help-group", "data": "help", "from": {"id": 7},
                        "message": {"chat": {"id": -10042, "type": "supergroup"}}}})
        self.api.call.assert_called_once_with("answerCallbackQuery", callback_query_id="help-group")

    def test_rules_button_displays_rules_in_private(self):
        self.bot.handle({"callback_query": {"id": "abc", "data": "rules",
                                            "message": {"chat": {"id": 7, "type": "private"}}}})
        self.assertEqual(self.api.call.call_args_list[0].args, ("answerCallbackQuery",))
        self.api.call.assert_called_with("sendMessage", chat_id=7, text="Règlement")

    def test_rules_callback_in_group_does_not_publish_rules(self):
        self.bot.handle({"callback_query": {"id": "abc", "data": "rules",
                                            "message": {"chat": {"id": -10042, "type": "supergroup"}}}})
        self.api.call.assert_called_once_with("answerCallbackQuery", callback_query_id="abc")

    def test_group_deep_link_opens_rules_directly(self):
        self.bot.handle({"message": {"chat": {"id": 7, "type": "private"}, "text": "/start rules"}})
        self.api.call.assert_called_once_with("sendMessage", chat_id=7, text="Règlement")

    def test_new_member_gets_welcome_in_private_only(self):
        self.bot.handle({"message": {"chat": {"id": -10042, "type": "supergroup"},
                                    "new_chat_members": [{"id": 7, "first_name": "Camille"}]}})
        args = self.api.call.call_args.kwargs
        self.assertEqual(args["chat_id"], 7)
        self.assertNotIn("Règlement", args["text"])
        self.assertEqual(args["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "rules")

    def test_unreachable_member_has_no_group_fallback_and_next_member_is_welcomed(self):
        for code in (400, 403):
            self.api.reset_mock()
            self.api.call.side_effect = [TelegramError(code), {}]
            self.bot.handle({"message": {"chat": {"id": -10042, "type": "supergroup"},
                                        "new_chat_members": [{"id": 7}, {"id": 8}]}})
            self.assertEqual([call.kwargs["chat_id"] for call in self.api.call.call_args_list], [7, 8])

    def test_private_welcome_network_failure_can_be_retried(self):
        self.api.call.side_effect = TelegramError(0)
        with self.assertRaises(TelegramError):
            self.bot.handle({"message": {"chat": {"id": -10042, "type": "supergroup"},
                                        "new_chat_members": [{"id": 7}]}})

    def test_other_group_and_regular_messages_are_ignored(self):
        for message in [{"chat": {"id": -99, "type": "group"}, "new_chat_members": [{"id": 7}]},
                        {"chat": {"id": -10042, "type": "supergroup"}, "text": "Bonjour"},
                        {"chat": {"id": -10042, "type": "supergroup"}, "new_chat_members": [{"is_bot": True}]}]:
            self.bot.handle({"message": message})
        self.api.call.assert_not_called()

    def test_api_failure_is_propagated_for_retry(self):
        self.api.call.side_effect = TelegramError(429, 10)
        with self.assertRaises(TelegramError):
            self.bot.private_welcome(7)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.api.call.return_value = {"status": "administrator"}
        self.bot = Bot(self.api, "MaxxieBot", "-10042", "https://t.me/Maxxie_432Hz", "Règles")
        self.message = {"chat": {"id": 7, "type": "private"}, "from": {"id": 7},
                        "message_id": 42, "audio": {"file_name": "Chanson.MP3"},
                        "caption": "Ma légende"}

    def test_admin_audio_is_copied_without_changing_caption(self):
        self.bot.handle({"message": self.message})
        self.assertEqual(self.api.call.call_args_list[0].args, ("getChatMember",))
        self.api.call.assert_any_call("copyMessage", chat_id="-10042", from_chat_id=7, message_id=42)
        self.api.call.assert_called_with("sendMessage", chat_id=7, text="MP3 publié dans le groupe ✅")

    def test_owner_document_mp3_is_accepted(self):
        self.api.call.return_value = {"status": "creator"}
        self.message["document"] = self.message.pop("audio")
        self.bot.handle({"message": self.message})
        self.api.call.assert_any_call("copyMessage", chat_id="-10042", from_chat_id=7, message_id=42)

    def test_non_admin_even_allowlisted_cannot_publish(self):
        self.bot.admin_ids = {7}
        for status in ("member", "restricted", "left", "kicked"):
            self.api.reset_mock()
            self.api.call.return_value = {"status": status}
            self.bot.handle({"message": self.message})
            self.assertNotIn("copyMessage", [call.args[0] for call in self.api.call.call_args_list])

    def test_other_audio_formats_are_rejected(self):
        self.message["audio"] = {"file_name": "chanson.m4a", "mime_type": "audio/mp4"}
        self.bot.handle({"message": self.message})
        self.assertNotIn("copyMessage", [call.args[0] for call in self.api.call.call_args_list])

    def test_verification_failure_never_publishes(self):
        self.api.call.side_effect = TelegramError(0)
        with self.assertRaises(TelegramError):
            self.bot.handle({"message": self.message})
        self.assertEqual(self.api.call.call_count, 1)

    def test_failed_copy_does_not_confirm_success(self):
        self.api.call.side_effect = [{"status": "administrator"}, TelegramError(403), {}]
        self.bot.handle({"message": self.message})
        self.assertIn("n'a pas pu", self.api.call.call_args.kwargs["text"])

    def test_confirmation_failure_does_not_retry_publication(self):
        self.api.call.side_effect = [{"status": "administrator"}, {}, TelegramError(0)]
        self.bot.handle({"message": self.message})
        self.assertEqual(sum(call.args[0] == "copyMessage" for call in self.api.call.call_args_list), 1)


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.bot = Bot(self.api, "MaxxieBot", "-10042", "https://t.me/Maxxie_432Hz", "Règles", {7})

    def test_allowlisted_member_is_promoted_with_limited_rights(self):
        self.api.call.return_value = {"status": "member"}
        self.bot.promote_known({"id": 7})
        args = self.api.call.call_args.kwargs
        self.assertEqual(self.api.call.call_args.args, ("promoteChatMember",))
        self.assertEqual(args["chat_id"], "-10042")
        self.assertEqual(args["user_id"], 7)
        self.assertTrue(args["can_manage_chat"])
        self.assertFalse(args["can_promote_members"])
        self.assertFalse(args["can_restrict_members"])
        self.assertFalse(args["can_delete_messages"])

    def test_unknown_ids_and_impersonated_username_never_promoted(self):
        self.bot.promote_known({"id": 8, "username": "trusted_admin"})
        self.bot.promote_known({"id": 7, "is_bot": True})
        self.api.call.assert_not_called()

    def test_moderator_gets_deletion_and_member_management(self):
        self.bot.moderator_ids = {7}
        self.api.call.return_value = {"status": "member"}
        self.bot.promote_known({"id": 7})
        args = self.api.call.call_args.kwargs
        self.assertTrue(args["can_delete_messages"])
        self.assertTrue(args["can_restrict_members"])
        self.assertFalse(args["can_promote_members"])

    def test_existing_admin_upgrade_preserves_other_rights(self):
        self.bot.moderator_ids = {7}
        self.api.call.return_value = {"status": "administrator", "can_pin_messages": True,
                                      "can_be_edited": True, "can_delete_messages": False}
        self.bot.promote_known({"id": 7})
        args = self.api.call.call_args.kwargs
        self.assertTrue(args["can_pin_messages"])
        self.assertTrue(args["can_delete_messages"])
        self.assertTrue(args["can_restrict_members"])
        self.assertNotIn("can_be_edited", args)

    def test_existing_admins_and_absent_users_unchanged(self):
        for status in ("creator", "administrator", "left", "kicked"):
            self.api.reset_mock()
            self.api.call.return_value = {"status": status}
            self.bot.promote_known({"id": 7})
            self.assertEqual(self.api.call.call_count, 1)

    def test_membership_event_only_applies_to_target_group(self):
        self.api.call.return_value = {"status": "member"}
        self.bot.handle({"chat_member": {"chat": {"id": -99},
                                       "new_chat_member": {"user": {"id": 7}}}})
        self.api.call.assert_not_called()
        self.bot.handle({"chat_member": {"chat": {"id": -10042},
                                       "new_chat_member": {"user": {"id": 7}}}})
        self.assertEqual(self.api.call.call_args.args, ("promoteChatMember",))

    def test_private_start_promotes_existing_member(self):
        self.api.call.return_value = {"status": "member"}
        self.bot.handle({"message": {"chat": {"id": 7, "type": "private"},
                                    "from": {"id": 7}, "text": "/start"}})
        self.assertIn("promoteChatMember", [call.args[0] for call in self.api.call.call_args_list])

    def test_id_command_reports_sender_without_promoting(self):
        self.bot.handle({"message": {"chat": {"id": 7, "type": "private"},
                                    "from": {"id": 7}, "text": "/id"}})
        self.api.call.assert_called_once_with("sendMessage", chat_id=7,
                                              text="Votre identifiant Telegram : 7")


if __name__ == "__main__":
    unittest.main()
