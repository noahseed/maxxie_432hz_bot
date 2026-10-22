"""Copie SSH chiffrée vers l'ordinateur, avec test local de déchiffrement/restauration."""
import argparse
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

from backups import restore


def verify_encrypted(archive, key, staging):
    clear = staging / "snapshot.tar.gz"
    subprocess.run(["openssl", "cms", "-decrypt", "-binary", "-inform", "DER",
                    "-in", str(archive), "-inkey", str(key), "-out", str(clear)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    unpacked = staging / "snapshot"
    unpacked.mkdir(mode=0o700)
    with tarfile.open(clear, "r:gz") as tar:
        expected = {"manifest.json", "tasks.sqlite3", "visitors.sqlite3"}
        if {member.name for member in tar.getmembers()} != expected:
            raise ValueError("Archive inattendue")
        for name in expected:
            member = tar.getmember(name)
            if not member.isfile():
                raise ValueError("Type de fichier inattendu")
            with tar.extractfile(member) as source, (unpacked / name).open("wb") as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
    restore(unpacked, staging / "restore-check")
    metadata = json.loads((unpacked / "manifest.json").read_text(encoding="utf-8"))
    created = datetime.fromisoformat(metadata["created_at"])
    if created.tzinfo is None:
        raise ValueError("Date de sauvegarde sans fuseau horaire")
    age = (datetime.now(timezone.utc) - created).total_seconds()
    if age < -300 or age > 36 * 3600:
        raise ValueError("Sauvegarde hors VPS périmée (plus de 36 heures) ou datée dans le futur")
    return metadata


def pull(host, key, destination):
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", host):
        raise ValueError("Destination SSH invalide")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pull-", dir=destination) as temporary:
        staging = Path(temporary)
        archive = staging / "download.enc"
        with archive.open("wb") as stream:
            subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2", host,
                            "sudo", "-n", "cat", "/var/lib/maxxie-bot/backups/offsite-latest.enc"],
                           stdout=stream, stderr=subprocess.PIPE, check=True, timeout=120)
        verify_encrypted(archive, key, staging)
        with archive.open("rb") as stream:
            fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
        final = destination / (fingerprint + ".enc")
        # Remplacer aussi une copie existante : elle pourrait avoir été altérée
        # depuis sa dernière vérification. Le nom identique évite les doublons.
        os.replace(archive, final)
        copies = sorted(destination.glob("*.enc"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
        for old in [path for path in copies if path != final][13:]:
            old.unlink()
        return final


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="debian@maxxie432.rondeau-cameira.fr")
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    result = pull(args.host, args.key, args.destination)
    print(f"Copie chiffrée vérifiée : {result.name}")


if __name__ == "__main__":
    main()
