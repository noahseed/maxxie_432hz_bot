from io import BytesIO
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from catalogue import index_music
from covers import Skip
from random_artwork import RandomArtwork
from reliability import TaskStore


class RandomArtworkTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = TaskStore(':memory:')
        self.addCleanup(self.store.close)
        self.api = Mock()
        self.source = {'message_id': 1, 'audio': {'file_id': 'audio', 'file_unique_id': 'unique',
                       'mime_type': 'audio/mpeg', 'performer': 'Édith Piaf', 'title': 'La vie en rose [432Hz]', 'file_size': 100}}
        index_music(self.store, '-10042', self.source)
        self.resolver = RandomArtwork(self.api, self.store, '-10042', 99, self.folder.name)

    @patch('random_artwork.images', return_value=(b'online-jpeg', b'thumb'))
    @patch('random_artwork.find_cover', return_value=(b'image', 'https://source'))
    def test_internet_image_has_priority_over_existing_image(self, find, images):
        with self.store.db:
            key = self.store.db.execute('SELECT track_key FROM music').fetchone()[0]
            self.store.db.execute('INSERT INTO music_artwork VALUES (?,?,?,?,?,?)', ('-10042', 1, key, b'old', 'old', 0))
        with patch.object(self.resolver, 'embedded') as embedded:
            self.assertEqual(self.resolver.resolve(1), b'online-jpeg')
            embedded.assert_not_called()
        find.assert_called_once_with('Édith Piaf', 'La vie en rose [432Hz]', album='')
        self.assertEqual(self.store.db.execute('SELECT jpeg FROM music_artwork').fetchone()[0], b'online-jpeg')

    @patch('random_artwork.find_cover', side_effect=Skip('no match'))
    def test_no_online_match_uses_mp3_artwork(self, find):
        with patch.object(self.resolver, 'embedded', return_value=(b'embedded-jpeg', 'embedded:telegram')) as embedded:
            self.assertEqual(self.resolver.resolve(1), b'embedded-jpeg')
            embedded.assert_called_once()
        self.assertEqual(self.store.db.execute('SELECT source_url FROM music_artwork').fetchone()[0], 'embedded:telegram')

    @patch('random_artwork.find_cover', side_effect=TimeoutError)
    def test_online_outage_also_falls_back_to_mp3(self, find):
        with patch.object(self.resolver, 'embedded', return_value=(b'embedded-jpeg', 'embedded:telegram')):
            self.assertEqual(self.resolver.resolve(1), b'embedded-jpeg')

    @patch('random_artwork.find_cover', side_effect=Skip('no match'))
    def test_no_artwork_returns_none_without_fake_image(self, find):
        with patch.object(self.resolver, 'embedded', return_value=None):
            self.assertIsNone(self.resolver.resolve(1))

    def test_embedded_apic_is_extracted_and_mp3_is_deleted(self):
        try:
            from PIL import Image
            from mutagen.id3 import ID3, APIC
        except ImportError:
            self.skipTest('Optional cover dependencies absent')
        image = BytesIO()
        Image.new('RGB', (400, 400), 'blue').save(image, format='JPEG')
        files = []
        def download(file_id, target, **kwargs):
            files.append(target)
            target.write_bytes((bytes.fromhex('fffb9064') + bytes(413)) * 20)
            tags = ID3()
            tags.add(APIC(encoding=3, mime='image/jpeg', type=3, data=image.getvalue()))
            tags.save(target)
        with patch.object(self.resolver.media, 'download', side_effect=download):
            jpeg, origin = self.resolver.embedded(1, self.source, time.monotonic() + 30)
        self.assertEqual(origin, 'embedded:telegram')
        self.assertEqual(Image.open(BytesIO(jpeg)).size, (400, 400))
        self.assertTrue(files)
        self.assertTrue(all(not file.exists() for file in files))

    def test_large_mp3_can_use_telegram_thumbnail_without_downloading_mp3(self):
        self.source['audio'].update(file_size=30_000_000, thumbnail={'file_id': 'thumb'})
        def download(file_id, target, **kwargs):
            self.assertEqual(file_id, 'thumb')
            target.write_bytes(b'image')
        with patch.object(self.resolver.media, 'download', side_effect=download) as download, \
                patch('random_artwork.images', return_value=(b'jpeg', b'thumb')):
            self.assertEqual(self.resolver.embedded(1, self.source, time.monotonic() + 30), (b'jpeg', 'thumbnail:telegram'))
            self.assertEqual(download.call_count, 1)

    def test_new_indexing_retains_file_reference_without_private_profile(self):
        source = json.loads(self.store.db.execute('SELECT data FROM music_media').fetchone()[0])
        self.assertEqual(source['audio']['file_id'], 'audio')
        self.assertNotIn('from', source)
        self.assertNotIn('chat', source)

    def test_reference_recovery_uses_only_owner_then_deletes_temporary_forward(self):
        self.api.call.side_effect = [dict(self.source, message_id=55), True]
        self.resolver.recover_source(1)
        self.assertEqual(self.api.call.call_args_list[0].kwargs,
                         {'chat_id': 99, 'from_chat_id': '-10042', 'message_id': 1, 'disable_notification': True})
        self.assertEqual(self.api.call.call_args_list[1].kwargs, {'chat_id': 99, 'message_id': 55})

    def test_unknown_message_never_calls_internet_or_telegram(self):
        with patch('random_artwork.find_cover') as find:
            self.assertIsNone(self.resolver.resolve(99))
            find.assert_not_called()
        self.api.call.assert_not_called()
