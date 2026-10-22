"""Bot d'accueil Maxxie 432Hz — Python 3.11+, sans dépendances externes."""
import json
import logging
import os
import sqlite3
import socket
import threading
from http.client import HTTPSConnection, HTTPException
import signal
from pathlib import Path
import time
from datetime import datetime, timezone

from reliability import JournalAPI, READ_METHODS, TaskStore, TelegramError, Worker

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
        safe_to_retry = method in READ_METHODS
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
                 notify_chat_id=None, visitors=None, welcome=None, owner_user_id=None, backup_dir=None):
        self.api = api
        self.username = username
        self.group_id = group_id
        self.group_url = group_url
        self.rules = rules
        self.admin_ids = frozenset(admin_ids)
        self.moderator_ids = frozenset(moderator_ids)
        self.notify_chat_id = notify_chat_id
        self.visitors = visitors
        self.welcome = welcome if welcome is not None else Path(__file__).with_name("welcome.txt").read_text(encoding="utf-8").strip()
        self.background = None
        self.owner_user_id = owner_user_id
        self.backup_dir = Path(backup_dir) if backup_dir else None

    def action(self, step, method, **params):
        if isinstance(self.api, JournalAPI):
            return self.api.action(step, method, **params)
        return self.api.call(method, **params)

    def show_status(self, chat_id, user_id):
        if not self.owner_user_id or user_id != self.owner_user_id:
            self.action("status-denied", "sendMessage", chat_id=chat_id,
                        text="Cette commande est réservée au propriétaire du bot.")
            return
        if not isinstance(self.background, TaskStore):
            return
        counts, recent = self.background.summary()
        lines = ["État du bot", f"En attente : {counts.get('pending', 0)}",
                 f"En cours : {counts.get('running', 0)}", f"Terminées : {counts.get('done', 0)}",
                 f"Échouées : {counts.get('failed', 0)}", f"Incertaines : {counts.get('uncertain', 0)}"]
        if recent:
            lines.append("Dernières tâches à vérifier :")
            lines.extend(f"#{task_id} — {kind} : {status}" for task_id, kind, status in recent)
        marker = self.backup_dir / "latest.json" if self.backup_dir else None
        if marker and marker.exists():
            try:
                metadata = json.loads(marker.read_text(encoding="utf-8"))
                lines.append(f"Dernière sauvegarde vérifiée : {metadata['created_at']}")
                created = datetime.fromisoformat(metadata["created_at"])
                if created.tzinfo is None:
                    raise ValueError("Date sans fuseau")
                if (datetime.now(timezone.utc) - created).total_seconds() > 36 * 3600:
                    lines.append("⚠️ Sauvegarde ancienne : plus de 36 heures. Vérifiez le service de sauvegarde.")
                snapshot = metadata.get("snapshot", "")
                if not snapshot or Path(snapshot).name != snapshot or not (self.backup_dir / snapshot).is_dir():
                    lines.append("⚠️ Le dossier de la dernière sauvegarde est introuvable.")
            except (OSError, ValueError, KeyError, TypeError):
                lines.append("État de sauvegarde illisible.")
        else:
            lines.append("Aucune sauvegarde vérifiée disponible.")
        self.action("status", "sendMessage", chat_id=chat_id, text="\n".join(lines))

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
            self.action("visitor-notification", "sendMessage", chat_id=self.notify_chat_id,
                          text=f"Nouveau visiteur du bot 👋\n\n{name}\n{username}\n"
                               f"Identifiant Telegram : {user_id}\nA démarré le bot avec /start.")
        except TelegramError as error:
            if error.code == 429 or error.code == 0 or error.code >= 500:
                raise
            # Un refus permanent ne bloque jamais l'accueil du visiteur.
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
            self.action("promotion", "promoteChatMember", chat_id=self.group_id, user_id=user_id, **rights)
            return
        if member["status"] == "restricted" and not member.get("is_member"):
            return
        self.action("promotion", "promoteChatMember", chat_id=self.group_id, user_id=user_id,
                      can_manage_chat=True, can_delete_messages=moderation,
                      can_manage_video_chats=False, can_restrict_members=moderation,
                      can_promote_members=False, can_change_info=False,
                      can_invite_users=False, can_pin_messages=False,
                      can_manage_topics=False, is_anonymous=False)
        LOG.info("Compte autorisé promu administrateur : %s", user_id)

    def private_welcome(self, chat_id):
        self.action(f"welcome:{chat_id}", "sendMessage", chat_id=chat_id,
                      text=self.welcome,
                      reply_markup={"inline_keyboard": [
                          [{"text": "Lire les règles du groupe 📜", "callback_data": "rules"}], [
                          {"text": "Rejoindre le groupe 🎵", "url": self.group_url}
                      ]]})

    def show_rules(self, chat_id):
        self.action(f"rules:{chat_id}", "sendMessage", chat_id=chat_id, text=self.rules)

    def publish_mp3(self, message):
        chat_id = message["chat"]["id"]
        user = message.get("from", {})
        if not user.get("id") or user.get("is_bot"):
            return
        # Une copie confirmée reste publiée même si les droits changent ensuite.
        if isinstance(self.api, JournalAPI) and self.api.completed(
                "mp3-copy", "copyMessage", chat_id=self.group_id,
                from_chat_id=chat_id, message_id=message["message_id"]):
            self.confirm_mp3(chat_id)
            return
        # Vérifier le statut réel, sans promouvoir un compte pour cet envoi.
        member = self.api.call("getChatMember", chat_id=self.group_id, user_id=user["id"])
        if member["status"] not in ("creator", "administrator"):
            self.action("publication-denied", "sendMessage", chat_id=chat_id,
                          text="Seuls les administrateurs du groupe peuvent publier un MP3 via le bot.")
            return
        media = message.get("audio") or message.get("document") or {}
        filename = media.get("file_name", "").lower()
        if not (filename.endswith(".mp3") or (not filename and media.get("mime_type") == "audio/mpeg")):
            self.action("invalid-mp3", "sendMessage", chat_id=chat_id,
                          text="Envoyez un fichier au format MP3 (.mp3).")
            return
        try:
            # Copier le message conserve fichier, légende et métadonnées,
            # sans téléchargement sur le VPS ni mention de transfert.
            self.action("mp3-copy", "copyMessage", chat_id=self.group_id,
                          from_chat_id=chat_id, message_id=message["message_id"])
        except TelegramError as error:
            if error.code not in (400, 403):
                raise
            self.action("mp3-error", "sendMessage", chat_id=chat_id,
                          text="Le MP3 n'a pas pu être publié. Vérifiez que le bot peut publier dans le groupe.")
            return
        self.confirm_mp3(chat_id)

    def confirm_mp3(self, chat_id):
        try:
            self.action("mp3-confirmation", "sendMessage", chat_id=chat_id, text="MP3 publié dans le groupe ✅")
        except TelegramError as error:
            if error.code == 429:
                raise
            # Une confirmation privée en échec ne doit pas republier le MP3.
            LOG.warning("MP3 publié, mais confirmation privée impossible.")

    def handle(self, update):
        callback = update.get("callback_query")
        if callback:
            try:
                if isinstance(self.api, JournalAPI):
                    self.api.acknowledge_callback(callback["id"])
                else:
                    self.api.call("answerCallbackQuery", callback_query_id=callback["id"])
            except TelegramError as error:
                LOG.info("Acquittement du bouton indisponible (code %s), réponse conservée.", error.code)
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
            if command in ("/statut", f"/statut@{self.username.lower()}"):
                self.show_status(chat["id"], user.get("id"))
                return
            start = command in ("/start", f"/start@{self.username.lower()}")
            if "audio" in message or "document" in message:
                self.publish_mp3(message)
                return
            if command in ("/id", f"/id@{self.username.lower()}"):
                self.action("identity", "sendMessage", chat_id=chat["id"],
                              text=f"Votre identifiant Telegram : {user['id']}")
                return
            if not start:
                return
            if parts[1:] == ["rules"]:
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


