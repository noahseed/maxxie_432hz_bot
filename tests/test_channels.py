from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from bot import Bot, wait_for_destination
from catalogue import index_music, search_music
from import_catalogue import export_rows
from reliability import TaskStore, TelegramError


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = TaskStore(Path(self.directory.name) / 'tasks.sqlite3')
        self.addCleanup(self.store.close)
        self.raw = Mock()
        self.raw.call.return_value = {'status': 'administrator', 'can_post_messages': True}
        self.bot = Bot(self.raw, 'TestBot', '-10042', 'https://t.me/ChannelTest', 'Règles',
                       admin_ids={7}, moderator_ids={7}, notify_chat_id=99, owner_user_id=99, chat_type='channel')
        self.bot.background = self.store
        self.song = {'message_id': 100, 'chat': {'id': -10042, 'type': 'channel'},
                     'audio': {'file_name': 'music.mp3', 'performer': 'Artiste', 'title': 'Chanson'}}

    def test_channel_posts_and_edits_are_indexed_without_writing_to_channel(self):
        self.bot.handle({'channel_post': self.song})
        self.assertEqual(search_music(self.store, '-10042', 'chanson')[0][0], 100)
        self.song['audio']['title'] = 'Autre'
        self.bot.handle({'edited_channel_post': self.song})
        self.assertEqual(search_music(self.store, '-10042', 'chanson'), [])
        self.assertTrue(search_music(self.store, '-10042', 'autre'))
        self.raw.call.assert_not_called()
        self.song['chat']['id'] = -10099
        self.song['message_id'] = 101
        self.bot.handle({'channel_post': self.song})
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM music').fetchone()[0], 1)

    def test_channel_duplicate_without_author_notifies_owner_only(self):
        index_music(self.store, '-10042', self.song)
        self.song['message_id'] = 101
        self.bot.handle({'channel_post': self.song})
        self.assertEqual(self.raw.call.call_args.kwargs['chat_id'], 99)
        self.assertIn('dans le canal', self.raw.call.call_args.kwargs['text'])

    def test_channel_menu_and_help_use_channel_labels(self):
        self.bot.private_welcome(7)
        keyboard = self.raw.call.call_args.kwargs['reply_markup']['inline_keyboard']
        self.assertIn('du canal', next(b['text'] for row in keyboard for b in row if b.get('callback_data') == 'rules'))
        self.assertIn('le canal', keyboard[1][0]['text'])
        self.bot.show_help(7, 7)
        self.assertIn('transférez-le du canal', self.raw.call.call_args.kwargs['text'])
        self.assertNotIn('/indexer', self.raw.call.call_args.kwargs['text'])

    def test_destination_placeholder_preserves_group_and_channel_text(self):
        for kind, label in (("channel", "canal"), ("supergroup", "groupe")):
            bot = Bot(self.raw, 'TestBot', '-10042', 'url', 'Règles du {destination}',
                      welcome='Bienvenue dans le {destination}', chat_type=kind)
            self.assertEqual(bot.rules, 'Règles du ' + label)
            self.assertEqual(bot.welcome, 'Bienvenue dans le ' + label)

    def test_channel_admin_without_post_permission_cannot_publish_or_confirm(self):
        message = {**self.song, 'chat': {'id': 7, 'type': 'private'}, 'from': {'id': 7}}
        self.raw.call.return_value = {'status': 'administrator', 'can_post_messages': False}
        self.bot.publish_mp3(message)
        self.bot.confirm_duplicate(7, 'a'*16, True)
        self.assertFalse(any(c.args[0] == 'copyMessage' for c in self.raw.call.call_args_list))
        self.assertIn('autorisés à publier', self.raw.call.call_args.kwargs['text'])

    def test_allowed_publication_targets_channel_and_indexes_returned_id(self):
        self.raw.call.side_effect = [{'status': 'administrator', 'can_post_messages': True}, {'message_id': 200}, {}]
        message = {**self.song, 'chat': {'id': 7, 'type': 'private'}, 'from': {'id': 7}}
        self.bot.publish_mp3(message)
        self.raw.call.assert_any_call('copyMessage', chat_id='-10042', from_chat_id=7, message_id=100)
        self.assertEqual(search_music(self.store, '-10042', 'chanson')[0][0], 200)
        self.assertEqual(self.raw.call.call_args.kwargs['text'], 'MP3 publié dans le canal ✅')

    def test_channel_promotion_grants_posting_without_group_only_flags(self):
        self.raw.call.return_value = {'status': 'member'}
        self.bot.promote_known({'id': 7})
        rights = self.raw.call.call_args.kwargs
        self.assertTrue(rights['can_post_messages'])
        self.assertTrue(rights['can_delete_messages'])
        self.assertFalse(rights['can_promote_members'])
        self.assertNotIn('can_restrict_members', rights)
        self.assertNotIn('can_pin_messages', rights)

    def test_existing_channel_admin_keeps_editing_rights_when_posting_is_added(self):
        self.raw.call.return_value = {'status': 'administrator', 'can_post_messages': False,
                                      'can_edit_messages': True, 'can_delete_messages': True, 'can_be_edited': True}
        self.bot.promote_known({'id': 7})
        self.assertTrue(self.raw.call.call_args.kwargs['can_edit_messages'])
        self.assertTrue(self.raw.call.call_args.kwargs['can_post_messages'])
        self.assertNotIn('can_be_edited', self.raw.call.call_args.kwargs)

    def test_forwarded_channel_mp3_is_indexed_without_republication(self):
        message = {**self.song, 'message_id': 12, 'chat': {'id': 7, 'type': 'private'}, 'from': {'id': 7},
                   'forward_origin': {'type': 'channel', 'chat': {'id': -10042}, 'message_id': 100}}
        self.bot.handle({'message': message})
        self.assertEqual(search_music(self.store, '-10042', 'chanson')[0][0], 100)
        self.assertFalse(any(c.args[0] == 'copyMessage' for c in self.raw.call.call_args_list))
        self.assertIn('ajouté au catalogue', self.raw.call.call_args.kwargs['text'])
        self.raw.call.return_value = {'status': 'member'}
        message['forward_origin']['message_id'] = 101
        self.bot.handle({'message': message})
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM music').fetchone()[0], 1)

    def test_old_group_catalogue_is_not_reassigned_to_channel(self):
        index_music(self.store, '-10099', self.song)
        self.bot.find_music(7, 'chanson')
        self.assertIn('Aucun morceau', self.raw.call.call_args.kwargs['text'])
        self.assertEqual(self.store.db.execute('SELECT group_id FROM music').fetchone()[0], '-10099')

    def test_channel_exports_supported_but_wrong_destination_rejected(self):
        for kind in ('public_channel', 'private_channel'):
            data = {'id': 42, 'type': kind, 'messages': [{'id': 100, 'type': 'message', 'file_name': 'song.mp3'}]}
            self.assertEqual(len(export_rows(data, '-10042')), 1)
            with self.assertRaises(ValueError): export_rows(data, '-10099')


