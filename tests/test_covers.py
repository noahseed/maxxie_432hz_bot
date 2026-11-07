from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import covers
from catalogue import index_music
from reliability import TaskStore, TelegramError


class CoverTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = TaskStore(':memory:')
        self.addCleanup(self.store.close)
        self.api = Mock()
        self.api.path = "/botTEST/"
        self.batch = covers.CoverBatch(self.api, self.store, self.directory.name, '-10042', 99)
        self.addCleanup(self.batch.db.close)
        self.source = {'message_id': 900, 'caption': 'Ma légende', 'caption_entities': [],
                       'audio': {'performer': 'Édith Piaf', 'title': 'La vie en rose',
                                 'file_id': 'original', 'file_unique_id': 'unique', 'file_size': 100,
                                 'file_name': 'mon fichier.mp3', 'mime_type': 'audio/mpeg'}}
        index_music(self.store, '-10042', self.source, message_id=12)
        self.key = self.store.db.execute('SELECT track_key FROM music').fetchone()[0]
        self.batch.media.download = Mock(side_effect=lambda file_id, target: target.write_bytes(b'original-audio'))
        self.batch.media.edit = Mock()
        self.api.call.side_effect = lambda method, **params: self.source if method == 'forwardMessage' else {}

    def status(self):
        return self.batch.db.execute('SELECT status FROM covers WHERE message_id=12').fetchone()[0]

    def test_strict_artist_title_matching(self):
        record = {'id': 'r', 'score': 100, 'title': 'La vie en rose',
                  'artist-credit': [{'artist': {'name': 'Édith Piaf'}}]}
        self.assertEqual(covers.select_recordings({'recordings': [record]}, 'edith piaf', 'la vie en rose'), [record])
        self.assertFalse(covers.select_recordings({'recordings': [record]}, 'other artist', 'la vie en rose'))
        record['disambiguation'] = 'live'
        self.assertFalse(covers.select_recordings({'recordings': [record]}, 'edith piaf', 'la vie en rose'))

    def test_no_match_never_modifies_telegram(self):
        with patch.object(covers, 'find_cover', side_effect=covers.Skip('ambiguous')), patch.object(covers.time, 'sleep'):
            self.batch.run()
        self.assertEqual(self.status(), 'skipped')
        self.batch.media.edit.assert_not_called()

    def test_duplicate_run_does_not_repeat_done_track(self):
        self.batch.mark(12, 'done')
        with patch.object(self.batch, 'process') as process:
            self.batch.run()
            process.assert_not_called()

    def test_interrupted_upload_is_not_replayed(self):
        self.batch.mark(12, 'applying')
        with patch.object(self.batch, 'process') as process:
            self.batch.run()
            process.assert_not_called()
        self.assertEqual(self.status(), 'uncertain')

    def test_large_file_is_skipped_before_download(self):
        self.source['audio']['file_size'] = covers.MAX_AUDIO + 1
        with patch.object(covers, 'find_cover', return_value=(b'image', 'source')), \
                patch.object(covers, 'images', return_value=(b'jpeg', b'thumb')), \
                patch.object(self.batch.media, 'download') as download:
            with self.assertRaises(covers.Skip):
                self.batch.process(12, self.key)
            download.assert_not_called()
        self.assertTrue(any(c.args[0] == 'deleteMessage' for c in self.api.call.call_args_list))

    def test_success_edits_in_place_deletes_files_and_preserves_catalogue(self):
        result = dict(self.source, message_id=12, audio=dict(self.source['audio'], file_unique_id='new-unique'))
        def download(file_id, target):
            target.write_bytes(b'original-audio')
        with patch.object(covers, 'find_cover', return_value=(b'image', 'source')), \
                patch.object(covers, 'images', return_value=(b'jpeg', b'thumb')), \
                patch.object(covers, 'embed_cover') as embed, \
                patch.object(self.batch.media, 'download', side_effect=download), \
                patch.object(self.batch.media, 'edit', return_value=result) as edit:
            self.batch.process(12, self.key)
        self.assertEqual(edit.call_args.args[:2], ('-10042', 12))
        self.assertEqual(edit.call_args.args[2]['caption'], 'Ma légende')
        folder = Path(self.directory.name) / '-10042' / '12'
        self.assertFalse(folder.exists())
        saved_source = json.loads(self.batch.db.execute('SELECT source FROM covers WHERE message_id=12').fetchone()[0])
        self.assertEqual(saved_source['audio']['file_id'], 'original')
        self.assertEqual(self.status(), 'done')
        artwork = self.store.db.execute('SELECT jpeg,source_url FROM music_artwork WHERE message_id=12').fetchone()
        self.assertEqual(artwork, (b'jpeg', 'source'))
        self.assertEqual(self.store.db.execute('SELECT file_unique_id FROM music').fetchone()[0], 'new-unique')

    def test_unknown_edit_failure_is_not_retried(self):
        def uncertain(mid, key):
            self.batch.mark(mid, 'applying')
            raise TelegramError(0)
        with patch.object(self.batch, 'process', side_effect=uncertain), patch.object(covers.time, 'sleep'):
            self.batch.run()
        self.assertEqual(self.status(), 'uncertain')

    def test_image_url_allowlist(self):
        for url in ('http://musicbrainz.org/x', 'https://127.0.0.1/x', 'https://musicbrainz.org.evil.com/x',
                    'https://user:password@archive.org/x', 'https://archive.org:123/x'):
            with self.assertRaises(covers.Skip):
                covers.allowed_url(url)
        covers.allowed_url('https://ia800.us.archive.org/x')

    def test_jpeg_thumbnail_constraints(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest('Pillow absent')
        raw = BytesIO()
        Image.new('RGB', (900, 600), 'red').save(raw, format='PNG')
        embedded, thumbnail = covers.images(raw.getvalue())
        image = Image.open(BytesIO(thumbnail))
        self.assertEqual(image.format, 'JPEG')
        self.assertLessEqual(max(image.size), 320)
        self.assertLess(len(thumbnail), 200000)
        self.assertEqual(Image.open(BytesIO(embedded)).size, (600, 400))

    def test_embedding_preserves_audio_and_text_tags(self):
        try:
            from mutagen.id3 import ID3, TIT2, APIC
        except ImportError:
            self.skipTest('Mutagen absent')
        path = Path(self.directory.name) / 'sample.mp3'
        frames = (bytes.fromhex('fffb9064') + bytes(413)) * 20
        path.write_bytes(frames)
        tags = ID3()
        tags.add(TIT2(encoding=3, text='Titre original'))
        tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Old', data=b'old'))
        tags.save(path)
        covers.embed_cover(path, b'new-cover')
        tags = ID3(path)
        self.assertEqual(str(tags['TIT2']), 'Titre original')
        self.assertEqual(len(tags.getall('APIC')), 1)
        self.assertEqual(tags.getall('APIC')[0].data, b'new-cover')
        self.assertTrue(path.read_bytes().endswith(frames))

    @patch.object(covers.MediaAPI, 'connection')
    def test_multipart_preserves_caption_and_sends_mp3_plus_thumbnail(self, connection):
        response = connection.return_value.getresponse.return_value
        response.read.return_value = json.dumps({'ok': True, 'result': {'message_id': 12}}).encode()
        response.status = 200
        audio = Path(self.directory.name) / 'file.mp3'
        audio.write_bytes(b'audio-bytes')
        covers.MediaAPI.edit(self.batch.media, '-10042', 12, self.source, audio, b'thumbnail-bytes')
        args = connection.return_value.request.call_args.args
        body = args[2]
        self.assertIn(b'audio-bytes', body)
        self.assertIn(b'thumbnail-bytes', body)
        self.assertIn(b'attach://thumbnail', body)
        self.assertIn(b'mon fichier.mp3', body)

    def test_failure_cleans_audio_but_keeps_catalogue_and_artwork(self):
        folder = Path(self.directory.name) / '-10042' / '12'
        with self.store.db:
            self.store.db.execute('INSERT INTO music_artwork VALUES (?,?,?,?,?,?)',
                                  ('-10042', 12, self.key, b'jpeg', 'source', 0))
        def fail(mid, key):
            folder.mkdir(parents=True)
            (folder / 'original.mp3').write_bytes(b'audio')
            self.batch.mark(mid, 'applying')
            raise TelegramError(0)
        with patch.object(self.batch, 'process', side_effect=fail), patch.object(covers.time, 'sleep'):
            self.batch.run()
        self.assertFalse(folder.exists())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM music').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('SELECT jpeg FROM music_artwork').fetchone()[0], b'jpeg')

    def test_existing_artwork_is_bypassed_and_preserved_if_search_fails(self):
        with self.store.db:
            self.store.db.execute('INSERT INTO music_artwork VALUES (?,?,?,?,?,?)',
                                  ('-10042', 12, self.key, b'old-jpeg', 'old-source', 0))
        with patch.object(covers, 'find_cover', side_effect=covers.Skip('not found')) as find,                 patch.object(covers.time, 'sleep'):
            self.batch.run()
            find.assert_called_once_with('Édith Piaf', 'La vie en rose', album='')
        self.assertEqual(self.status(), 'skipped')
        self.assertEqual(self.store.db.execute('SELECT jpeg FROM music_artwork').fetchone()[0], b'old-jpeg')
        self.assertFalse((Path(self.directory.name) / '-10042' / '12').exists())

    def test_previous_revision_done_tracks_are_reprocessed(self):
        self.batch.mark(12, 'done')
        with self.batch.db:
            self.batch.db.execute("UPDATE covers SET revision='old'")
        def done(mid, key):
            self.batch.mark(mid, 'done')
        with patch.object(self.batch, 'process', side_effect=done) as process, patch.object(covers.time, 'sleep'):
            self.batch.run()
            process.assert_called_once_with(12, self.key)
        self.assertEqual(self.batch.db.execute('SELECT revision FROM covers').fetchone()[0], covers.SEARCH_REVISION)

    def test_tuning_marker_is_removed_from_search_and_comparison(self):
        for value in ('Song [432Hz]', 'Song [432 Hz]', 'Song [432hz]', 'Song (432 HZ)'):
            self.assertEqual(covers.without_tuning(value), 'Song')
        with patch.object(covers, 'fetch', return_value=b'{"recordings":[]}') as fetch, patch.object(covers.time, 'sleep'):
            with self.assertRaises(covers.Skip):
                covers.find_cover('Artist [432Hz]', 'Song [432 Hz]', 'Album [432hz]')
            from urllib.parse import unquote_plus
            self.assertNotIn('432', unquote_plus(fetch.call_args.args[0]))

    def test_album_matching_ignores_432_marker_and_rejects_other_albums(self):
        recording = {'releases': [
            {'id': 'correct', 'title': 'Mon album', 'status': 'Official'},
            {'id': 'wrong', 'title': 'Autre compilation', 'status': 'Official'}]}
        self.assertEqual([item['id'] for item in covers.release_candidates([recording], 'Mon album [432Hz]')], ['correct'])

    def test_transient_errors_stop_after_three_attempts(self):
        with patch.object(self.batch, 'process', side_effect=covers.Retry('HTTP 503')) as process, \
                patch.object(covers.time, 'sleep'):
            for _ in range(4):
                with self.batch.db:
                    self.batch.db.execute('UPDATE covers SET next_retry=0')
                self.batch.run()
        self.assertEqual(process.call_count, 3)
        self.assertEqual(self.status(), 'exhausted')
        self.assertEqual(self.batch.db.execute('SELECT attempts FROM covers').fetchone()[0], 3)

    def test_unseen_tracks_take_priority_over_failed_tracks(self):
        self.batch.begin_attempt(12)
        self.batch.mark(12, 'failed')
        self.assertEqual(self.batch.pending([(12, self.key), (13, self.key)]), [(13, self.key), (12, self.key)])

    def test_failed_track_waits_before_retry(self):
        self.batch.begin_attempt(12)
        self.batch.mark(12, 'failed')
        self.batch.schedule_retry(12)
        self.assertEqual(self.batch.pending([(12, self.key)]), [])
        self.assertGreater(self.batch.db.execute('SELECT next_retry FROM covers').fetchone()[0], covers.time.time())

    def test_status_shows_real_cover_progress(self):
        from bot import Bot
        self.batch.mark(12, 'done')
        bot = Bot(self.api, 'bot', '-10042', 'https://t.me/test', 'rules', owner_user_id=99, welcome='Accueil')
        bot.background = self.store
        with patch.dict('os.environ', {'COVERS_DIR': self.directory.name}):
            bot.show_status(99, 99)
        self.assertIn('Jaquettes : 1/1 morceaux examinés', self.api.call.call_args.kwargs['text'])
        self.assertIn('Remplacées : 1', self.api.call.call_args.kwargs['text'])

    def test_three_source_errors_pause_then_continue_remaining_tracks(self):
        for mid in (13, 14, 15):
            index_music(self.store, '-10042', dict(self.source, message_id=mid))
        with patch.object(self.batch, 'process', side_effect=covers.Retry('HTTP 503')) as process, \
                patch.object(covers.time, 'sleep'), patch.object(covers.STOP, 'wait', return_value=False) as wait:
            self.batch.run()
        self.assertEqual(process.call_count, 4)
        self.assertEqual([call.args[0] for call in process.call_args_list], [12, 13, 14, 15])
        wait.assert_called_once_with(60)
        self.assertEqual(self.batch.db.execute("SELECT count(*) FROM covers WHERE status='failed'").fetchone()[0], 4)
        self.assertIn('4/4 morceaux examinés', self.api.call.call_args.kwargs['text'])