def wait_for_startup(api, stop):
    delay = 2
    while not stop.is_set():
        try:
            username = api.call("getMe")["username"]
            if api.call("getWebhookInfo").get("url"):
                raise SystemExit("Un webhook existe déjà. Désactivez-le avant de démarrer ce service.")
            return username
        except TelegramError as error:
            if error.code not in (0, 429) and error.code < 500:
                raise SystemExit(f"Connexion Telegram refusée (code {error.code}).") from None
            wait = max(1, error.retry_after) if error.code == 429 else delay
            LOG.warning("Démarrage : Telegram indisponible (code %s), nouvelle tentative dans %ss.", error.code, wait)
            stop.wait(wait)
            delay = min(60, delay * 2)
    return None


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
    try:
        admin_ids = {int(value.strip()) for value in os.environ.get("ADMIN_USER_IDS", "").split(",") if value.strip()}
        moderator_ids = {int(value.strip()) for value in os.environ.get("MODERATOR_USER_IDS", "").split(",") if value.strip()}
        if any(value <= 0 for value in admin_ids | moderator_ids) or not moderator_ids <= admin_ids:
            raise ValueError
    except ValueError:
        raise SystemExit("Les listes doivent contenir des IDs positifs ; MODERATOR_USER_IDS doit être inclus dans ADMIN_USER_IDS.") from None
    notify_chat_id = os.environ.get("NOTIFY_CHAT_ID", "").strip()
    owner_user_id = os.environ.get("OWNER_USER_ID", "").strip() or notify_chat_id
    if owner_user_id and (not owner_user_id.isdigit() or int(owner_user_id) <= 0):
        raise SystemExit("OWNER_USER_ID doit être un identifiant Telegram positif.")
    visitors = None
    if notify_chat_id:
        if not notify_chat_id.isdigit() or int(notify_chat_id) <= 0:
            raise SystemExit("NOTIFY_CHAT_ID doit être l'identifiant positif du destinataire privé.")
        visitors = StartVisitors(os.environ.get("VISITORS_DB", "/var/lib/maxxie-bot/visitors.sqlite3"))
    welcome = Path(os.environ.get("WELCOME_FILE", str(Path(__file__).with_name("welcome.txt")))).read_text(encoding="utf-8").strip()
    if not welcome or len(welcome) > 3500:
        raise SystemExit("Le message d'accueil doit contenir entre 1 et 3500 caractères.")
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    api = Telegram(token, ipv4_only=os.environ.get("TELEGRAM_IPV4_ONLY", "1") == "1")
    store = TaskStore(os.environ.get("STATE_DB", "/var/lib/maxxie-bot/tasks.sqlite3"),
                      owner_user_id=int(owner_user_id) if owner_user_id else None,
                      trusted_media_ids=admin_ids)
    workers = []
    try:
        username = wait_for_startup(api, stop)
        if username is None:
            return
        journal = JournalAPI(api, store)
        bot = Bot(journal, username, group_id, group_url, rules, admin_ids, moderator_ids,
                  int(notify_chat_id) if notify_chat_id else None, visitors, welcome,
                  int(owner_user_id) if owner_user_id else None,
                  os.environ.get("BACKUP_DIR", "/var/lib/maxxie-bot/backups"))
        bot.background = store
        def handle_task(kind, payload):
            if kind == "update":
                bot.handle(payload)
            elif kind == "notify_new_visitor":
                bot.notify_new_visitor(payload)
            elif kind == "promote_known":
                bot.promote_known(payload)
            else:
                raise ValueError("Tâche inconnue")
        workers = [Worker(store, journal, handle_task, lane, stop) for lane in (0, 1)]
        for worker in workers:
            worker.thread.start()
        LOG.info("Bot démarré : @%s ; file persistante active.", username)
        last_cleanup = 0
        while not stop.is_set():
            if not all(worker.thread.is_alive() for worker in workers):
                raise RuntimeError("Un thread de traitement s'est arrêté")
            try:
                if store.pending_count() >= 1000:
                    stop.wait(1)  # Ralentir la réception sans abandonner de tâche.
                    continue
                updates = api.call("getUpdates", offset=store.offset(), timeout=30, limit=50,
                                   allowed_updates=["message", "chat_member", "callback_query"])
                store.ingest(updates)
                if time.monotonic() - last_cleanup > 3600:
                    store.cleanup()
                    last_cleanup = time.monotonic()
            except TelegramError as error:
                if error.code in (401, 404, 409):
                    raise SystemExit(f"Réception arrêtée (code Telegram {error.code}). Vérifiez le jeton et les autres instances.") from None
                LOG.warning("Réception Telegram indisponible (code %s).", error.code)
                stop.wait(max(1, error.retry_after))
    finally:
        stop.set()
        for worker in workers:
            worker.thread.join(timeout=20)
        if not any(worker.thread.is_alive() for worker in workers):
            store.close()
            if visitors:
                visitors.db.close()
        api.close()


if __name__ == "__main__":
    main()