class DestinationStartupTests(unittest.TestCase):
    def test_group_detection_keeps_existing_mode(self):
        api = Mock()
        api.call.return_value = {'type': 'supergroup'}
        self.assertEqual(wait_for_destination(api, '-42', threading.Event()), 'supergroup')

    def test_channel_requires_bot_administrator_and_posting_permission(self):
        for rights in ({'status': 'member'}, {'status': 'administrator', 'can_post_messages': False}):
            api = Mock()
            api.call.side_effect = [{'type': 'channel'}, {'id': 1}, rights]
            with self.assertRaises(SystemExit): wait_for_destination(api, '-10042', threading.Event())
        api.call.side_effect = [{'type': 'channel'}, {'id': 1}, {'status': 'administrator', 'can_post_messages': True}]
        self.assertEqual(wait_for_destination(api, '-10042', threading.Event()), 'channel')

    def test_transient_destination_error_is_retried(self):
        api, stop = Mock(), Mock()
        stop.is_set.return_value = False
        api.call.side_effect = [TelegramError(429, 2), {'type': 'channel'}, {'id': 1},
                                {'status': 'administrator', 'can_post_messages': True}]
        self.assertEqual(wait_for_destination(api, '-10042', stop), 'channel')
        stop.wait.assert_called_once_with(2)
