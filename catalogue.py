"""Catalogue des MP3 du groupe, conservé dans la base SQLite des tâches."""
import re
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
        store.db.execute('INSERT INTO music(group_id,message_id,label,searchable) VALUES (?,?,?,?) '
                         'ON CONFLICT(group_id,message_id) DO UPDATE SET label=excluded.label,searchable=excluded.searchable',
                         (str(group_id), message_id, *metadata))
    return True


def search_music(store, group_id, query, limit=10):
    words = normalize(query).split()
    if not words:
        return []
    conditions = ' AND '.join('instr(searchable,?)>0' for _ in words)
    with store.lock:
        return store.db.execute('SELECT message_id,label FROM music WHERE group_id=? AND ' + conditions +
                                ' ORDER BY message_id DESC LIMIT ?', (str(group_id), *words, limit)).fetchall()
