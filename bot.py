"""Bot d'accueil Maxxie 432Hz — Python 3.11+, sans dépendances externes."""
import json
import logging
import os
import sqlite3
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LOG = logging.getLogger("maxxie")


class StartVisitors:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS visitors (user_id INTEGER PRIMARY KEY)")
        self.db.commit()

    def contains(self, user_id):
        return self.db.execute("SELECT 1 FROM visitors WHERE user_id = ?", (user_id,)).fetchone() is not None

    def remember(self, user_id):
        self.db.execute("INSERT OR IGNORE INTO visitors VALUES (?)", (user_id,))
        self.db.commit()


class TelegramError(Exception):
    def __init__(self, code, retry_after=5):
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"Telegram error {code}")


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, **params):
        request = Request(self.base + method, json.dumps(params).encode(),
                          {"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=45) as response:
                result = json.load(response)
        except HTTPError as error:
            try:
                result = json.load(error)
            except (ValueError, OSError):
                raise TelegramError(error.code) from None
        except (URLError, TimeoutError, OSError):
            # Ne jamais journaliser l'URL : elle contient le jeton.
            raise TelegramError(0) from None
        if not result.get("ok"):
            raise TelegramError(result.get("error_code", 0),
                                result.get("parameters", {}).get("retry_after", 5))
        return result["result"]


class Bot:
    def __init__(self, api, username, group_id, group_url, rules, admin_ids=(), moderator_ids=(),
                 notify_chat_id=None, visitors=None):
        self.api = api
        self.username = username
        self.group_id = group_id
        self.group_url = group_url
        self.rules = rules
        self.admin_ids = frozenset(admin_ids)
        self.moderator_ids = frozenset(moderator_ids)
        self.notify_chat_id = notify_chat_id
        self.visitors = visitors

    def notify_new_visitor(self, user):
        user_id = user.get("id")
        if not self.notify_chat_id or self.visitors is None or not user_id or user.get("is_bot"):
            return
        if self.visitors.contains(user_id):
            return
        name = " ".join(user.get(key, "") for key in ("first_name", "last_name")).strip() or "Sans nom"
        username = f"@{user['username']}" if user.get("username") else "Aucun pseudonyme"
        try:
            self.api.call("sendMessage", chat_id=self.notify_chat_id,
                          text=f"Nouveau visiteur du bot 👋\n\n{name}\n{username}\n"
                               f"Identifiant Telegram : {user_id}\nA démarré le bot avec /start.")
        except TelegramError as error:
            # Un échec de notification ne bloque jamais l'accueil du visiteur.
            LOG.warning("Notification de visiteur impossible (code %s).", error.code)
            return
        self.visitors.remember(user_id)

    def promote_known(self, user):
        user_id = user.get("id")
        if user.get("is_bot") or user_id not in self.admin_ids:
            return
        member = self.api.call("getChatMember", chat_id=self.group_id, user_id=user_id)
        if member["status"] in ("creator", "left", "kicked"):
            return
        moderation = user_id in self.moderator_ids
        if member["status"] == "administrator":
            if not moderation or (member.get("can_delete_messages") and member.get("can_restrict_members")):
                return
            # Conserver les droits déjà accordés à un administrateur existant.
            rights = {key: value for key, value in member.items()
                      if (key.startswith("can_") and key != "can_be_edited") or key == "is_anonymous"}
            rights.update(can_delete_messages=True, can_restrict_members=True)
            self.api.call("promoteChatMember", chat_id=self.group_id, user_id=user_id, **rights)
            return
        if member["status"] == "restricted" and not member.get("is_member"):
            return
        self.api.call("promoteChatMember", chat_id=self.group_id, user_id=user_id,
                      can_manage_chat=True, can_delete_messages=moderation,
                      can_manage_video_chats=False, can_restrict_members=moderation,
                      can_promote_members=False, can_change_info=False,
                      can_invite_users=False, can_pin_messages=False,
                      can_manage_topics=False, is_anonymous=False)
        LOG.info("Compte autorisé promu administrateur : %s", user_id)

    def private_welcome(self, chat_id):
        self.api.call("sendMessage", chat_id=chat_id,
                      text="Bienvenue chez Maxxie 432Hz ! 🎵\n\n"
                           'Ce Bot gère le groupe "Maxxie 432Hz".\n'
                           "Il s'agit d'un groupe de partage de musiques converties en 432Hz "
                           "(Variétés Françaises et Internationales)\n\n"
                           "Choisissez une option :",
                      reply_markup={"inline_keyboard": [
                          [{"text": "Lire les règles du groupe 📜", "callback_data": "rules"}], [
                          {"text": "Rejoindre le groupe 🎵", "url": self.group_url}
                      ]]})

    def show_rules(self, chat_id):
        self.api.call("sendMessage", chat_id=chat_id, text=self.rules)

    def publish_mp3(self, message):
        chat_id = message["chat"]["id"]
        user = message.get("from", {})
        if not user.get("id") or user.get("is_bot"):
            return
        # Vérifier le statut réel, sans promouvoir un compte pour cet envoi.
        member = self.api.call("getChatMember", chat_id=self.group_id, user_id=user["id"])
        if member["status"] not in ("creator", "administrator"):
            self.api.call("sendMessage", chat_id=chat_id,
                          text="Seuls les administrateurs du groupe peuvent publier un MP3 via le bot.")
            return
        media = message.get("audio") or message.get("document") or {}
        filename = media.get("file_name", "").lower()
        if not (filename.endswith(".mp3") or (not filename and media.get("mime_type") == "audio/mpeg")):
            self.api.call("sendMessage", chat_id=chat_id,
                          text="Envoyez un fichier au format MP3 (.mp3).")
            return
        try:
            # Copier le message conserve fichier, légende et métadonnées,
            # sans téléchargement sur le VPS ni mention de transfert.
            self.api.call("copyMessage", chat_id=self.group_id,
                          from_chat_id=chat_id, message_id=message["message_id"])
        except TelegramError as error:
            if error.code not in (400, 403):
                raise
            self.api.call("sendMessage", chat_id=chat_id,
                          text="Le MP3 n'a pas pu être publié. Vérifiez que le bot peut publier dans le groupe.")
            return
        try:
            self.api.call("sendMessage", chat_id=chat_id, text="MP3 publié dans le groupe ✅")
        except TelegramError:
            # Une confirmation privée en échec ne doit pas republier le MP3.
            LOG.warning("MP3 publié, mais confirmation privée impossible.")

    def handle(self, update):
        callback = update.get("callback_query")
        if callback:
            self.api.call("answerCallbackQuery", callback_query_id=callback["id"])
            chat = callback.get("message", {}).get("chat", {})
            if callback.get("data") == "rules" and chat.get("type") == "private":
                self.show_rules(chat["id"])
            return
        membership = update.get("chat_member")
        if membership:
            if str(membership.get("chat", {}).get("id")) == self.group_id:
                self.promote_known(membership["new_chat_member"]["user"])
            return
        message = update.get("message", {})
        chat = message.get("chat", {})
        if chat.get("type") == "private":
            user = message.get("from", {})
            command = message.get("text", "").split(maxsplit=1)[0:1]
            if command and command[0].lower() in ("/start", f"/start@{self.username.lower()}"):
                self.notify_new_visitor(user)
            if "audio" in message or "document" in message:
                self.publish_mp3(message)
                return
            if message.get("text", "").split(maxsplit=1)[0:1] == ["/id"]:
                self.api.call("sendMessage", chat_id=chat["id"],
                              text=f"Votre identifiant Telegram : {user['id']}")
                return
            self.promote_known(user)
            if message.get("text", "").split() == ["/start", "rules"]:
                self.show_rules(chat["id"])
                return
            self.private_welcome(chat["id"])
        elif str(chat.get("id")) == self.group_id:
            if not message.get("sender_chat"):
                self.promote_known(message.get("from", {}))
            members = [u for u in message.get("new_chat_members", []) if not u.get("is_bot")]
            for user in members:
                self.promote_known(user)
                try:
                    # Telegram autorise l'envoi seulement si le membre a déjà
                    # ouvert le bot en privé. Aucun message de repli dans le groupe.
                    self.private_welcome(user["id"])
                except TelegramError as error:
                    if error.code not in (400, 403):
                        raise
                    LOG.info("Accueil privé impossible : conversation non ouverte ou bot bloqué.")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("BOT_TOKEN", "").strip()
    group_id = os.environ.get("GROUP_ID", "").strip()
    group_url = os.environ.get("GROUP_URL", "https://t.me/Maxxie_432Hz").strip()
    if not token or not group_id.lstrip("-").isdigit():
        raise SystemExit("Configurez BOT_TOKEN et GROUP_ID (identifiant numérique du groupe).")
    if not group_url.startswith("https://t.me/"):
        raise SystemExit("GROUP_URL doit être un lien https://t.me/ valide.")
    rules_path = Path(os.environ.get("RULES_FILE", "rules.txt"))
    rules = rules_path.read_text(encoding="utf-8").strip()
    if not rules or len(rules) > 3500:
        raise SystemExit("Le règlement doit contenir entre 1 et 3500 caractères.")
    api = Telegram(token)
    try:
        username = api.call("getMe")["username"]
        webhook = api.call("getWebhookInfo")
        if webhook.get("url"):
            raise SystemExit("Un webhook existe déjà. Désactivez-le avant de démarrer ce service.")
    except TelegramError as error:
        raise SystemExit(f"Connexion Telegram impossible (code {error.code}).") from None
    try:
        admin_ids = {int(value.strip()) for value in os.environ.get("ADMIN_USER_IDS", "").split(",") if value.strip()}
        moderator_ids = {int(value.strip()) for value in os.environ.get("MODERATOR_USER_IDS", "").split(",") if value.strip()}
        if any(value <= 0 for value in admin_ids | moderator_ids) or not moderator_ids <= admin_ids:
            raise ValueError
    except ValueError:
        raise SystemExit("Les listes doivent contenir des IDs positifs ; MODERATOR_USER_IDS doit être inclus dans ADMIN_USER_IDS.") from None
    notify_chat_id = os.environ.get("NOTIFY_CHAT_ID", "").strip()
    visitors = None
    if notify_chat_id:
        if not notify_chat_id.isdigit() or int(notify_chat_id) <= 0:
            raise SystemExit("NOTIFY_CHAT_ID doit être l'identifiant positif du destinataire privé.")
        visitors = StartVisitors(os.environ.get("VISITORS_DB", "/var/lib/maxxie-bot/visitors.sqlite3"))
    bot = Bot(api, username, group_id, group_url, rules, admin_ids, moderator_ids,
              int(notify_chat_id) if notify_chat_id else None, visitors)
    offset = 0
    LOG.info("Bot démarré : @%s", username)
    while True:
        try:
            updates = api.call("getUpdates", offset=offset, timeout=30,
                               allowed_updates=["message", "chat_member", "callback_query"])
            for update in updates:
                try:
                    bot.handle(update)
                except TelegramError as error:
                    if error.code not in (400, 403):
                        raise
                    LOG.warning("Message non envoyé (code %s).", error.code)
                offset = update["update_id"] + 1
        except TelegramError as error:
            if error.code in (401, 404, 409):
                raise SystemExit(f"Service arrêté (code Telegram {error.code}). Vérifiez le jeton et les autres instances.") from None
            LOG.warning("Telegram indisponible (code %s), nouvelle tentative.", error.code)
            time.sleep(max(1, error.retry_after))


if __name__ == "__main__":
    main()
