from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock

from backups import create_backup, restore
from bot import Bot
from catalogue import index_music, message_link, search_music
from reliability import JournalAPI, TaskStore, TelegramError, Worker


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = TaskStore(self.root / 'tasks.sqlite3')
        self.addCleanup(lambda: self.store.close())
        self.raw = Mock()
        self.raw.call.return_value = {'status': 'member'}
        self.bot = Bot(self.raw, 'TestBot', '-10042', 'https://t.me/Maxxie_432Hz', 'Règles', owner_user_id=99)
        self.bot.background = self.store

    def song(self, mid=100):
        return {'message_id': mid, 'chat': {'id': -10042, 'type': 'supergroup'},
                'audio': {'title': 'La Vie en rose', 'performer': 'Édith Piaf', 'file_name': 'rose.mp3'},
                'caption': 'Variétés françaises — Album Chansons'}

    def private(self, command, uid=7):
        self.bot.handle({'message': {'chat': {'id': uid, 'type': 'private'}, 'from': {'id': uid}, 'text': command}})

    def test_search_matches_artist_title_filename_caption_and_all_terms(self):
        index_music(self.store, '-10042', self.song())
        for query in ('EDITH PIAF', 'vie ROSE', 'rose.mp3', 'varietes francaises', 'chansons'):
            with self.subTest(query=query):
                self.assertEqual(search_music(self.store, '-10042', query)[0][0], 100)
        for query in ('piaf inconnu', "%' OR 1=1 --", '%', '_'):
            self.assertEqual(search_music(self.store, '-10042', query), [])
        self.assertEqual(search_music(self.store, '-10099', 'piaf'), [])

    def test_document_and_updates_are_idempotent_and_persist_across_restart(self):
        song = {'message_id': 101, 'document': {'file_name': 'Artiste - Étoile.mp3'}}
        index_music(self.store, '-10042', song)
        song['document']['file_name'] = 'Artiste - Océan.mp3'
        index_music(self.store, '-10042', song)
        self.store.close()
        self.store = TaskStore(self.root / 'tasks.sqlite3')
        self.assertEqual(search_music(self.store, '-10042', 'etoile'), [])
        self.assertEqual(len(search_music(self.store, '-10042', 'ocean')), 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM music').fetchone()[0], 1)

    def test_group_messages_are_indexed_silently_and_edits_replace_metadata(self):
        song = self.song()
        self.bot.handle({'message': song})
        self.raw.call.assert_not_called()
        song['audio']['title'] = 'Milord'
        self.bot.handle({'edited_message': song})
        self.assertEqual(search_music(self.store, '-10042', 'vie rose'), [])
        self.assertTrue(search_music(self.store, '-10042', 'milord'))
        song['audio'] = {'file_name': 'Milord.ogg'}
        self.bot.handle({'edited_message': song})
        self.assertEqual(search_music(self.store, '-10042', 'milord'), [])
        self.raw.call.assert_not_called()

    def test_other_groups_and_private_mp3_are_not_indexed_without_publication(self):
        song = self.song()
        song['chat']['id'] = -99
        self.bot.handle({'message': song})
        song['chat'] = {'id': 7, 'type': 'private'}
        song['from'] = {'id': 7}
        self.bot.handle({'message': song})
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM music').fetchone()[0], 0)

    def test_search_returns_private_message_links_and_bounds_result_count(self):
        for mid in range(100, 112):
            index_music(self.store, '-10042', self.song(mid))
        self.private('/recherche@TestBot edith')
        params = self.raw.call.call_args.kwargs
        self.assertEqual(params['chat_id'], 7)
        buttons = params['reply_markup']['inline_keyboard']
        self.assertEqual(len(buttons), 10)
        self.assertEqual(buttons[0][0]['url'], 'https://t.me/Maxxie_432Hz/111')
        self.assertIn('affiner', params['text'])
        self.assertLess(len(params['text']), 4096)

    def test_empty_query_empty_catalogue_and_long_query_are_explained(self):
        for command in ('/recherche', '/recherche x', '/recherche ' + 'a' * 121):
            self.private(command)
            self.assertIn('2 et 120', self.raw.call.call_args.kwargs['text'])
        self.private('/recherche inconnu')
        self.assertIn('Aucun morceau', self.raw.call.call_args.kwargs['text'])
        self.assertNotIn('reply_markup', self.raw.call.call_args.kwargs)

    def test_help_is_role_specific_and_never_exposes_owner_command_to_visitors(self):
        self.private('/aide')
        text = self.raw.call.call_args.kwargs['text']
        self.assertIn('/recherche', text)
        self.assertNotIn('/indexer', text)
        self.assertNotIn('/statut', text)
        self.raw.call.return_value = {'status': 'administrator'}
        self.private('/aide@TestBot')
        text = self.raw.call.call_args.kwargs['text']
        self.assertIn('/indexer', text)
        self.assertNotIn('/statut', text)
        self.private('/aide', 99)
        self.assertIn('/statut', self.raw.call.call_args.kwargs['text'])

    def test_group_search_and_help_do_not_reply_in_group(self):
        for command in ('/aide', '/recherche piaf'):
            self.bot.handle({'message': {'chat': {'id': -10042, 'type': 'supergroup'}, 'text': command}})
        self.raw.call.assert_not_called()

    def test_index_old_song_requires_live_admin_rights_and_confirms_privately(self):
        event = {'message': {'chat': {'id': -10042, 'type': 'supergroup'}, 'from': {'id': 7},
                             'text': '/indexer@TestBot', 'reply_to_message': self.song()}}
        self.bot.handle(event)
        self.assertEqual(search_music(self.store, '-10042', 'piaf'), [])
        self.raw.call.reset_mock()
        self.raw.call.return_value = {'status': 'administrator'}
        self.bot.handle(event)
        self.assertTrue(search_music(self.store, '-10042', 'piaf'))
        self.assertEqual(self.raw.call.call_args.kwargs['chat_id'], 7)
        self.assertEqual(self.raw.call.call_args.args[0], 'sendMessage')

    def test_successful_copy_is_indexed_with_group_message_id_even_after_confirmation_retry(self):
        journal = JournalAPI(self.raw, self.store)
        self.bot.api = journal
        message = self.song(12)
        message.update(chat={'id': 7, 'type': 'private'}, **{'from': {'id': 7}})
        self.store.ingest([{'update_id': 1, 'message': message}])
        self.raw.call.side_effect = [{'status': 'administrator'}, {'message_id': 90}, TelegramError(429, 1), {}]
        worker = Worker(self.store, journal, lambda _, u: self.bot.handle(u), 0, threading.Event())
        worker.process_one()
        with self.store.db:
            self.store.db.execute('UPDATE tasks SET ready=0')
            # Simuler l'index manquant après un arrêt entre copie et indexation.
            self.store.db.execute('DELETE FROM music')
        worker.process_one()
        self.assertEqual(search_music(self.store, '-10042', 'piaf')[0][0], 90)
        self.assertEqual(sum(c.args[0] == 'copyMessage' for c in self.raw.call.call_args_list), 1)

    def test_catalogue_is_included_in_backup_restore_and_not_pruned_by_task_cleanup(self):
        index_music(self.store, '-10042', self.song())
        self.store.cleanup()
        visitors = self.root / 'visitors.sqlite3'
        with closing(sqlite3.connect(visitors)) as db, db:
            db.execute('CREATE TABLE visitors(user_id INTEGER PRIMARY KEY)')
        snapshot = create_backup(self.root / 'tasks.sqlite3', visitors, self.root / 'backups')
        restored = restore(snapshot, self.root / 'restored')
        with closing(sqlite3.connect(restored / 'tasks.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT message_id FROM music').fetchone()[0], 100)

    def test_links_for_private_groups_do_not_use_invitation_as_message_path(self):
        self.assertEqual(message_link('-10042', 'https://t.me/+secret', 100), 'https://t.me/c/42/100')
        self.assertIsNone(message_link('-42', 'https://t.me/+secret', 100))
