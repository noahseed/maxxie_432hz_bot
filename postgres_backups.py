"""Sauvegardes PostgreSQL cohérentes, restauration dans une base temporaire puis comparaison."""
from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid


def pg_environment(dsn, database=None):
    from psycopg.conninfo import conninfo_to_dict
    env = dict(os.environ)
    for key in ('PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD', 'PGSSLMODE', 'PGOPTIONS', 'PGSERVICE'):
        env.pop(key, None)
    for key, value in conninfo_to_dict(dsn).items():
        env['PG' + ('DATABASE' if key == 'dbname' else key.upper())] = str(value)
    if database:
        env['PGDATABASE'] = database
    env['PGCONNECT_TIMEOUT'] = '10'
    return env


def table_state(connection):
    from psycopg import sql
    from migrate_postgres import row_digest
    state = {}
    for schema, table in connection.execute("SELECT table_schema,table_name FROM information_schema.tables WHERE table_schema IN ('app','visitors','covers') AND table_type='BASE TABLE' ORDER BY table_schema,table_name").fetchall():
        primary = [row[0] for row in connection.execute('SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) WHERE i.indrelid=%s::regclass AND i.indisprimary ORDER BY array_position(i.indkey::smallint[],a.attnum)', (schema + '.' + table,))]
        if not primary:
            raise RuntimeError('Clé primaire absente dans une table sauvegardée')
        query = sql.SQL('SELECT * FROM {}.{} ORDER BY {}').format(sql.Identifier(schema), sql.Identifier(table), sql.SQL(',').join(map(sql.Identifier, primary)))
        count, fingerprint = row_digest(connection.execute(query))
        state[schema + '.' + table] = {'rows': count, 'sha256': fingerprint}
    return state


@contextmanager
def temporary_server():
    import re
    version = subprocess.check_output(['pg_restore', '--version'], text=True)
    major = re.search(r'PostgreSQL\) (\d+)', version).group(1)
    bindir = Path('/usr/lib/postgresql') / major / 'bin'
    with tempfile.TemporaryDirectory(prefix='maxxie-pg-verify-') as folder:
        root = Path(folder)
        socket = root / 'socket'
        socket.mkdir(mode=0o700)
        data = root / 'data'
        subprocess.run([str(bindir / 'initdb'), '-D', str(data), '--auth=trust', '--no-locale', '--encoding=UTF8', '--username=restorebot'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
        started = False
        try:
            subprocess.run([str(bindir / 'pg_ctl'), '-D', str(data), '-l', str(root / 'postgres.log'),
                            '-o', f"-k {socket} -h '' -p 55439 -c fsync=off", '-w', 'start'],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
            started = True
            yield f'host={socket} port=55439 user=restorebot dbname=postgres'
        finally:
            if started:
                subprocess.run([str(bindir / 'pg_ctl'), '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)


def verify_restore(dsn, dump, expected):
    import psycopg
    # The production role never needs CREATEDB: restore in a private temporary server.
    with temporary_server() as restored_dsn:
        subprocess.run(['pg_restore', '--exit-on-error', '--no-owner', '--no-acl', '--dbname', 'postgres', str(dump)],
                       env=pg_environment(restored_dsn), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180)
        with psycopg.connect(restored_dsn, autocommit=True) as restored:
            if table_state(restored) != expected:
                raise RuntimeError('La restauration PostgreSQL diffère du snapshot')


def create_backup(dsn, root, keep=14):
    import psycopg
    from backups import digest
    if keep < 1:
        raise ValueError('Rétention invalide')
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.postgres-backup-', dir=root))
    try:
        dump = temporary / 'database.dump'
        with psycopg.connect(dsn, autocommit=True) as source:
            with source.transaction():
                source.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                snapshot = source.execute('SELECT pg_export_snapshot()').fetchone()[0]
                expected = table_state(source)
                subprocess.run(['pg_dump', '--format=custom', '--no-owner', '--no-acl', '--snapshot', snapshot, '--file', str(dump)],
                               env=pg_environment(dsn), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180)
        os.chmod(dump, 0o600)
        verify_restore(dsn, dump, expected)
        manifest = {'format': 'postgresql', 'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                    'files': {'database.dump': digest(dump)}, 'tables': expected, 'restore_verified': True}
        (temporary / 'manifest.json').write_text(json.dumps(manifest))
        final = root / ('backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-postgresql')
        temporary.rename(final)
        marker = root / '.latest.pending'
        marker.write_text(json.dumps({**manifest, 'snapshot': final.name}))
        marker.replace(root / 'latest.json')
        previous = sorted((path for path in root.glob('backup-*') if path != final and path.is_dir() and not path.is_symlink()), key=lambda path: path.stat().st_mtime_ns, reverse=True)
        for path in previous[keep - 1:]:
            shutil.rmtree(path)
        return final
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def prepare_restore(snapshot, destination):
    from backups import digest
    snapshot, destination = Path(snapshot), Path(destination)
    metadata = json.loads((snapshot / 'manifest.json').read_text())
    if set(metadata['files']) != {'database.dump'}:
        raise ValueError('Manifest PostgreSQL invalide')
    dump = snapshot / 'database.dump'
    if dump.is_symlink() or digest(dump) != metadata['files']['database.dump']:
        raise ValueError('Empreinte PostgreSQL invalide')
    # Decode the full archive without executing SQL against any database.
    subprocess.run(['pg_restore', '--file', os.devnull, str(dump)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('La destination existe déjà')
    destination.mkdir(mode=0o700)
    try:
        shutil.copyfile(dump, destination / 'database.dump')
        os.chmod(destination / 'database.dump', 0o600)
        shutil.copyfile(snapshot / 'manifest.json', destination / 'manifest.json')
    except BaseException:
        shutil.rmtree(destination)
        raise
    return destination
