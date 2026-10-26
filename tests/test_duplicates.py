from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock

from backups import create_backup, restore
from bot import Bot
from catalogue import find_duplicates, index_music
from duplicates import create_publication, resolve_publication
from reliability import JournalAPI, TaskStore, TelegramError, Worker


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / 'tasks.sqlite3'
        self.store = TaskStore(self.path)
        self.addCleanup(lambda: self.store.close())
        self.raw = Mock()
        self.status = 'administrator'
        def call(method, **params):
            if method == 'getChatMember': return {'status': self.status}
            if method == 'copyMessage': return {'message_id': 900}
            return {'message_id': 300}
        self.raw.call.side_effect = call
        self.journal = JournalAPI(self.raw, self.store)
        self.bot = Bot(self.journal, 'TestBot', '-10042', 'https://t.me/Maxxie_432Hz', 'Règles')
        self.bot.background = self.store
        self.worker = Worker(self.store, self.journal, self.handle, 0, threading.Event())
        self.update_id = 0
        self.message = {'message_id': 12, 'chat': {'id': 7, 'type': 'private'}, 'from': {'id': 7},
                        'audio': {'file_name': 'Edith - Chanson.mp3', 'performer': 'Édith Piaf',
                                  'title': 'Chanson [432 Hz]', 'file_unique_id': 'file-one'}}
        index_music(self.store, '-10042', self.message, 100)

    def handle(self, kind, payload):
        if kind == 'update': self.bot.handle(payload)
        elif kind == 'publish_confirmed': self.bot.publish_confirmed(payload)
        else: raise ValueError(kind)

    def update(self, body):
        self.update_id += 1
        self.store.ingest([{'update_id': self.update_id, **body}])
        self.worker.process_one()

    def warn(self):
        self.update({'message': self.message})
        return self.store.db.execute('SELECT token FROM duplicate_publications ORDER BY rowid LIMIT 1').fetchone()[0]

    def click(self, token, decision='yes', uid=7):
        self.update({'callback_query': {'id': str(self.update_id), 'from': {'id': uid},
                     'message': {'chat': {'id': uid, 'type': 'private'}}, 'data': f'duplicate:{token}:{decision}'}})

    def copies(self):
        return [call for call in self.raw.call.call_args_list if call.args[0] == 'copyMessage']

    def reopen(self):
        self.store.close()
        self.store = TaskStore(self.path)
        self.journal = JournalAPI(self.raw, self.store)
        self.bot.api = self.journal
        self.bot.background = self.store
        self.worker = Worker(self.store, self.journal, self.handle, 0, threading.Event())

    def test_file_identity_title_and_filename_are_separate_match_reasons(self):
        self.assertEqual(find_duplicates(self.store, '-10042', self.message)[1], 'Même fichier Telegram')
        message = {**self.message, 'audio': {**self.message['audio'], 'file_unique_id': 'new', 'title': 'CHANSON [432Hz]'}}
        self.assertEqual(find_duplicates(self.store, '-10042', message)[1], 'Même artiste et même titre')
        message['audio']['title'] = 'Autre titre'
        self.assertEqual(find_duplicates(self.store, '-10042', message)[1], 'Même nom de fichier')
        self.assertEqual(find_duplicates(self.store, '-10099', message)[0], [])

    def test_different_artist_or_acoustic_version_is_not_a_title_duplicate(self):
        for artist, title in (('Autre artiste', 'Chanson'), ('Édith Piaf', 'Chanson acoustique')):
            message = {**self.message, 'audio': {'file_name': 'different.mp3', 'performer': artist, 'title': title}}
            self.assertEqual(find_duplicates(self.store, '-10042', message)[0], [])

    def test_warning_prevents_copy_and_contains_existing_link_and_choice(self):
        self.warn()
        self.assertEqual(self.copies(), [])
        params = self.raw.call.call_args.kwargs
        self.assertEqual(params['chat_id'], 7)
        keyboard = params['reply_markup']['inline_keyboard']
        self.assertEqual(keyboard[0][0]['url'], 'https://t.me/Maxxie_432Hz/100')
        self.assertEqual([b['text'] for b in keyboard[-1]], ['Publier quand même', 'Annuler'])
        self.assertTrue(all(len(b['callback_data'].encode()) <= 64 for b in keyboard[-1]))

    def test_repeated_approval_and_restart_publish_only_once(self):
        token = self.warn()
        self.click(token)
        self.reopen()
        self.worker.process_one()
        self.click(token)
        while self.worker.process_one(): pass
        self.assertEqual(len(self.copies()), 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM tasks WHERE kind='publish_confirmed'").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('SELECT message FROM duplicate_publications').fetchone()[0], '{}')

    def test_cancel_other_user_and_expiration_never_enqueue_publication(self):
        token = self.warn()
        self.click(token, uid=8)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM tasks WHERE kind='publish_confirmed'").fetchone()[0], 0)
        self.click(token, 'no')
        self.click(token)
        self.assertEqual(self.copies(), [])
        token = create_publication(self.store, '-10042', self.message)
        with self.store.db: self.store.db.execute('UPDATE duplicate_publications SET expires=0')
        self.assertEqual(resolve_publication(self.store, token, 7, '-10042', True), 'expired')
        self.store.cleanup()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM duplicate_publications').fetchone()[0], 0)

    def test_permissions_are_checked_on_confirmation_and_again_before_copy(self):
        token = self.warn()
        self.status = 'member'
        self.click(token)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM tasks WHERE kind='publish_confirmed'").fetchone()[0], 0)
        self.status = 'administrator'
        self.click(token)
        self.status = 'member'
        self.worker.process_one()
        self.assertEqual(self.copies(), [])

    def test_group_change_invalidates_queued_publication(self):
        token = self.warn()
        self.click(token)
        self.bot.group_id = '-10099'
        self.worker.process_one()
        self.assertEqual(self.copies(), [])
        self.assertEqual(self.store.db.execute("SELECT status FROM tasks WHERE kind='publish_confirmed'").fetchone()[0], 'failed')

    def test_warning_429_retry_keeps_usable_confirmation(self):
        original = self.raw.call.side_effect
        failed = False
        def call(method, **params):
            nonlocal failed
            if method == 'sendMessage' and 'Doublon possible' in params.get('text', '') and not failed:
                failed = True
                raise TelegramError(429, 1)
            return original(method, **params)
        self.raw.call.side_effect = call
        token = self.warn()
        with self.store.db: self.store.db.execute('UPDATE tasks SET ready=0')
        self.worker.process_one()
        self.assertIn(token, self.raw.call.call_args.kwargs['reply_markup']['inline_keyboard'][-1][0]['callback_data'])
        self.click(token)
        self.worker.process_one()
        self.assertEqual(len(self.copies()), 1)

    def test_new_file_is_published_normally_and_confirmation_429_does_not_warn_or_recopy(self):
        self.message['audio'] = {'file_name': 'new.mp3', 'performer': 'New artist', 'title': 'New song'}
        original = self.raw.call.side_effect
        failed = False
        def call(method, **params):
            nonlocal failed
            if method == 'sendMessage' and params.get('text') == 'MP3 publié dans le groupe ✅' and not failed:
                failed = True
                raise TelegramError(429, 1)
            return original(method, **params)
        self.raw.call.side_effect = call
        self.update({'message': self.message})
        with self.store.db: self.store.db.execute('UPDATE tasks SET ready=0')
        self.worker.process_one()
        self.assertEqual(len(self.copies()), 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM duplicate_publications').fetchone()[0], 0)

    def test_direct_group_duplicate_only_warns_sender_in_private_and_ignores_its_own_entry(self):
        message = {**self.message, 'message_id': 101, 'chat': {'id': -10042, 'type': 'supergroup'}}
        self.update({'message': message})
        params = self.raw.call.call_args.kwargs
        self.assertEqual(params['chat_id'], 7)
        self.assertIn('déjà dans le groupe', params['text'])
        self.assertEqual(self.copies(), [])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM music').fetchone()[0], 2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM duplicate_publications').fetchone()[0], 0)

    def test_restore_invalidates_pending_confirmations(self):
        self.warn()
        visitors = self.root / 'visitors.sqlite3'
        with closing(sqlite3.connect(visitors)) as db, db:
            db.execute('CREATE TABLE visitors(user_id INTEGER PRIMARY KEY)')
        snapshot = create_backup(self.path, visitors, self.root / 'backups')
        restored = restore(snapshot, self.root / 'restored')
        with closing(sqlite3.connect(restored / 'tasks.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM duplicate_publications').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM duplicate_publications').fetchone()[0], 1)

    def test_legacy_catalogue_gets_track_keys_without_losing_entries(self):
        path = self.root / 'legacy.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE music(group_id TEXT,message_id INTEGER,label TEXT,searchable TEXT,PRIMARY KEY(group_id,message_id))')
            db.execute("INSERT INTO music VALUES ('-10042',100,'Édith Piaf — Chanson [432Hz]','chanson')")
        store = TaskStore(path)
        try:
            self.assertEqual(find_duplicates(store, '-10042', self.message)[1], 'Même artiste et même titre')
        finally: store.close()
