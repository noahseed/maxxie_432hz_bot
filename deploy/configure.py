"""Configuration interactive sur le VPS : sudo python3 configure.py."""
from getpass import getpass
import os
from pathlib import Path
import sys

sys.path.insert(0, "/opt/maxxie-bot")
from bot import Telegram, TelegramError


def main():
    if os.geteuid() != 0:
        raise SystemExit("Exécutez ce script avec sudo.")
    target = Path("/etc/maxxie-bot.env")
    if target.exists():
        raise SystemExit("La configuration existe déjà. Modifiez-la avec sudo nano /etc/maxxie-bot.env.")
    token = getpass("Jeton BotFather (saisie masquée) : ").strip()
    if not token or any(c.isspace() for c in token):
        raise SystemExit("Jeton invalide.")
    api = Telegram(token)
    try:
        identity = api.call("getMe")
        if identity.get("username", "").lower() != "maxxie_432hz_bot":
            raise SystemExit("Ce jeton ne correspond pas à @Maxxie_432Hz_Bot.")
        group = api.call("getChat", chat_id="@Maxxie_432Hz")
        if group.get("type") not in ("group", "supergroup"):
            raise SystemExit("Le lien Maxxie_432Hz ne correspond pas à un groupe.")
        membership = api.call("getChatMember", chat_id=group["id"], user_id=identity["id"])
        if membership.get("status") in ("left", "kicked"):
            raise SystemExit("Ajoutez d'abord le bot au groupe.")
    except TelegramError as error:
        raise SystemExit(f"Vérification Telegram impossible (code {error.code}). Vérifiez le jeton et l'ajout du bot au groupe.") from None
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(f"BOT_TOKEN={token}\nGROUP_ID={group['id']}\n"
                     "GROUP_URL=https://t.me/Maxxie_432Hz\nRULES_FILE=/opt/maxxie-bot/rules.txt\n")
    print("Configuration enregistrée. Vérifiez le règlement avant de démarrer le service.")


if __name__ == "__main__":
    main()
