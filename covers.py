"""Pochettes automatiques : édition en place, fichiers temporaires, rapport privé."""
import fcntl
from contextvars import ContextVar
import hashlib
from http.client import HTTPSConnection
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urljoin
from urllib.request import Request, build_opener, HTTPRedirectHandler
import uuid

from bot import Telegram, connect_ipv4
from catalogue import index_music, normalize, track_identity
from reliability import TelegramError
from database import connect, target, is_postgres

FETCH_DEADLINE = ContextVar('cover_fetch_deadline', default=None)
MAX_AUDIO = 20 * 1024 * 1024
SEARCH_REVISION = 'album-aware-no-432-v2'
MAX_ATTEMPTS = 3
STOP = threading.Event()
USER_AGENT = 'Maxxie432HzBot/1.0 (https://github.com/noahseed/maxxie_432hz_bot)'


class Skip(Exception):
    pass


class Retry(Skip):
    pass


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = urljoin(req.full_url, newurl)
        if newurl.startswith('http:'):
            newurl = 'https:' + newurl[5:]
        allowed_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def allowed_url(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    if (parsed.scheme != 'https' or parsed.port not in (None, 443) or parsed.username or parsed.password
            or not (host in ('musicbrainz.org', 'coverartarchive.org', 'archive.org', 'api.deezer.com', 'cdn-images.dzcdn.net', 'cdns-images.dzcdn.net', 'e-cdns-images.dzcdn.net') or host.endswith('.archive.org'))):
        raise Skip('Hôte de pochette non autorisé.')


def fetch(url, limit=2_000_000):
    allowed_url(url)
    remaining = FETCH_DEADLINE.get()
    if remaining is not None:
        remaining -= time.monotonic()
        if remaining <= 0:
            raise Retry('Délai de recherche de jaquette dépassé.')
    request = Request(url, headers={'User-Agent': USER_AGENT})
    try:
        with build_opener(SafeRedirect()).open(request, timeout=min(20, remaining) if remaining is not None else 20) as response:
            data = response.read(limit + 1)
            if len(data) > limit:
                raise Skip('Image ou réponse trop volumineuse.')
            return data
    except HTTPError as error:
        if error.code in (404, 400):
            return None
        raise Retry(f'Service de pochettes indisponible (HTTP {error.code}).') from None
    except (URLError, OSError, TimeoutError):
        raise Retry('Service de pochettes temporairement inaccessible.') from None


def quote_query(value):
    return re.sub(r'([+\-!(){}\[\]^"~*?:\\/])', r'\\\1', value)


def without_tuning(value):
    return re.sub(r'[\[(]\s*432\s*hz\s*[\])]', '', value, flags=re.IGNORECASE).strip()


def cover_key(value):
    return normalize(without_tuning(value))


def release_candidates(recordings, album=''):
    releases = {}
    for recording in recordings:
        for release in recording.get('releases', []):
            group = release.get('release-group', {})
            secondary = group.get('secondary-types', [])
            if release.get('status', 'Official') != 'Official':
                continue
            if album and cover_key(release.get('title', '')) != cover_key(album):
                continue
            if not album and any(kind in secondary for kind in ('Compilation', 'Live', 'Remix')):
                continue
            if release.get('id'):
                releases[release['id']] = release
    return sorted(releases.values(), key=lambda release: (
        0 if release.get('release-group', {}).get('primary-type') in ('Album', 'Single') else 1,
        release.get('date') or '9999', release.get('id', '')))


def select_recordings(data, artist, title):
    matches = []
    for recording in data.get('recordings', []):
        names = [credit.get('artist', {}).get('name', credit.get('name', ''))
                 for credit in recording.get('artist-credit', []) if isinstance(credit, dict)]
        if (cover_key(recording.get('title', '')) == cover_key(title)
                and cover_key(artist) in [cover_key(name) for name in names]
                and not recording.get('disambiguation')
                and int(recording.get('score', 0)) >= 95):
            matches.append(recording)
    return matches


def find_deezer_cover(artist, title, album=""):
    artist, title, album = (without_tuning(value) for value in (artist, title, album))
    # Plain text search also works when Deezer's structured track query returns nothing.
    raw = fetch('https://api.deezer.com/search?' + urlencode({'q': f'{artist} {title}', 'limit': 25}))
    data = json.loads(raw) if raw else {}
    if data.get('error'):
        raise Retry('Recherche Deezer temporairement indisponible.')
    tracks = sorted(data.get('data', []), key=lambda track:
                    0 if album and cover_key((track.get('album') or {}).get('title', '')) == cover_key(album) else 1)
    for track in tracks:
        release = track.get('album') or {}
        if (cover_key(track.get('artist', {}).get('name', '')) != cover_key(artist)
                or cover_key(track.get('title', '')) != cover_key(title)):
            continue
        exact_album = album and cover_key(release.get('title', '')) == cover_key(album)
        if not exact_album and re.search(r'\b(live|remix|karaoke|tribute|compilation|best of|greatest hits)\b',
                                         cover_key(release.get('title', ''))):
            continue
        url = release.get('cover_xl') or release.get('cover_big')
        album_id = str(release.get('id', ''))
        if not url or not album_id.isdigit():
            continue
        raw = fetch(url, limit=8_000_000)
        if raw:
            images(raw)  # Reject invalid images before trying another provider.
            return raw, f'https://www.deezer.com/album/{album_id}'
    raise Skip('Aucune pochette Deezer correspondant aux métadonnées.')


def find_cover(artist, title, album=""):
    deadline = FETCH_DEADLINE.get()
    transient = False
    for provider in (find_deezer_cover, find_musicbrainz_cover):
        # Reserve time for the second source within Hasard's short search budget.
        remaining = deadline - time.monotonic() if deadline is not None else 30
        if remaining <= 0:
            raise Retry('Délai de recherche de jaquette dépassé.')
        budget = min(5, remaining / 2) if provider is find_deezer_cover else remaining
        token = FETCH_DEADLINE.set(time.monotonic() + budget)
        try:
            return provider(artist, title, album)
        except Retry:
            transient = True
        except (Skip, ValueError, TypeError, KeyError, OSError):
            pass
        finally:
            FETCH_DEADLINE.reset(token)
    if transient:
        raise Retry('Services de pochettes temporairement indisponibles.')
    raise Skip('Aucune pochette correspondant aux métadonnées disponible.')


def find_musicbrainz_cover(artist, title, album=""):
    artist, title, album = (without_tuning(value) for value in (artist, title, album))
    query = f'recording:"{quote_query(title)}" AND artist:"{quote_query(artist)}"'
    if album:
        query += f' AND release:"{quote_query(album)}"'
    time.sleep(1.1)  # MusicBrainz asks for at most one request per second.
    data = fetch('https://musicbrainz.org/ws/2/recording/?' + urlencode({'query': query, 'fmt': 'json', 'limit': 25}))
    if data is None:
        raise Skip('Aucune correspondance MusicBrainz.')
    matches = select_recordings(json.loads(data), artist, title)
    if not matches:
        raise Skip('Artiste/titre non identifiés avec certitude.')
    releases = release_candidates(matches, album)
    if not releases:
        raise Skip('Aucune édition correspondant à l’album du MP3.' if album else 'Aucune édition officielle identifiée.')
    for release in releases[:5]:
        mbid = release.get('id', '')
        if not re.fullmatch(r'[0-9a-f-]{36}', mbid):
            continue
        raw = fetch(f'https://coverartarchive.org/release/{mbid}/')
        if not raw:
            continue
        for image in json.loads(raw).get('images', []):
            if image.get('front') and image.get('approved'):
                url = image.get('thumbnails', {}).get('500') or image.get('image', '')
                if url.startswith('http:'):
                    url = 'https:' + url[5:]
                image_data = fetch(url, limit=8_000_000)
                if image_data:
                    return image_data, f'https://musicbrainz.org/release/{mbid}'
    raise Skip('Aucune pochette de face vérifiée disponible.')


def images(raw):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 20_000_000
    with Image.open(BytesIO(raw)) as image:
        image = ImageOps.exif_transpose(image).convert('RGB')
        image.thumbnail((600, 600))
        cover = BytesIO()
        image.save(cover, format='JPEG', quality=85)
        if len(cover.getvalue()) > 500_000:
            raise Skip('Jaquette compressée trop volumineuse.')
        image.thumbnail((320, 320))
        thumbnail = BytesIO()
        image.save(thumbnail, format='JPEG', quality=85)
    if len(thumbnail.getvalue()) >= 200_000:
        raise Skip('Miniature trop volumineuse.')
    return cover.getvalue(), thumbnail.getvalue()


def embed_cover(path, data):
    from mutagen.id3 import APIC, ID3, ID3NoHeaderError
    from mutagen.mp3 import MP3
    before = MP3(path).info
    audio_identity = (before.length, before.sample_rate, before.channels)
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    tags.delall('APIC')
    tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=data))
    tags.save(path)
    after = MP3(path).info
    if audio_identity != (after.length, after.sample_rate, after.channels):
        raise Skip('Contrôle audio après intégration en échec.')


