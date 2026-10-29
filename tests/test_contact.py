import time
import unittest
from unittest.mock import Mock, patch

from bot import Bot
from reliability import TaskStore, TelegramError


class ContactTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.api.call.return_value = {'message_id': 100}
        self.store = TaskStore(':memory:')
        self.addCleanup(self.store.close)
        self.bot = Bot(self.api, 'bot', '-10042', 'https://t.me/test', 'Rules', owner_user_id=99)
        self.bot.background = self.store

    def contact(self, kind='private'):
        self.bot.handle({'callback_query': {'id': 'contact', 'data': 'contact', 'from': {'id': 7},
                        'message': {'chat': {'id': 7, 'type': kind}}}})

    def message(self, **extra):
        self.bot.handle({'message': {'chat': {'id': 7, 'type': 'private'},
                        'from': {'id': 7, 'first_name': 'Camille'}, 'message_id': 101,
                        'reply_to_message': {'message_id': 100}, **extra}})

    def test_prompt_and_forward_private_text_with_confirmation(self):
        self.contact()
        self.assertTrue(self.api.call.call_args.kwargs['reply_markup']['force_reply'])
        self.api.reset_mock()
        self.message(text='Bonjour')
        calls = self.api.call.call_args_list
        self.assertEqual([c.args[0] for c in calls], ['sendMessage', 'forwardMessage', 'sendMessage'])
        self.assertEqual(calls[1].kwargs, {'chat_id': 99, 'from_chat_id': 7, 'message_id': 101})
        self.assertEqual(calls[-1].kwargs['chat_id'], 7)

    def test_regular_messages_and_unknown_replies_are_not_forwarded(self):
        self.message(text='Bonjour')
        self.api.call.assert_not_called()
        self.contact()
        self.api.reset_mock()
        self.message(text='Bonjour', reply_to_message={'message_id': 999})
        self.api.call.assert_not_called()

    def test_contact_media_is_forwarded_instead_of_published(self):
        self.contact()
        self.api.reset_mock()
        with patch.object(self.bot, 'publish_mp3') as publish:
            self.message(audio={'file_id': 'audio'})
            publish.assert_not_called()
        self.assertEqual(self.api.call.call_args_list[1].args[0], 'forwardMessage')

    def test_expired_prompts_are_ignored_and_cleaned(self):
        self.contact()
        with self.store.db:
            self.store.db.execute('UPDATE contact_prompts SET expires=?', (time.time() - 1,))
        self.api.reset_mock()
        self.message(text='Bonjour')
        self.api.call.assert_not_called()
        self.store.cleanup()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM contact_prompts').fetchone()[0], 0)

    def test_group_button_cannot_start_contact(self):
        self.contact('group')
        self.api.call.assert_called_once_with('answerCallbackQuery', callback_query_id='contact')

    def test_failure_is_reported_to_visitor(self):
        self.contact()
        self.api.reset_mock()
        self.api.call.side_effect = [{}, TelegramError(403), {}]
        self.message(text='Bonjour')
        self.assertIn('pas pu', self.api.call.call_args.kwargs['text'])
        self.assertEqual(self.api.call.call_args.kwargs['chat_id'], 7)

    def test_no_owner_means_contact_unavailable(self):
        self.bot.owner_user_id = None
        self.contact()
        self.assertIn('indisponible', self.api.call.call_args.kwargs['text'])
