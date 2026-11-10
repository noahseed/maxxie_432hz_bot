"""Hasard : internet d'abord, pochette du MP3 en repli, aucun MP3 conservé."""
import json
import os
import logging
from pathlib import Path
import sqlite3
import tempfile
import threading
import time

from database import connect
from covers import FETCH_DEADLINE, MAX_AUDIO, MediaAPI, Skip, find_cover, images

LOG = logging.getLogger('maxxie')
SEARCH_LOCK = threading.Lock()


class RandomArtwork:
    def __init__(self, api, store, destination, owner, directory):
        self.api, self.store, self.destination = api, store, str(destination)
        self.owner, self.directory = owner, Path(directory)
        self.media = MediaAPI(api)

    def cached_source(self, mid, unique):
        with self.store.lock:
            row = self.store.db.execute('SELECT data FROM music_media WHERE group_id=? AND message_id=?',
                                        (self.destination, mid)).fetchone()
        if row:
            source = json.loads(row[0])
            media = source.get('audio') or source.get('document', {})
            if media.get('file_id') and (not unique or media.get('file_unique_id') == unique):
                return source
        path = self.directory / 'covers.sqlite3'
        url = os.environ.get('DATABASE_URL', '')
        if not url and not path.exists():
            return None
        db = connect(url or path.resolve().as_uri() + '?mode=ro', schema='covers', readonly=True, uri=True, timeout=1)
        try:
            row = db.execute('SELECT source FROM covers WHERE group_id=? AND message_id=?',
                             (self.destination, mid)).fetchone()
        finally:
            db.close()
        if row and row[0]:
            source = json.loads(row[0])
            media = source.get('audio') or source.get('document', {})
            if media.get('file_id') and (not unique or media.get('file_unique_id') == unique):
                self.save_source(mid, source)
                return source
        return None

    def save_source(self, mid, source):
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT INTO music_media VALUES (?,?,?,?) ON CONFLICT(group_id,message_id) DO UPDATE SET data=excluded.data,updated=excluded.updated',
                                  (self.destination, mid, json.dumps(source), time.time()))

    def recover_source(self, mid):
        if not self.owner:
            return None
        # For old imports only, recover the current reference through a silent temporary forward.
        forwarded = self.api.call('forwardMessage', chat_id=self.owner, from_chat_id=self.destination,
                                  message_id=mid, disable_notification=True)
        temporary = forwarded['message_id']
        try:
            source = {key: forwarded[key] for key in ('audio', 'document') if key in forwarded}
            self.save_source(mid, source)
            return source
        finally:
            try:
                self.api.call('deleteMessage', chat_id=self.owner, message_id=temporary)
            except Exception:
                LOG.warning('Suppression du transfert temporaire de jaquette indisponible.')

    def save_image(self, mid, key, jpeg, origin):
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT INTO music_artwork VALUES (?,?,?,?,?,?) ON CONFLICT(group_id,message_id) DO UPDATE SET track_key=excluded.track_key,jpeg=excluded.jpeg,source_url=excluded.source_url,updated=excluded.updated',
                                  (self.destination, mid, key, jpeg, origin, time.time()))
        return jpeg

    def embedded(self, mid, source, deadline):
        from mutagen.id3 import ID3, ID3NoHeaderError
        media = source.get('audio') or source.get('document', {})
        with tempfile.TemporaryDirectory(prefix='maxxie-random-cover-') as folder:
            path = Path(folder) / 'track.mp3'
            if media.get('file_id') and media.get('file_size', 0) <= MAX_AUDIO:
                try:
                    self.media.download(media['file_id'], path, deadline=deadline)
                    try:
                        tags = ID3(path)
                    except ID3NoHeaderError:
                        tags = None
                    if tags:
                        if tags.get('TALB'):
                            source['_album'] = str(tags['TALB'])
                            self.save_source(mid, source)
                        pictures = sorted(tags.getall('APIC'), key=lambda pic: pic.type != 3)
                        for picture in pictures:
                            try:
                                return images(picture.data)[0], 'embedded:telegram'
                            except Exception:
                                continue
                except Exception:
                    LOG.info('Pochette intégrée non récupérable pour ce tirage.')
            # Telegram's audio thumbnail is useful even for files exceeding getFile's MP3 limit.
            thumbnail = media.get('thumbnail', {})
            if thumbnail.get('file_id') and time.monotonic() < deadline:
                self.media.download(thumbnail['file_id'], path, deadline=deadline)
                return images(path.read_bytes())[0], 'thumbnail:telegram'
        return None

    def resolve(self, mid):
        deadline = time.monotonic() + 30
        try:
            with self.store.lock:
                row = self.store.db.execute('SELECT artist,track_key,file_unique_id FROM music WHERE group_id=? AND message_id=?',
                                            (self.destination, mid)).fetchone()
            if not row:
                return None
            with self.store.lock:
                manual = self.store.db.execute("SELECT jpeg FROM music_artwork WHERE group_id=? AND message_id=? AND source_url LIKE 'manual:%'", (self.destination,mid)).fetchone()
            if manual:
                return bytes(manual[0])
            artist, key, unique = row
            try:
                source = self.cached_source(mid, unique)
            except Exception:
                source = None
            media = (source.get('audio') or source.get('document', {})) if source else {}
            try:
                _, title = json.loads(key)
            except (ValueError, TypeError):
                title = ''
            artist, title = media.get('performer') or artist, media.get('title') or title
            if artist and title:
                locked = SEARCH_LOCK.acquire(timeout=1)
                if locked:
                    token = FETCH_DEADLINE.set(min(deadline, time.monotonic() + 12))
                    try:
                        raw, origin = find_cover(artist, title, album=source.get('_album', '') if source else '')
                        jpeg = images(raw)[0]
                        return self.save_image(mid, key, jpeg, origin)
                    except Exception:
                        LOG.info('Recherche internet sans jaquette exploitable ; repli sur le MP3.')
                    finally:
                        FETCH_DEADLINE.reset(token)
                        SEARCH_LOCK.release()
            if source is None:
                source = self.recover_source(mid)
            if source:
                result = self.embedded(mid, source, deadline)
                if result:
                    jpeg, origin = result
                    return self.save_image(mid, key, jpeg, origin)
        except Exception as error:
            # Never log token-bearing paths or remote error bodies.
            LOG.warning('Jaquette du tirage indisponible (%s).', type(error).__name__)
        return None
