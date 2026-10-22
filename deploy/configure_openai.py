"""Saisir et tester la clé OpenAI directement sur le VPS."""
from getpass import getpass
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, "/opt/maxxie-bot")
from assistant_ai import AIError, AssistantAI, DEFAULT_MODEL


def main():
    if os.geteuid() != 0 or not sys.stdin.isatty():
        raise SystemExit("Lancez ce script dans un terminal : sudo python3 /opt/maxxie-bot/configure_openai.py")
    path = Path("/etc/maxxie-bot.env")
    source = path.read_text()
    config = {}
    for line in source.splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            config[name.strip()] = value.strip().strip('"').strip("'")
    key = getpass("Clé API OpenAI (saisie masquée) : ").strip()
    if not key.startswith("sk-") or any(c.isspace() for c in key):
        raise SystemExit("Format de clé invalide.")
    model = config.get("OPENAI_MODEL") or DEFAULT_MODEL
    print(f"Test de connexion au modèle {model} (une courte requête API facturée)…")
    try:
        AssistantAI(key, model).answer(0, "Comment rejoindre le groupe ?", "Respectez les membres.",
                                       config.get("GROUP_URL", "https://t.me/Maxxie_432Hz"))
    except AIError as error:
        raise SystemExit(f"Test échoué (code {error.code}). Vérifiez la clé, la facturation et l'accès au modèle. Configuration inchangée.") from None
    updates = {"OPENAI_API_KEY": key, "OPENAI_MODEL": model,
               "OPENAI_HOURLY_LIMIT": config.get("OPENAI_HOURLY_LIMIT") or "60"}
    lines = [line for line in source.splitlines()
             if line.split("=", 1)[0].strip() not in updates]
    lines.extend(f"{name}={value}" for name, value in updates.items())
    fd, temporary = tempfile.mkstemp(prefix=".maxxie-openai-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write("\n".join(lines) + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("Connexion validée, clé enregistrée dans /etc/maxxie-bot.env (root uniquement).")
    print("Activez les réponses privées : sudo systemctl restart maxxie-bot")


if __name__ == "__main__":
    main()
