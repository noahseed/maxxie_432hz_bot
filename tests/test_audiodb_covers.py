import unittest
from unittest.mock import patch
import audiodb_covers as provider
import covers


class AudioDBTests(unittest.TestCase):
    def test_track_and_album_artist_must_match(self):
        tracks = {'track': [
            {'strArtist': 'Other', 'strTrack': 'Song', 'idAlbum': '1'},
            {'strArtist': 'Artist', 'strTrack': 'Song (Remix)', 'idAlbum': '2'},
            {'strArtist': 'Artist', 'strTrack': 'Song', 'idAlbum': '3'}]}
        albums = {'album': [
            {'strArtist': 'Other', 'idAlbum': '3', 'strAlbumThumb': 'https://www.theaudiodb.com/wrong'},
            {'strArtist': 'Artist', 'idAlbum': '3', 'strAlbumThumb': 'https://www.theaudiodb.com/right'}]}
        with patch.object(provider, 'lookup', side_effect=[tracks, albums]) as lookup, patch.object(covers, 'fetch', return_value=b'image') as fetch, patch.object(covers, 'images'):
            self.assertEqual(provider.find_cover('Artist [432Hz]', 'Song')[1], 'https://www.theaudiodb.com/album/3')
            self.assertEqual(lookup.call_args_list[0].args[1], {'s': 'Artist', 't': 'Song'})
            fetch.assert_called_once_with('https://www.theaudiodb.com/right', limit=8_000_000)

    def test_null_results_are_not_an_error(self):
        with patch.object(provider, 'lookup', return_value={'track': None}):
            with self.assertRaises(covers.Skip):
                provider.find_cover('Artist', 'Song')

    def test_api_and_image_hosts_are_restricted(self):
        covers.allowed_url('https://www.theaudiodb.com/images/cover.jpg')
        covers.allowed_url('https://r2.theaudiodb.com/images/cover.jpg')
        with self.assertRaises(covers.Skip):
            covers.allowed_url('https://www.theaudiodb.com.evil.test/cover.jpg')
