from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock

from bot import Bot
from catalogue import artists_page, create_session, index_music, music_page
from import_catalogue import import_export
from reliability import JournalAPI, TaskStore, TelegramError, Worker


class BrowsingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / 'tasks.sqlite3'
        self.store = TaskStore(self.path)
        self.addCleanup(lambda: self.store.close())
        self.raw = Mock()
        self.bot = Bot(self.raw, 'TestBot', '-10042', 'https://t.me/Maxxie_432Hz', 'Règles')
        self.bot.background = self.store

    def song(self, mid, artist='Édith Piaf', title='Chanson'):
        return {'message_id': mid, 'audio': {'file_name': 'song.mp3', 'performer': artist, 'title': title}}

    def private(self, text):
        self.bot.handle({'message': {'chat': {'id': 7, 'type': 'private'}, 'from': {'id': 7}, 'text': text}})

    def click(self, data, uid=7, sender=None, chat_type='private'):
        self.bot.handle({'callback_query': {'id': 'click', 'from': {'id': uid if sender is None else sender},
                        'message': {'chat': {'id': uid, 'type': chat_type}}, 'data': data}})

    def result(self):
        return self.raw.call.call_args.kwargs

    def test_search_next_and_previous_pages_without_missing_or_repeated_results(self):
        for mid in range(1, 26):
            index_music(self.store, '-10042', self.song(mid))
        self.private('/recherche chanson')
        first = self.result()
        self.assertIn('Page 1/3', first['text'])
        first_urls = {row[0]['url'] for row in first['reply_markup']['inline_keyboard'] if 'url' in row[0]}
        self.click(first['reply_markup']['inline_keyboard'][-1][0]['callback_data'])
        second = self.result()
        self.assertIn('Page 2/3', second['text'])
        second_urls = {row[0]['url'] for row in second['reply_markup']['inline_keyboard'] if 'url' in row[0]}
        self.assertFalse(first_urls & second_urls)
        previous, following = second['reply_markup']['inline_keyboard'][-1]
        self.click(following['callback_data'])
        self.assertIn('Page 3/3', self.result()['text'])
        self.assertEqual(len(self.result()['reply_markup']['inline_keyboard']), 6)
        self.click(previous['callback_data'])
        self.assertIn('Page 1/3', self.result()['text'])

    def test_artist_selection_uses_exact_normalized_artist_and_counts(self):
        for mid, artist in ((1, 'Édith Piaf'), (2, 'EDITH PIAF'), (3, 'Édith Piaf Tribute'), (4, '')):
            index_music(self.store, '-10042', self.song(mid, artist))
        self.private('/artistes@TestBot')
        rows = self.result()['reply_markup']['inline_keyboard']
        selected = next(row[0] for row in rows if '(2)' in row[0]['text'])
        self.click(selected['callback_data'])
        params = self.result()
        self.assertIn('2 résultat(s)', params['text'])
        urls = {row[0]['url'] for row in params['reply_markup']['inline_keyboard']}
        self.assertEqual(urls, {'https://t.me/Maxxie_432Hz/1', 'https://t.me/Maxxie_432Hz/2'})
        self.assertNotIn('Tribute', params['text'])

    def test_welcome_catalogue_buttons_open_existing_features_privately(self):
        index_music(self.store, '-10042', self.song(1))
        self.click('artists')
        self.assertIn('Artistes du catalogue', self.result()['text'])
        self.click('random')
        self.assertEqual(self.result()['reply_markup']['inline_keyboard'][0][0]['url'],
                         'https://t.me/Maxxie_432Hz/1')
        for callback in ('artists', 'random'):
            self.raw.reset_mock()
            self.click(callback, uid=-10042, chat_type='supergroup')
            self.raw.call.assert_called_once_with('answerCallbackQuery', callback_query_id='click')
            self.raw.reset_mock()
            self.click(callback, sender=8)
            self.raw.call.assert_called_once_with('answerCallbackQuery', callback_query_id='click')

    def test_artist_list_is_paginated(self):
        for mid in range(1, 13):
            index_music(self.store, '-10042', self.song(mid, f'Artiste {mid:02}'))
        self.private('/artistes')
        self.assertIn('Page 1/2', self.result()['text'])
        self.click(self.result()['reply_markup']['inline_keyboard'][-1][0]['callback_data'])
        self.assertIn('Page 2/2', self.result()['text'])
        self.assertIn('Artiste 11', self.result()['text'])

    def test_pagination_survives_restart_and_rejects_other_users(self):
        for mid in range(1, 12):
            index_music(self.store, '-10042', self.song(mid))
        self.private('/recherche chanson')
        data = self.result()['reply_markup']['inline_keyboard'][-1][0]['callback_data']
        self.assertLessEqual(len(data.encode()), 64)
        self.store.close()
        self.store = TaskStore(self.path)
        self.bot.background = self.store
        self.click(data, uid=8)
        self.assertIn('expiré', self.result()['text'])
        self.click(data)
        self.assertIn('Page 2/2', self.result()['text'])

    def test_expired_session_is_explained_and_cleaned(self):
        token = create_session(self.store, 7, 'search', 'chanson')
        with self.store.db:
            self.store.db.execute('UPDATE browse_sessions SET expires=0')
        self.click(f'browse:{token}:0')
        self.assertIn('Relancez /recherche', self.result()['text'])
        self.store.cleanup()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM browse_sessions').fetchone()[0], 0)

    def test_bad_callbacks_and_group_commands_do_not_publish_any_result(self):
        token = create_session(self.store, 7, 'artists')
        for data in ('browse:bad:0', f'browse:{token}:-1', f'browse:{token}:99999999999999999'):
            self.raw.reset_mock()
            self.click(data)
            self.raw.call.assert_called_once_with('answerCallbackQuery', callback_query_id='click')
        self.raw.reset_mock()
        self.click(f'browse:{token}:0', sender=8)
        self.raw.call.assert_called_once_with('answerCallbackQuery', callback_query_id='click')
        for command in ('/artistes', '/hasard'):
            self.raw.reset_mock()
            self.bot.handle({'message': {'chat': {'id': -10042, 'type': 'supergroup'}, 'text': command}})
            self.raw.call.assert_not_called()

    def test_random_is_scoped_to_group_and_can_be_requested_again(self):
        index_music(self.store, '-10099', self.song(99, 'Autre groupe'))
        index_music(self.store, '-10042', self.song(1))
        self.private('/hasard@TestBot')
        self.assertEqual(self.result()['reply_markup']['inline_keyboard'][0][0]['url'], 'https://t.me/Maxxie_432Hz/1')
        self.click('random')
        self.assertIn('Édith Piaf', self.result()['text'])

    def test_empty_catalogue_and_help_are_clear(self):
        self.private('/artistes')
        self.assertIn('Aucun artiste', self.result()['text'])
        self.private('/hasard')
        self.assertIn('Aucun morceau', self.result()['text'])
        self.raw.call.return_value = {'status': 'member'}
        self.private('/aide')
        self.assertIn('/artistes', self.result()['text'])
        self.assertIn('/hasard', self.result()['text'])

    def test_random_retry_retains_initial_choice(self):
        for mid in range(1, 4):
            index_music(self.store, '-10042', self.song(mid))
        journal = JournalAPI(self.raw, self.store)
        self.bot.api = journal
        self.store.ingest([{'update_id': 1, 'message': {'chat': {'id': 7, 'type': 'private'}, 'text': '/hasard'}}])
        self.raw.call.side_effect = [TelegramError(429, 1), {}]
        worker = Worker(self.store, journal, lambda _, u: self.bot.handle(u), 0, threading.Event())
        worker.process_one()
        with self.store.db:
            self.store.db.execute('UPDATE tasks SET ready=0')
        worker.process_one()
        self.assertEqual(self.raw.call.call_args_list[0].kwargs, self.raw.call.call_args_list[1].kwargs)

    def test_legacy_database_migration_preserves_songs_and_export_fills_artists(self):
        legacy = self.root / 'legacy.sqlite3'
        with closing(sqlite3.connect(legacy)) as db, db:
            db.execute('CREATE TABLE music(group_id TEXT,message_id INTEGER,label TEXT,searchable TEXT,PRIMARY KEY(group_id,message_id))')
            db.execute("INSERT INTO music VALUES ('-10042',1,'Titre corrigé','chanson')")
            db.execute("INSERT INTO music VALUES ('-10042',2,'Aaliyah — Try Again','try again')")
        old_store = TaskStore(legacy)
        try:
            self.assertEqual(music_page(old_store, '-10042', 'search', 'chanson')[1], 1)
            self.assertEqual(old_store.db.execute('SELECT artist,artist_key FROM music WHERE message_id=2').fetchone(),
                             ('Aaliyah', 'aaliyah'))
            source = self.root / 'export.json'
            source.write_text(json.dumps({'id': 42, 'type': 'public_supergroup', 'messages': [
                {'type': 'message', 'id': 1, 'file_name': 'song.mp3', 'performer': 'Édith Piaf'}]}))
            result = import_export(source, legacy, '-10042')
            self.assertEqual(result['added'], 0)
            self.assertIn(('edith piaf', 'Édith Piaf', 1), artists_page(old_store, '-10042')[0])
            self.assertEqual(old_store.db.execute('SELECT label FROM music').fetchone()[0], 'Titre corrigé')
        finally:
            old_store.close()
