"""Catalogue des MP3 du groupe, conservé dans la base SQLite des tâches."""
import json
import re
import secrets
import time
import unicodedata
from urllib.parse import urlsplit


def normalize(text):
    text = unicodedata.normalize('NFKD', text.casefold().replace('_', ' '))
    text = ''.join(char for char in text if not unicodedata.combining(char))
    return ' '.join(re.findall(r'\w+', text, flags=re.UNICODE))


def music_metadata(message):
    media = message.get('audio') or message.get('document') or {}
    filename = media.get('file_name', '')
    if not (filename.lower().endswith('.mp3') or (not filename and media.get('mime_type') == 'audio/mpeg')):
        return None
    title = media.get('title', '')[:300]
    artist = media.get('performer', '')[:300]
    caption = message.get('caption', '')[:1024]
    filename = filename[:300]
    label = ' — '.join(value for value in (artist, title) if value)
    label = label or filename or caption.split('\n')[0] or 'MP3 sans titre'
    searchable = normalize(' '.join((artist, title, filename, caption)))
    return label[:200], searchable


def artist_metadata(message):
    media = message.get('audio') or message.get('document') or {}
    artist = media.get('performer', '').strip()[:300]
    return artist, normalize(artist)


def track_identity(artist, title):
    title = re.sub(r'[\[(]\s*432\s*hz\s*[\])]', '', title, flags=re.IGNORECASE)
    artist, title = normalize(artist), normalize(title)
    return json.dumps([artist, title], ensure_ascii=False) if artist and title else ''


def duplicate_metadata(message):
    media = message.get('audio') or message.get('document') or {}
    return (media.get('file_unique_id', '')[:200],
            track_identity(media.get('performer', ''), media.get('title', '')),
            normalize(media.get('file_name', '')[:300]))


def find_duplicates(store, group_id, message, exclude_id=0):
    if music_metadata(message) is None:
        return [], ''
    keys = duplicate_metadata(message)
    for column, value, reason in zip(('file_unique_id', 'track_key', 'file_name_key'), keys,
                                    ('Même fichier Telegram', 'Même artiste et même titre', 'Même nom de fichier')):
        if value:
            with store.lock:
                rows = store.db.execute('SELECT message_id,label FROM music WHERE group_id=? AND message_id<>? AND ' +
                                        column + '=? ORDER BY message_id DESC LIMIT 3',
                                        (str(group_id), exclude_id, value)).fetchall()
            if rows:
                return rows, reason
    return [], ''


def message_link(group_id, group_url, message_id):
    parsed = urlsplit(group_url)
    username = parsed.path.strip('/')
    if parsed.netloc == 't.me' and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', username) and username != 'joinchat':
        return f'https://t.me/{username}/{int(message_id)}'
    group_id = str(group_id)
    if group_id.startswith('-100') and group_id[4:].isdigit():
        return f'https://t.me/c/{group_id[4:]}/{int(message_id)}'
    return None


def index_music(store, group_id, message, message_id=None):
    metadata = music_metadata(message)
    message_id = message_id if message_id is not None else message.get('message_id')
    if not isinstance(message_id, int) or message_id <= 0:
        return False
    if metadata is None:
        with store.lock, store.db:
            store.db.execute('DELETE FROM music WHERE group_id=? AND message_id=?', (str(group_id), message_id))
        return False
    with store.lock, store.db:
        store.db.execute('INSERT INTO music(group_id,message_id,label,searchable,artist,artist_key,file_unique_id,track_key,file_name_key) VALUES (?,?,?,?,?,?,?,?,?) '
                         'ON CONFLICT(group_id,message_id) DO UPDATE SET label=excluded.label,searchable=excluded.searchable,'
                         'artist=excluded.artist,artist_key=excluded.artist_key,file_unique_id=excluded.file_unique_id,'
                         'track_key=excluded.track_key,file_name_key=excluded.file_name_key',
                         (str(group_id), message_id, *metadata, *artist_metadata(message), *duplicate_metadata(message)))
    return True


def search_music(store, group_id, query, limit=10):
    words = normalize(query).split()
    if not words:
        return []
    conditions = ' AND '.join('instr(searchable,?)>0' for _ in words)
    with store.lock:
        return store.db.execute('SELECT message_id,label FROM music WHERE group_id=? AND ' + conditions +
                                ' ORDER BY message_id DESC LIMIT ?', (str(group_id), *words, limit)).fetchall()


def create_session(store, chat_id, kind, value=''):
    token = secrets.token_hex(8)
    with store.lock, store.db:
        store.db.execute('INSERT INTO browse_sessions VALUES (?,?,?,?,?)',
                         (token, chat_id, kind, value, time.time() + 3600))
    return token


def get_session(store, chat_id, token):
    with store.lock:
        return store.db.execute('SELECT kind,value FROM browse_sessions WHERE token=? AND chat_id=? AND expires>?',
                                (token, chat_id, time.time())).fetchone()


def music_page(store, group_id, kind, value, offset=0, limit=10):
    if kind == 'search':
        words = normalize(value).split()
        if not words:
            return [], 0
        condition = ' AND '.join('instr(searchable,?)>0' for _ in words)
        params = words
    elif kind == 'artist':
        condition, params = 'artist_key=?', [value]
    else:
        raise ValueError('Type de recherche inconnu')
    where = 'group_id=? AND ' + condition
    with store.lock:
        total = store.db.execute('SELECT count(*) FROM music WHERE ' + where, (str(group_id), *params)).fetchone()[0]
        rows = store.db.execute('SELECT message_id,label FROM music WHERE ' + where +
                                ' ORDER BY message_id DESC LIMIT ? OFFSET ?',
                                (str(group_id), *params, limit, offset)).fetchall()
    return rows, total


def artists_page(store, group_id, offset=0, limit=10):
    with store.lock:
        total = store.db.execute('SELECT count(DISTINCT artist_key) FROM music WHERE group_id=?',
                                 (str(group_id),)).fetchone()[0]
        rows = store.db.execute("SELECT artist_key,coalesce(nullif(min(artist),''),'Artiste non renseigné'),count(*) "
                                'FROM music WHERE group_id=? GROUP BY artist_key ORDER BY artist_key LIMIT ? OFFSET ?',
                                (str(group_id), limit, offset)).fetchall()
    return rows, total


def random_music(store, group_id):
    with store.lock:
        return store.db.execute('SELECT message_id,label FROM music WHERE group_id=? ORDER BY random() LIMIT 1',
                                (str(group_id),)).fetchone()