class MediaAPI:
    def __init__(self, api):
        self.api = api

    def connection(self):
        connection = HTTPSConnection('api.telegram.org', timeout=90)
        if self.api.ipv4_only:
            connection._create_connection = connect_ipv4
        return connection

    def download(self, file_id, target, deadline=None):
        info = self.api.call('getFile', file_id=file_id)
        path = info.get('file_path', '')
        if not re.fullmatch(r'[A-Za-z0-9_./-]+', path) or '..' in path or info.get('file_size', 0) > MAX_AUDIO:
            raise Skip('Fichier inaccessible ou supérieur à la limite de téléchargement.')
        if deadline is not None and time.monotonic() >= deadline:
            raise Skip('Délai de récupération de la jaquette dépassé.')
        connection = self.connection()
        if deadline is not None:
            connection.timeout = max(0.1, deadline - time.monotonic())
        try:
            # Fixed Telegram host; never expose the token in a URL or exception.
            connection.request('GET', '/file' + self.api.path + path)
            response = connection.getresponse()
            if response.status != 200:
                raise Skip('Téléchargement Telegram refusé.')
            total = 0
            with target.open('wb') as stream:
                while True:
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise Skip('Délai de récupération de la jaquette dépassé.')
                        if connection.sock is not None:
                            connection.sock.settimeout(min(90, remaining))
                    block = response.read(65536)
                    if not block:
                        break
                    total += len(block)
                    if total > MAX_AUDIO:
                        raise Skip('MP3 supérieur à 20 Mio.')
                    stream.write(block)
        finally:
            connection.close()

    def edit(self, destination, mid, source, audio, thumbnail):
        media = source.get('audio') or source.get('document')
        kind = 'audio' if source.get('audio') else 'document'
        payload = {'type': kind, 'media': 'attach://audio', 'thumbnail': 'attach://thumbnail',
                   'caption': source.get('caption', ''), 'caption_entities': source.get('caption_entities', [])}
        if kind == 'audio':
            payload.update({key: media[key] for key in ('duration', 'performer', 'title') if key in media})
        boundary = uuid.uuid4().hex
        body = bytearray()
        for key, value in {'chat_id': str(destination), 'message_id': str(mid), 'media': json.dumps(payload)}.items():
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        # Keep original filename while stripping multipart control characters.
        filename = re.sub(r'[^\w. -]', '_', Path(media.get('file_name') or f'{mid}.mp3').name)[:180]
        if not filename.lower().endswith('.mp3'):
            filename += '.mp3'
        for key, name, mime, data in (('audio', filename, 'audio/mpeg', audio.read_bytes()),
                                       ('thumbnail', 'cover.jpg', 'image/jpeg', thumbnail)):
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{name}"\r\nContent-Type: {mime}\r\n\r\n'.encode())
            body.extend(data)
            body.extend(b'\r\n')
        body.extend(f'--{boundary}--\r\n'.encode())
        connection = self.connection()
        try:
            connection.request('POST', self.api.path + 'editMessageMedia', body,
                               {'Content-Type': f'multipart/form-data; boundary={boundary}'})
            response = connection.getresponse()
            result = json.loads(response.read())
            if not result.get('ok'):
                raise TelegramError(result.get('error_code', response.status), result.get('parameters', {}).get('retry_after', 5))
            return result['result']
        finally:
            connection.close()


