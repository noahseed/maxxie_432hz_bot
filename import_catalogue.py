"""Import transactionnel d'un export JSON Telegram Desktop dans un catalogue existant."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from catalogue import artist_metadata, music_metadata


def export_rows(data, group_id):
    if not isinstance(data, dict) or data.get('type') not in ('public_supergroup', 'private_supergroup'):
        raise ValueError('Un export de supergroupe Telegram est requis')
    exported_id = data.get('id')
    if type(exported_id) is not int or exported_id <= 0 or str(group_id) != '-100' + str(exported_id):
        raise ValueError('L’export ne correspond pas au groupe configuré')
    messages = data.get('messages')
    if not isinstance(messages, list):
        raise ValueError('Liste de messages invalide')
    rows, seen = [], set()
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError('Message invalide')
        if message.get('type') != 'message':
            continue
        media = {key: message.get(key, '') for key in ('file_name', 'mime_type', 'title', 'performer')}
        if any(not isinstance(value, str) for value in media.values()):
            raise ValueError('Métadonnées musicales invalides')
        text = message.get('text', '')
        if isinstance(text, list):
            text = ''.join(part if isinstance(part, str) else part.get('text', '')
                           for part in text if isinstance(part, (str, dict)))
        if not isinstance(text, str):
            raise ValueError('Légende invalide')
        metadata = music_metadata({'audio': media, 'caption': text})
        if metadata is None:
            continue
        message_id = message.get('id')
        if type(message_id) is not int or message_id <= 0 or message_id in seen:
            raise ValueError('Identifiant de message invalide ou répété')
        seen.add(message_id)
        rows.append((str(group_id), message_id, *metadata, *artist_metadata({"audio": media})))
    return rows


def import_export(source, database, group_id):
    rows = export_rows(json.loads(Path(source).read_text(encoding='utf-8')), group_id)
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=rw', uri=True, timeout=30)) as db, db:
        # Ne pas ouvrir TaskStore : le bot reste seul propriétaire de sa file.
        db.execute('BEGIN IMMEDIATE')
        before = db.total_changes
        db.executemany('INSERT OR IGNORE INTO music(group_id,message_id,label,searchable,artist,artist_key) VALUES (?,?,?,?,?,?)', rows)
        added = db.total_changes - before
        # Compléter les anciens catalogues sans remplacer les artistes renseignés depuis.
        db.executemany("UPDATE music SET artist=?,artist_key=? WHERE group_id=? AND message_id=? AND artist='' AND ?<>''",
                       [(row[4], row[5], row[0], row[1], row[4]) for row in rows])
        present = {row[0] for row in db.execute('SELECT message_id FROM music WHERE group_id=?', (str(group_id),))}
        if not {row[1] for row in rows} <= present:
            raise RuntimeError('Vérification du catalogue importé impossible')
        return {'mp3': len(rows), 'added': added, 'existing': len(rows) - added, 'total': len(present)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('export', type=Path)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--group-id', required=True)
    args = parser.parse_args()
    print(json.dumps(import_export(args.export, args.database, args.group_id), ensure_ascii=False))


if __name__ == '__main__':
    main()
