import json
import unittest
from unittest.mock import patch
import covers


class CoverProvidersTests(unittest.TestCase):
    def test_deezer_checks_metadata_and_ignores_tuning(self):
        def track(artist, title, album='Album'):
            return {'artist': {'name': artist}, 'title': title,
                    'album': {'title': album, 'id': 123, 'cover_xl': 'https://cdn-images.dzcdn.net/cover.jpg'}}
        data = {'data': [track('Other', 'Song'), track('Artist', 'Song (Remix)'),
                         track('Artist', 'Song', 'Other'), track('Artist', 'Song')]}
        with patch.object(covers, 'fetch', side_effect=[json.dumps(data).encode(), b'image']) as fetch, patch.object(covers, 'images'):
            self.assertEqual(covers.find_deezer_cover('Artist [432Hz]', 'Song', 'Album'),
                             (b'image', 'https://www.deezer.com/album/123'))
            self.assertNotIn('432', fetch.call_args_list[0].args[0])
            self.assertEqual(fetch.call_count, 2)

    def test_provider_outage_falls_back_and_restores_deadline(self):
        token = covers.FETCH_DEADLINE.set(covers.time.monotonic() + 12)
        deadline = covers.FETCH_DEADLINE.get()
        try:
            with patch.object(covers, 'find_deezer_cover', side_effect=covers.Retry('offline')), patch.object(covers, 'find_musicbrainz_cover', return_value=(b'image', 'source')):
                self.assertEqual(covers.find_cover('Artist', 'Song'), (b'image', 'source'))
            self.assertEqual(covers.FETCH_DEADLINE.get(), deadline)
        finally:
            covers.FETCH_DEADLINE.reset(token)

    def test_image_host_allowlist(self):
        covers.allowed_url('https://cdn-images.dzcdn.net/cover.jpg')
        with self.assertRaises(covers.Skip):
            covers.allowed_url('https://cdn-images.dzcdn.net.evil.example/cover.jpg')

    def test_relaxes_album_but_rejects_live_remix_and_other_artist(self):
        def track(artist, title, album, album_id):
            return {'artist': {'name': artist}, 'title': title,
                    'album': {'title': album, 'id': album_id, 'cover_xl': f'https://cdn-images.dzcdn.net/{album_id}.jpg'}}
        data = {'data': [track('Other', 'Song', 'Original', 1),
                         track('Artist', 'Song (Remix)', 'Original', 2),
                         track('Artist', 'Song', 'Live at Paris', 3),
                         track('Artist', 'Song', 'Greatest Hits', 4),
                         track('Artist', 'Song', 'Another Album', 5)]}
        with patch.object(covers, 'fetch', side_effect=[json.dumps(data).encode(), b'image']) as fetch, patch.object(covers, 'images'):
            self.assertEqual(covers.find_deezer_cover('Artist', 'Song', 'Original')[1], 'https://www.deezer.com/album/5')
            self.assertEqual(fetch.call_args.args[0], 'https://cdn-images.dzcdn.net/5.jpg')
