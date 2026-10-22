"""Bot d'accueil Maxxie 432Hz — Python 3.11+, sans dépendances externes."""
import json
import logging
import os
import sqlite3
import socket
import threading
from http.client import HTTPSConnection, HTTPException
from queue import Queue, Full
from pathlib import Path
import time

LOG = logging.getLogger("maxxie")


class StartVisitors:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS visitors (user_id INTEGER PRIMARY KEY)")
        self.db.commit()

    def contains(self, user_id):
        with self.lock:
            return self.db.execute("SELECT 1 FROM visitors WHERE user_id = ?", (user_id,)).fetchone() is not None

    def remember(self, user_id):
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO visitors VALUES (?)", (user_id,))
            self.db.commit()


class BackgroundTasks:
    """Tâches secondaires séquentielles, sans bloquer les réponses privées."""
    def __init__(self):
        self.queue = Queue(maxsize=128)
        self.pending = set()
        self.lock = threading.Lock()
        threading.Thread(target=self._run, name="notifications", daemon=True).start()

    def submit(self, function, user):
        key = (function.__name__, user.get("id"))
        with self.lock:
            if key in self.pending:
                return
            self.pending.add(key)
            try:
                self.queue.put_nowait((key, function, dict(user)))
            except Full:
                self.pending.discard(key)
                LOG.warning("File de tâches secondaires pleine : tâche reportée à un prochain événement.")

    def _run(self):
        while True:
            key, function, user = self.queue.get()
            try:
                function(user)
            except TelegramError as error:
                LOG.warning("Tâche %s impossible (code %s).", key[0], error.code)
            except Exception as error:
                LOG.error("Tâche %s interrompue (%s).", key[0], type(error).__name__)
            finally:
                with self.lock:
                    self.pending.discard(key)
                self.queue.task_done()


def connect_ipv4(address, timeout, source_address=None):
    """Éviter une route IPv6 défaillante sans modifier le réseau du VPS."""
    host, port = address
    last_error = None
    for family, kind, protocol, _, target in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
        connection = socket.socket(family, kind, protocol)
        connection.settimeout(timeout)
        try:
            if source_address:
                connection.bind(source_address)
            connection.connect(target)
            return connection
        except OSError as error:
            last_error = error
            connection.close()
    raise last_error or OSError("Aucune adresse IPv4 disponible")


class TelegramError(Exception):
    def __init__(self, code, retry_after=5):
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"Telegram error {code}")


class Telegram:
    def __init__(self, token, ipv4_only=True):
        self.path = f"/bot{token}/"
        self.ipv4_only = ipv4_only
        self.local = threading.local()

    def close(self):
        connection = getattr(self.local, "connection", None)
        if connection:
            connection.close()
            self.local.connection = None

    def call(self, method, **params):
        started = time.monotonic()
        read_timeout = params.get("timeout", 30) + 5 if method == "getUpdates" else 8
        safe_to_retry = method in ("getMe", "getChat", "getChatMember", "getWebhookInfo", "getUpdates")
        for attempt in range(2):
            connection = getattr(self.local, "connection", None)
            if connection and time.monotonic() - getattr(self.local, "last_used", 0) > 20:
                self.close()
                connection = None
            if connection is None:
                connection = HTTPSConnection("api.telegram.org", timeout=3)
                if self.ipv4_only:
                    connection._create_connection = connect_ipv4
                self.local.connection = connection
            try:
                if connection.sock is None:
                    connection.connect()
                connection.sock.settimeout(read_timeout)
                connection.request("POST", self.path + method, json.dumps(params).encode(),
                                   {"Content-Type": "application/json"})
                response = connection.getresponse()
                body = response.read()
                self.local.last_used = time.monotonic()
                status = response.status
                if response.will_close:
                    self.close()
                result = json.loads(body)
            except (OSError, HTTPException, ValueError):
                self.close()
                if safe_to_retry and attempt == 0:
                    continue
                LOG.warning("Appel %s en échec après %.2fs.", method, time.monotonic() - started)
                raise TelegramError(0) from None
            elapsed = time.monotonic() - started
            if method != "getUpdates" and elapsed > 1:
                LOG.warning("Appel %s lent : %.2fs.", method, elapsed)
            if not isinstance(result, dict):
                raise TelegramError(0)
            if not result.get("ok"):
                raise TelegramError(result.get("error_code", status),
                                    result.get("parameters", {}).get("retry_after", 5))
            if "result" not in result:
                raise TelegramError(0)
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
        self.background = None

    def later(self, function, user):
        if self.background:
            self.background.submit(function, user)
        else:
            try:
                function(user)
            except TelegramError as error:
                LOG.warning("Tâche secondaire impossible (code %s).", error.code)

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
            try:
                self.api.call("answerCallbackQuery", callback_query_id=callback["id"])
            except TelegramError as error:
                if error.code != 400:
                    raise
                LOG.info("Bouton expiré : affichage des règles conservé.")
            chat = callback.get("message", {}).get("chat", {})
            if callback.get("data") == "rules" and chat.get("type") == "private":
                self.show_rules(chat["id"])
            return
        membership = update.get("chat_member")
        if membership:
            if str(membership.get("chat", {}).get("id")) == self.group_id:
                self.later(self.promote_known, membership["new_chat_member"]["user"])
            return
        message = update.get("message", {})
        chat = message.get("chat", {})
        if chat.get("type") == "private":
            user = message.get("from", {})
            parts = message.get("text", "").split(maxsplit=1)
            command = parts[0].lower() if parts else ""
            start = command in ("/start", f"/start@{self.username.lower()}")
            if "audio" in message or "document" in message:
                self.publish_mp3(message)
                return
            if command in ("/id", f"/id@{self.username.lower()}"):
                self.api.call("sendMessage", chat_id=chat["id"],
                              text=f"Votre identifiant Telegram : {user['id']}")
                return
            if start and parts[1:] == ["rules"]:
                self.show_rules(chat["id"])
            else:
                self.private_welcome(chat["id"])
            if start:
                self.later(self.notify_new_visitor, user)
                self.later(self.promote_known, user)
        elif str(chat.get("id")) == self.group_id:
            members = [u for u in message.get("new_chat_members", []) if not u.get("is_bot")]
            for user in members:
                self.later(self.promote_known, user)
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
    api = Telegram(token, ipv4_only=os.environ.get("TELEGRAM_IPV4_ONLY", "1") == "1")
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
    bot.background = BackgroundTasks()
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
                    if error.code in (401, 404, 409):
                        raise
                    # Ne pas rejouer une mise à jour entière : l'envoi a pu
                    # réussir avant une coupure de sa réponse HTTP.
                    LOG.warning("Mise à jour %s interrompue (code %s), sans renvoi automatique.",
                                update["update_id"], error.code)
                offset = update["update_id"] + 1
        except TelegramError as error:
            if error.code in (401, 404, 409):
                raise SystemExit(f"Service arrêté (code Telegram {error.code}). Vérifiez le jeton et les autres instances.") from None
            LOG.warning("Telegram indisponible (code %s), nouvelle tentative.", error.code)
            time.sleep(max(1, error.retry_after))


if __name__ == "__main__":
    main()
