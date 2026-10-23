from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from import_catalogue import import_export
from reliability import TaskStore


class ImportCatalogueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = self.root / 'tasks.sqlite3'
        self.store = TaskStore(self.database)
        self.addCleanup(self.store.close)
        self.source = self.root / 'export.json'
        self.data = {'id': 42, 'type': 'public_supergroup', 'messages': [
            {'type': 'message', 'id': 10, 'file_name': 'Étoile.mp3', 'performer': 'Artiste',
             'title': 'Étoile', 'text': ['Album ', {'type': 'bold', 'text': 'Test'}]},
            {'type': 'service', 'id': 11}, {'type': 'message', 'id': 12, 'text': 'Bonjour'}]}

    def run_import(self, group='-10042'):
        self.source.write_text(json.dumps(self.data))
        return import_export(self.source, self.database, group)

    def test_import_is_idempotent_and_preserves_live_metadata_and_task_state(self):
        self.store.ingest([{'update_id': 1}])
        task = self.store.claim(0)
        self.assertEqual(self.run_import(), {'mp3': 1, 'added': 1, 'existing': 0, 'total': 1})
        with self.store.db:
            self.store.db.execute("UPDATE music SET label='Titre corrigé'")
        self.assertEqual(self.run_import()['added'], 0)
        self.assertEqual(self.store.db.execute('SELECT label FROM music').fetchone()[0], 'Titre corrigé')
        self.assertEqual(self.store.db.execute('SELECT status FROM tasks WHERE id=?', (task['id'],)).fetchone()[0], 'running')
        self.assertEqual(self.store.offset(), 2)
        self.assertIn('album test', self.store.db.execute('SELECT searchable FROM music').fetchone()[0])

    def test_wrong_group_is_rejected_before_write(self):
        with self.assertRaisesRegex(ValueError, 'groupe'):
            self.run_import('-10099')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM music').fetchone()[0], 0)

    def test_invalid_message_id_rejects_whole_import(self):
        self.data['messages'].append({'type': 'message', 'id': -1, 'file_name': 'bad.mp3'})
        with self.assertRaises(ValueError):
            self.run_import()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM music').fetchone()[0], 0)

    def test_database_error_rolls_back_all_insertions(self):
        self.data['messages'].append({'type': 'message', 'id': 13, 'file_name': 'second.mp3'})
        with self.store.db:
            self.store.db.execute("CREATE TRIGGER fail_import BEFORE INSERT ON music WHEN NEW.message_id=13 BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_import()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM music').fetchone()[0], 0)

    def test_missing_database_is_not_silently_created(self):
        self.database = self.root / 'missing.sqlite3'
        with self.assertRaises(sqlite3.OperationalError):
            self.run_import()
        self.assertFalse(self.database.exists())