class CoverBatch:
    def __init__(self, api, catalogue, directory, destination, owner, database_url=None):
        self.api, self.catalogue = api, catalogue
        self.directory, self.destination, self.owner = Path(directory), str(destination), owner
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        cover_target = database_url or os.environ.get('DATABASE_URL') or self.directory / 'covers.sqlite3'
        self.db = connect(cover_target, schema='covers')
        self.db.execute('CREATE TABLE IF NOT EXISTS covers (group_id TEXT, message_id INTEGER, status TEXT, detail TEXT, source TEXT, updated REAL, PRIMARY KEY(group_id,message_id))')
        if 'revision' not in {row[1] for row in self.db.execute('PRAGMA table_info(covers)')}:
            self.db.execute("ALTER TABLE covers ADD COLUMN revision TEXT NOT NULL DEFAULT ''")
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(covers)')}
        for column in ('attempts', 'next_retry'):
            if column not in columns:
                datatype = 'REAL' if column == 'next_retry' else 'INTEGER'
                self.db.execute(f'ALTER TABLE covers ADD COLUMN {column} {datatype} NOT NULL DEFAULT 0')
        if is_postgres(cover_target):
            types = {row[1]: row[2] for row in self.db.execute('PRAGMA table_info(covers)')}
            if types.get('next_retry') != 'double precision':
                self.db.execute('ALTER TABLE covers ALTER COLUMN next_retry TYPE DOUBLE PRECISION USING next_retry::double precision')
        self.db.execute('CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY, text TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS temporary_messages (message_id INTEGER PRIMARY KEY)')
        self.db.commit()
        with self.catalogue.lock, self.catalogue.db:
            self.catalogue.db.execute('CREATE TABLE IF NOT EXISTS music_media (group_id TEXT, message_id INTEGER, data TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(group_id,message_id))')
            self.catalogue.db.execute('CREATE TABLE IF NOT EXISTS music_artwork (group_id TEXT, message_id INTEGER, track_key TEXT NOT NULL, jpeg BLOB NOT NULL, source_url TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(group_id,message_id))')
            self.catalogue.db.execute('CREATE INDEX IF NOT EXISTS artwork_track ON music_artwork(group_id,track_key)')
        self.media = MediaAPI(api)

    def notify(self, text):
        with self.db:
            self.db.execute('INSERT INTO notifications(text) VALUES (?)', (text,))
        self.flush_notifications()

    def flush_notifications(self):
        for notification_id, text in self.db.execute('SELECT id,text FROM notifications ORDER BY id').fetchall():
            self.api.call('sendMessage', chat_id=self.owner, text=text)
            with self.db:
                self.db.execute('DELETE FROM notifications WHERE id=?', (notification_id,))

    def mark(self, mid, status, detail='', source=None):
        with self.db:
            self.db.execute('INSERT INTO covers(group_id,message_id,status,detail,source,updated,revision) VALUES (?,?,?,?,?,?,?) ON CONFLICT(group_id,message_id) DO UPDATE SET status=excluded.status,detail=excluded.detail,source=coalesce(excluded.source,covers.source),updated=excluded.updated,revision=excluded.revision',
                            (self.destination, mid, status, detail[:300], json.dumps(source) if source else None, time.time(), SEARCH_REVISION))

    def pending(self, rows):
        states = {mid: (status, revision, attempts, next_retry) for mid, status, revision, attempts, next_retry
                  in self.db.execute('SELECT message_id,status,revision,attempts,next_retry FROM covers WHERE group_id=?', (self.destination,))}
        fresh, retry = [], []
        for mid, key in rows:
            state = states.get(mid)
            if state is None or state[1] != SEARCH_REVISION:
                fresh.append((mid, key))
            elif state[0] == 'failed' and state[2] < MAX_ATTEMPTS and state[3] <= time.time():
                retry.append((mid, key))
        return fresh + retry

    def begin_attempt(self, mid):
        with self.db:
            self.db.execute("INSERT INTO covers(group_id,message_id,status,detail,source,updated,revision,attempts,next_retry) VALUES (?,?,'pending','',NULL,?,?,1,0) ON CONFLICT(group_id,message_id) DO UPDATE SET status='pending',updated=excluded.updated,attempts=CASE WHEN covers.revision=excluded.revision THEN covers.attempts+1 ELSE 1 END,revision=excluded.revision,next_retry=0",
                            (self.destination, mid, time.time(), SEARCH_REVISION))

    def schedule_retry(self, mid):
        with self.db:
            row = self.db.execute('SELECT status,attempts FROM covers WHERE group_id=? AND message_id=?',
                                  (self.destination, mid)).fetchone()
            if row and row[0] == 'failed':
                if row[1] >= MAX_ATTEMPTS:
                    self.db.execute("UPDATE covers SET status='exhausted',next_retry=0 WHERE group_id=? AND message_id=?", (self.destination, mid))
                else:
                    self.db.execute('UPDATE covers SET next_retry=? WHERE group_id=? AND message_id=?',
                                    (time.time() + 1800 * 2 ** max(0, row[1] - 1), self.destination, mid))

    def report(self, total, final=True):
        counts = dict(self.db.execute('SELECT status,count(*) FROM covers WHERE group_id=? AND revision=? GROUP BY status', (self.destination, SEARCH_REVISION)))
        examined = sum(counts.get(status, 0) for status in ('done', 'skipped', 'failed', 'exhausted', 'uncertain'))
        text = ('Bilan du passage' if final else 'Progression') + f' des pochettes : {examined}/{total} morceaux examinés.\n'
        text += '\n'.join(f'{label} : {counts.get(status, 0)}' for status, label in
                          [('done', 'Pochette MP3 et miniature remplacées'), ('skipped', 'Introuvables ou non traitables'),
                           ('failed', 'Erreurs à retenter (3 tentatives maximum)'), ('exhausted', 'Échecs après 3 tentatives'),
                           ('uncertain', 'Résultats incertains, sans relance automatique')])
        if examined < total:
            text += f'\nMorceaux restant à examiner : {total - examined}.'
        if final:
            text += '\nLes liens et le catalogue sont conservés. Les MP3 temporaires sont supprimés.'
        self.notify(text)

    def cleanup_temporary(self):
        for (mid,) in self.db.execute('SELECT message_id FROM temporary_messages').fetchall():
            try:
                self.api.call('deleteMessage', chat_id=self.owner, message_id=mid)
            except TelegramError as error:
                if error.code != 400:
                    continue
            with self.db:
                self.db.execute('DELETE FROM temporary_messages WHERE message_id=?', (mid,))

    def process(self, mid, track_key):
        artist, title = json.loads(track_key)
        if not artist or not title:
            raise Skip('Artiste ou titre absent.')
        folder = self.directory / self.destination / str(mid)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if shutil.disk_usage(self.directory).free < 300_000_000:
            raise Skip('Espace disque insuffisant : moins de 300 Mo libres.')
        self.mark(mid, 'fetching')
        # Bot API has no getMessage: recover metadata via a temporary private forward.
        source = self.api.call('forwardMessage', chat_id=self.owner, from_chat_id=self.destination,
                               message_id=mid, disable_notification=True)
        temporary = source['message_id']
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO temporary_messages VALUES (?)', (temporary,))
        self.cleanup_temporary()
        media = source.get('audio') or source.get('document', {})
        if not media.get('file_id') or media.get('file_size', 0) > MAX_AUDIO:
            raise Skip('MP3 inaccessible ou supérieur à 20 Mio (limite Bot API).')
        # Do not overwrite a track replaced since the catalogue snapshot.
        current_identity = track_identity(media.get('performer', ''), media.get('title', ''))
        if current_identity != track_key and source.get('audio'):
            raise Skip('Le message ne correspond plus à l’artiste/titre indexé.')
        minimal = {key: source[key] for key in ('audio', 'document', 'caption', 'caption_entities') if key in source}
        (folder / 'source.json').write_text(json.dumps(minimal, ensure_ascii=False))
        self.mark(mid, 'preparing', source=minimal)
        original = folder / 'original.mp3'
        downloaded = folder / 'download.mp3'
        self.media.download(media['file_id'], downloaded)
        if original.exists():
            if hashlib.sha256(original.read_bytes()).digest() != hashlib.sha256(downloaded.read_bytes()).digest():
                downloaded.unlink()
                raise Skip('Le fichier a changé depuis la sauvegarde : original conservé.')
            downloaded.unlink()
        else:
            downloaded.replace(original)
        # Read the MP3's own artist/title/album, not its old artwork.
        from mutagen.id3 import ID3, ID3NoHeaderError
        try:
            tags = ID3(original)
        except ID3NoHeaderError:
            tags = {}
        def tag_text(name, fallback=''):
            frame = tags.get(name)
            values = getattr(frame, 'text', [])
            return str(values[0]).strip() if values else fallback
        search_artist = tag_text('TPE1', media.get('performer') or artist)
        search_title = tag_text('TIT2', media.get('title') or title)
        search_album = tag_text('TALB')
        raw, origin = find_cover(search_artist, search_title, album=search_album)
        cover, thumbnail = images(raw)
        original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
        (folder / 'original.sha256').write_text(original_hash)
        updated = folder / 'updated.mp3'
        shutil.copyfile(original, updated)
        embed_cover(updated, cover)
        (folder / 'cover.jpg').write_bytes(cover)
        self.mark(mid, 'applying', origin)
        result = self.media.edit(self.destination, mid, minimal, updated, thumbnail)
        if not isinstance(result, dict) or result.get('message_id') != mid:
            raise RuntimeError('Modification Telegram non confirmée.')
        index_music(self.catalogue, self.destination, result)
        with self.catalogue.lock, self.catalogue.db:
            self.catalogue.db.execute('INSERT INTO music_artwork VALUES (?,?,?,?,?,?) ON CONFLICT(group_id,message_id) DO UPDATE SET track_key=excluded.track_key,jpeg=excluded.jpeg,source_url=excluded.source_url,updated=excluded.updated', (self.destination, mid, track_key, cover, origin, time.time()))
        self.mark(mid, 'done', origin, minimal)
        shutil.rmtree(folder)  # Telegram confirmed the replacement; retain catalogue metadata only.

    def run(self, limit=None):
        self.flush_notifications()
        self.cleanup_temporary()
        with self.db:
            self.db.execute("UPDATE covers SET status='failed' WHERE status IN ('pending','preparing') AND revision=?", (SEARCH_REVISION,))
            self.db.execute("UPDATE covers SET status='exhausted' WHERE status='failed' AND attempts>=? AND revision=?", (MAX_ATTEMPTS, SEARCH_REVISION))
            self.db.execute("UPDATE covers SET status='uncertain',detail='Interruption pendant une opération : vérifier le message avant reprise' WHERE status IN ('fetching','applying') AND revision=?", (SEARCH_REVISION,))
        destination_folder = self.directory / self.destination
        if destination_folder.is_dir():
            for folder in destination_folder.iterdir():
                if folder.is_dir() and folder.name.isdigit():
                    shutil.rmtree(folder)
        rows = self.catalogue.db.execute('SELECT message_id,track_key FROM music WHERE group_id=? ORDER BY message_id', (self.destination,)).fetchall()
        pending = self.pending(rows)
        if limit is not None:
            pending = pending[:limit]
        if not pending:
            return
        self.notify(f'Nouvelles recherches internet de pochettes : {len(pending)} morceau(x) à examiner. Les morceaux déjà terminés ne seront pas refaits. Les échecs sont limités à 3 tentatives. La mention [432Hz] est ignorée et les anciennes jaquettes ne sont pas réutilisées. Les fichiers temporaires seront supprimés après traitement, le catalogue sera conservé. Les messages du canal seront modifiés en place. Des transferts privés temporaires, silencieux puis supprimés, permettent de récupérer les fichiers.')
        failures = 0
        for number, (mid, key) in enumerate(pending, 1):
            if STOP.is_set():
                break
            self.begin_attempt(mid)
            try:
                self.process(mid, key)
                failures = 0
            except Retry as error:
                self.mark(mid, 'failed', str(error))
                failures += 1
            except Skip as error:
                self.mark(mid, 'skipped', str(error))
                failures = 0
            except TelegramError as error:
                status = self.db.execute('SELECT status FROM covers WHERE group_id=? AND message_id=?', (self.destination, mid)).fetchone()
                uncertain = error.code == 0 and status and status[0] in ('applying', 'fetching')
                self.mark(mid, 'uncertain' if uncertain else ('skipped' if error.code in (400, 403) else 'failed'), f'Telegram code {error.code}')
                failures += 1
                if error.code == 429:
                    time.sleep(min(error.retry_after, 60))
            except Exception as error:
                # Avoid logging Telegram file URLs or secrets from transport exceptions.
                status = self.db.execute('SELECT status FROM covers WHERE group_id=? AND message_id=?', (self.destination, mid)).fetchone()
                self.mark(mid, 'uncertain' if status and status[0] in ('applying', 'fetching') else 'failed', f'Erreur réseau, image ou fichier ({type(error).__name__}) : détails privés non journalisés')
                failures += 1
            self.schedule_retry(mid)
            self.cleanup_temporary()
            # On failure too, the source file_id remains in SQLite for recovery from Telegram.
            # Never accumulate MP3s or artwork on the VPS between tracks.
            shutil.rmtree(self.directory / self.destination / str(mid), ignore_errors=True)
            status = self.db.execute('SELECT status FROM covers WHERE group_id=? AND message_id=?', (self.destination, mid)).fetchone()
            print(f'Pochette message {mid} : {status[0] if status else "non traité"}', flush=True)
            if number % 50 == 0:
                self.report(len(rows), final=False)
            if failures >= 3:
                self.notify('La source des jaquettes est momentanément indisponible. Pause de 60 secondes, puis poursuite des morceaux restants. La progression est conservée.')
                if STOP.wait(60):
                    break
                failures = 0
            time.sleep(1.1)
        self.report(len(rows))


