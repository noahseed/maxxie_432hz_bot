"""Passage TheAudioDB limité aux pochettes absentes, avec les mêmes garanties Telegram."""
import json
import threading
import time
from urllib.parse import urlencode
import covers

_API_LOCK = threading.Lock()
_NEXT_REQUEST = 0.0


def lookup(endpoint, params):
    global _NEXT_REQUEST
    # Free tier: at most 30 API requests/minute, including album lookups.
    with _API_LOCK:
        time.sleep(max(0, _NEXT_REQUEST - time.monotonic()))
        _NEXT_REQUEST = time.monotonic() + 2.2
        raw = covers.fetch('https://www.theaudiodb.com/api/v1/json/123/' + endpoint + '?' + urlencode(params))
    return json.loads(raw) if raw else {}


def find_cover(artist, title, album=''):
    artist, title, album = (covers.without_tuning(value) for value in (artist, title, album))
    data = lookup('searchtrack.php', {'s': artist, 't': title})
    tracks = data.get('track') or []
    tracks = sorted(tracks, key=lambda t: 0 if album and covers.cover_key(t.get('strAlbum') or '') == covers.cover_key(album) else 1)
    for track in tracks:
        if (covers.cover_key(track.get('strArtist') or '') != covers.cover_key(artist)
                or covers.cover_key(track.get('strTrack') or '') != covers.cover_key(title)):
            continue
        album_id = str(track.get('idAlbum') or '')
        if not album_id.isdigit():
            continue
        release_data = lookup('album.php', {'m': album_id})
        for release in release_data.get('album') or []:
            if (str(release.get('idAlbum')) != album_id
                    or covers.cover_key(release.get('strArtist') or '') != covers.cover_key(artist)):
                continue
            url = release.get('strAlbumThumbHQ') or release.get('strAlbumThumb')
            if not url:
                continue
            raw = covers.fetch(url, limit=8_000_000)
            if raw:
                covers.images(raw)
                return raw, 'https://www.theaudiodb.com/album/' + album_id
    raise covers.Skip('Aucune pochette TheAudioDB correspondant à l’artiste et au titre.')


def missing_pending(self, rows):
    existing = {r[0] for r in self.catalogue.db.execute('SELECT message_id FROM music_artwork WHERE group_id=?', (self.destination,))}
    uncertain = {r[0] for r in self.db.execute("SELECT message_id FROM covers WHERE group_id=? AND status='uncertain'", (self.destination,))}
    selected = [(mid, key) for mid, key in rows if mid not in existing and mid not in uncertain]
    with self.db:
        for mid, key in selected:
            self.db.execute('UPDATE covers SET attempts=0,next_retry=0 WHERE group_id=? AND message_id=?', (self.destination, mid))
    return selected


if __name__ == '__main__':
    covers.find_cover = find_cover
    covers.CoverBatch.pending = missing_pending
    covers.main()
