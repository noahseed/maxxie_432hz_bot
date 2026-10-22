"""Sauvegardes SQLite cohérentes par base, vérifiées et restaurables sans écrasement."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import subprocess
import tarfile


def check_database(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RuntimeError("Sauvegarde SQLite invalide")


def copy_database(source, target):
    if target.exists():
        raise FileExistsError("La destination existe déjà")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as incoming:
        with closing(sqlite3.connect(target)) as outgoing:
            incoming.backup(outgoing, pages=128)
    os.chmod(target, 0o600)
    check_database(target)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def restore(snapshot, destination):
    """La destination doit être nouvelle ; jamais de remplacement de bases actives."""
    snapshot, destination = Path(snapshot), Path(destination)
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    if set(manifest["files"]) != {"tasks.sqlite3", "visitors.sqlite3"}:
        raise ValueError("Manifest de sauvegarde inattendu")
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, expected in manifest["files"].items():
        source = snapshot / name
        if source.is_symlink() or digest(source) != expected:
            raise ValueError("Empreinte de sauvegarde invalide")
        copy_database(source, destination / name)
    # Une sauvegarde peut précéder des envois réellement effectués depuis.
    # Ne jamais rejouer aveuglément ses anciennes tâches lors d'une restauration.
    with closing(sqlite3.connect(destination / "tasks.sqlite3")) as db, db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone():
            db.execute("UPDATE tasks SET status='uncertain',payload='{}' WHERE status IN ('pending','running')")
            db.execute("UPDATE actions SET status='uncertain' WHERE status IN ('pending','sending')")
            if "request" in {row[1] for row in db.execute("PRAGMA table_info(actions)")}:
                db.execute("UPDATE actions SET request=NULL")
    return destination


def encrypt_snapshot(snapshot, certificate, output):
    """CMS AES-256-GCM : seul le certificat public est présent sur le VPS."""
    with tempfile.TemporaryDirectory(prefix=".encrypt-", dir=output.parent) as staging:
        archive = Path(staging) / "snapshot.tar.gz"
        encrypted = Path(staging) / "snapshot.enc"
        with tarfile.open(archive, "w:gz") as tar:
            for name in ("manifest.json", "tasks.sqlite3", "visitors.sqlite3"):
                tar.add(snapshot / name, arcname=name)
        subprocess.run(["openssl", "cms", "-encrypt", "-binary", "-aes-256-gcm",
                        "-in", str(archive), "-out", str(encrypted), "-outform", "DER",
                        "-recip", str(certificate), "-keyopt", "rsa_padding_mode:oaep"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        os.chmod(encrypted, 0o600)
        os.replace(encrypted, output)


def create_backup(tasks, visitors, root, keep=14):
    if keep < 1:
        raise ValueError("La rétention doit être positive")
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".backup-", dir=root))
    try:
        for name, source in (("tasks.sqlite3", tasks), ("visitors.sqlite3", visitors)):
            copy_database(Path(source), temporary / name)
        manifest = {"created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "files": {name: digest(temporary / name) for name in ("tasks.sqlite3", "visitors.sqlite3")}}
        (temporary / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        # Vérifier réellement la restauration sans toucher aux bases actives.
        with tempfile.TemporaryDirectory(prefix=".restore-check-", dir=root) as staging:
            restore(temporary, Path(staging) / "restored")
        final = root / ("backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + temporary.name[-8:])
        temporary.rename(final)
        marker = root / ".latest.pending"
        marker.write_text(json.dumps({**manifest, "snapshot": final.name}), encoding="utf-8")
        os.replace(marker, root / "latest.json")
        snapshots = sorted((path for path in root.glob("backup-*") if path != final and path.is_dir() and not path.is_symlink()),
                           key=lambda path: path.stat().st_mtime_ns, reverse=True)
        for old in snapshots[keep - 1:]:
            shutil.rmtree(old)
        if not final.is_dir():
            raise RuntimeError("La dernière sauvegarde a disparu pendant la rotation")
        return final
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore", type=Path)
    parser.add_argument("--restore-to", type=Path)
    args = parser.parse_args()
    if args.restore or args.restore_to:
        if not args.restore or not args.restore_to:
            parser.error("Utiliser --restore et --restore-to ensemble")
        result = restore(args.restore, args.restore_to)
        print(f"Restauration vérifiée dans {result}. Bases actives inchangées ; anciennes tâches en attente neutralisées.")
    else:
        result = create_backup(os.environ.get("STATE_DB", "/var/lib/maxxie-bot/tasks.sqlite3"),
                               os.environ.get("VISITORS_DB", "/var/lib/maxxie-bot/visitors.sqlite3"),
                               os.environ.get("BACKUP_DIR", "/var/lib/maxxie-bot/backups"),
                               int(os.environ.get("BACKUP_KEEP", "14")))
        certificate = os.environ.get("BACKUP_CERT", "").strip()
        if certificate:
            encrypt_snapshot(result, Path(certificate), result.parent / "offsite-latest.enc")
        print(f"Sauvegarde et test de restauration réussis : {result.name}")


if __name__ == "__main__":
    main()