def progress_snapshot(directory, destination):
    path = Path(directory) / 'covers.sqlite3'
    url = os.environ.get('DATABASE_URL', '')
    if not url and not path.exists():
        return None
    db = connect(url or path.resolve().as_uri() + '?mode=ro', schema='covers', readonly=True, uri=True, timeout=1)
    try:
        return dict(db.execute('SELECT status,count(*) FROM covers WHERE group_id=? AND revision=? GROUP BY status',
                               (str(destination), SEARCH_REVISION)))
    finally:
        db.close()


def main():
    os.umask(0o077)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: STOP.set())
    from mutagen.mp3 import MP3
    from PIL import Image
    owner = os.environ.get('OWNER_USER_ID', '').strip() or os.environ.get('NOTIFY_CHAT_ID', '').strip()
    if not owner.isdigit() or int(owner) <= 0:
        raise SystemExit('Configurer le propriétaire privé.')
    directory = Path(os.environ.get('COVERS_DIR', '/var/lib/maxxie-bot/covers'))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        api = Telegram(os.environ['BOT_TOKEN'], ipv4_only=os.environ.get('TELEGRAM_IPV4_ONLY', '1') == '1')
        db = connect(target())
        db.execute('PRAGMA busy_timeout=10000')
        store = SimpleNamespace(db=db, lock=threading.RLock())
        try:
            identity = api.call('getMe')
            member = api.call('getChatMember', chat_id=os.environ['GROUP_ID'], user_id=identity['id'])
            if not member.get('can_edit_messages'):
                raise SystemExit('Le bot doit pouvoir modifier les messages du canal.')
            batch = CoverBatch(api, store, directory, os.environ['GROUP_ID'], int(owner))
            try:
                batch.run()
            finally:
                batch.db.close()
        finally:
            api.close()
            db.close()


if __name__ == '__main__':
    main()
