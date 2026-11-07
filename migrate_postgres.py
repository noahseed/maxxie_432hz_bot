"""Migration vérifiée des trois bases SQLite vers une base PostgreSQL vide."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from database import connect, identifier


def row_digest(rows):
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        values = [{'bytes': base64.b64encode(bytes(value)).decode()} if isinstance(value, (bytes, bytearray, memoryview)) else (int(value) if isinstance(value, float) and value.is_integer() else value) for value in row]
        payload = json.dumps(values, ensure_ascii=False, separators=(',', ':')).encode()
        digest.update(len(payload).to_bytes(8, 'big'))
        digest.update(payload)
        count += 1
    return count, digest.hexdigest()


def initialize(dsn):
    from reliability import TaskStore
    from bot import StartVisitors
    from covers import CoverBatch
    store = TaskStore(dsn)
    visitors = StartVisitors(dsn)
    visitors.db.close()
    with tempfile.TemporaryDirectory() as directory:
        batch = CoverBatch(None, store, directory, '-1000', 1, database_url=dsn)
        batch.db.close()
    return store


def migrate(dsn, sources):
    import psycopg
    from psycopg import sql
    store = initialize(dsn)
    report = {}
    try:
        with store.db.raw.transaction():
            for schema in sources:
                tables = store.db.raw.execute('SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type=\'BASE TABLE\'', (schema,)).fetchall()
                for (table,) in tables:
                    if store.db.raw.execute(sql.SQL('SELECT count(*) FROM {}.{}').format(sql.Identifier(schema), sql.Identifier(table))).fetchone()[0]:
                        raise RuntimeError('La destination doit être vide ; aucune donnée ne sera écrasée.')
            for schema, path in sources.items():
                source = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
                try:
                    tables = source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
                    for (table,) in tables:
                        info = source.execute('PRAGMA table_info(' + identifier(table) + ')').fetchall()
                        columns = [row[1] for row in info]
                        primary = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
                        if not primary:
                            raise RuntimeError('Table sans clé primaire : ' + table)
                        select = 'SELECT ' + ','.join(identifier(column) for column in columns) + ' FROM ' + identifier(table) + ' ORDER BY ' + ','.join(identifier(column) for column in primary)
                        rows = source.execute(select).fetchall()
                        insert = sql.SQL('INSERT INTO {}.{} ({}) VALUES ({})').format(
                            sql.Identifier(schema), sql.Identifier(table),
                            sql.SQL(',').join(map(sql.Identifier, columns)), sql.SQL(',').join(sql.Placeholder() for _ in columns))
                        if rows:
                            with store.db.raw.cursor() as cursor:
                                cursor.executemany(insert, rows)
                        restored = store.db.raw.execute(sql.SQL('SELECT {} FROM {}.{} ORDER BY {}').format(
                            sql.SQL(',').join(map(sql.Identifier, columns)), sql.Identifier(schema), sql.Identifier(table),
                            sql.SQL(',').join(map(sql.Identifier, primary))))
                        expected, actual = row_digest(rows), row_digest(restored)
                        if expected != actual:
                            raise RuntimeError('Vérification des données en échec : ' + schema + '.' + table)
                        report[schema + '.' + table] = {'rows': expected[0], 'sha256': expected[1]}
                        sequence = store.db.raw.execute('SELECT pg_get_serial_sequence(%s,%s)', (schema + '.' + table, 'id')).fetchone()[0] if 'id' in columns else None
                        if sequence:
                            maximum = source.execute('SELECT max(id) FROM ' + identifier(table)).fetchone()[0]
                            store.db.raw.execute('SELECT setval(%s,%s,%s)', (sequence, maximum or 1, maximum is not None))
                finally:
                    source.close()
        return report
    finally:
        store.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--tasks', required=True)
    parser.add_argument('--visitors', required=True)
    parser.add_argument('--covers', required=True)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    report = migrate(args.database, {'app': args.tasks, 'visitors': args.visitors, 'covers': args.covers})
    args.report.write_text(json.dumps(report, indent=2))
    print('Migration et comparaison intégrale réussies :', len(report), 'tables')


if __name__ == '__main__':
    main()
