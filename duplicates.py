"""Confirmation privée d'une publication ressemblant à un morceau existant."""
import json
import secrets
import time


def create_publication(store, group_id, message):
    token = secrets.token_hex(8)
    user_id = message['from']['id']
    media_key = 'audio' if message.get('audio') else 'document'
    media = message[media_key]
    minimal = {'message_id': message['message_id'], 'chat': {'id': message['chat']['id'], 'type': 'private'},
               'from': {'id': user_id, 'is_bot': False},
               media_key: {key: media[key] for key in ('file_name', 'mime_type', 'title', 'performer', 'file_unique_id') if key in media}}
    if message.get('caption'):
        minimal['caption'] = message['caption'][:1024]
    with store.lock, store.db:
        store.db.execute('INSERT INTO duplicate_publications VALUES (?,?,?,?,?,NULL)',
                         (token, user_id, str(group_id), json.dumps(minimal), time.time() + 3600))
    return token


def resolve_publication(store, token, chat_id, group_id, approve):
    with store.lock, store.db:
        row = store.db.execute('SELECT message,expires,task_id FROM duplicate_publications '
                               'WHERE token=? AND chat_id=? AND group_id=?',
                               (token, chat_id, str(group_id))).fetchone()
        if row is None or row[1] <= time.time():
            return 'expired'
        if row[2] is not None:
            return 'approved'
        if not approve:
            store.db.execute('DELETE FROM duplicate_publications WHERE token=?', (token,))
            return 'cancelled'
        payload = json.dumps({'group_id': str(group_id), 'message': json.loads(row[0])})
        task = store.db.execute("INSERT INTO tasks(kind,payload,lane,updated) VALUES ('publish_confirmed',?,0,?)",
                                (payload, time.time()))
        store.db.execute("UPDATE duplicate_publications SET task_id=?,message='{}' WHERE token=?", (task.lastrowid, token))
        return 'queued'
