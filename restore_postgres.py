"""Restaurer dans une base PostgreSQL vide, sans rejouer les anciens envois Telegram."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile


def restore(snapshot, destination):
    import psycopg
    from postgres_backups import prepare_restore, pg_environment, table_state
    snapshot = Path(snapshot)
    with tempfile.TemporaryDirectory(prefix='maxxie-restore-') as folder:
        prepared = prepare_restore(snapshot, Path(folder) / 'checked')
        metadata = json.loads((prepared / 'manifest.json').read_text())
        with psycopg.connect(destination, autocommit=True) as guard:
            key = int.from_bytes(hashlib.sha256(b'maxxie:bot').digest()[:8], 'big', signed=True)
            if not guard.execute('SELECT pg_try_advisory_lock(%s)', (key,)).fetchone()[0]:
                raise RuntimeError('Un bot utilise déjà la destination')
            if guard.execute("SELECT 1 FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema') LIMIT 1").fetchone():
                raise RuntimeError('La destination doit être une base vide ; aucune donnée écrasée')
            subprocess.run(['pg_restore', '--exit-on-error', '--no-owner', '--no-acl', '--dbname', guard.info.dbname, str(prepared / 'database.dump')],
                           env=pg_environment(destination), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180)
            if table_state(guard) != metadata['tables']:
                raise RuntimeError('La restauration diffère de la sauvegarde')
            with guard.transaction():
                guard.execute("UPDATE app.tasks SET status='uncertain',payload='{}' WHERE status IN ('pending','running')")
                guard.execute("UPDATE app.actions SET status='uncertain',request=NULL WHERE status IN ('pending','sending')")
                guard.execute('DELETE FROM app.duplicate_publications')
    return destination


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',required=True,type=Path)
    parser.add_argument('--database',required=True)
    args=parser.parse_args()
    restore(args.snapshot,args.database)
    print('Restauration PostgreSQL vérifiée ; anciennes tâches non rejouées.')


if __name__ == '__main__':
    main()
