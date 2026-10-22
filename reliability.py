"""File SQLite et journal des actions Telegram, partagés entre les threads."""
import hashlib
import json
import logging
import sqlite3
import threading
import time
import fcntl

LOG = logging.getLogger("maxxie")
READ_METHODS = {"getMe", "getChat", "getChatMember", "getWebhookInfo", "getUpdates"}


class TelegramError(Exception):
    def __init__(self, code, retry_after=5):
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"Telegram error {code}")


class UncertainAction(Exception):
    """Telegram a peut-être accepté l'envoi : ne pas le répéter."""


class TaskStore:
    def __init__(self, path, command_limit=5, command_window=10, owner_user_id=None,
                 media_limit=20, trusted_media_ids=()):
        self.lock = threading.RLock()
        self.instance_lock = None
        self.command_limit, self.command_window = command_limit, command_window
        self.owner_user_id = owner_user_id
        self.media_limit = media_limit
        self.trusted_media_ids = frozenset(trusted_media_ids)
        if str(path) != ":memory:":
            self.instance_lock = open(str(path) + ".lock", "a", encoding="utf-8")
            try:
                fcntl.flock(self.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.instance_lock.close()
                raise RuntimeError("Une autre instance utilise déjà cette base de tâches.") from None
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value INTEGER);
            CREATE TABLE IF NOT EXISTS command_limits (user_id INTEGER PRIMARY KEY, started REAL, count INTEGER);
            CREATE TABLE IF NOT EXISTS media_limits (user_id INTEGER PRIMARY KEY, started REAL, count INTEGER);
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY, update_id INTEGER UNIQUE, kind TEXT NOT NULL,
                payload TEXT NOT NULL, dedupe TEXT, lane INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                ready REAL NOT NULL DEFAULT 0, updated REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS task_ready ON tasks(lane, status, ready);
            CREATE UNIQUE INDEX IF NOT EXISTS active_task ON tasks(dedupe)
                WHERE dedupe IS NOT NULL AND status IN ('pending', 'running');
            CREATE TABLE IF NOT EXISTS actions (
                task_id INTEGER NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                result TEXT, code INTEGER, PRIMARY KEY(task_id, fingerprint));
        """)
        if "request" not in {row[1] for row in self.db.execute("PRAGMA table_info(actions)")}:
            self.db.execute("ALTER TABLE actions ADD COLUMN request TEXT")
        # Une interruption entre l'envoi et son enregistrement laisse un résultat
        # incertain. Les étapes précédentes confirmées restent dédoublonnées.
        with self.db:
            self.db.execute("UPDATE actions SET status='uncertain' WHERE status='sending'")
            self.db.execute("UPDATE tasks SET status='pending' WHERE status='running'")

    def close(self):
        with self.lock:
            self.db.close()
            if self.instance_lock:
                self.instance_lock.close()
                self.instance_lock = None

    def summary(self):
        with self.lock:
            counts = dict(self.db.execute("SELECT status,count(*) FROM tasks GROUP BY status"))
            recent = self.db.execute("SELECT id,kind,status FROM tasks WHERE status IN ('failed','uncertain') "
                                     "ORDER BY updated DESC LIMIT 5").fetchall()
            return counts, recent

    def prepare_request(self, task_id, step, method, params):
        fingerprint = hashlib.sha256(("step:" + step).encode()).hexdigest()
        legacy = hashlib.sha256(json.dumps([method, params], sort_keys=True).encode()).hexdigest()
        with self.lock, self.db:
            row = self.db.execute("SELECT request,status FROM actions WHERE task_id=? AND fingerprint=?",
                                  (task_id, fingerprint)).fetchone()
            if row is None:
                # Compatibilité avec le journal de la version précédente.
                self.db.execute("UPDATE actions SET fingerprint=? WHERE task_id=? AND fingerprint=?",
                                (fingerprint, task_id, legacy))
                row = self.db.execute("SELECT request,status FROM actions WHERE task_id=? AND fingerprint=?",
                                      (task_id, fingerprint)).fetchone()
            if row and row[0]:
                saved = json.loads(row[0])
                if saved[0] != method or any(saved[1].get(key) != params.get(key)
                                             for key in ("chat_id", "user_id", "from_chat_id", "message_id")):
                    raise PermissionError("La destination ou l'identité de l'action a changé")
                if method == "promoteChatMember" and row[1] == "pending":
                    # Un refus explicite 429 n'a pas appliqué les anciens droits.
                    # Utiliser ceux recalculés depuis la configuration actuelle.
                    self.db.execute("UPDATE actions SET request=? WHERE task_id=? AND fingerprint=?",
                                    (json.dumps([method, params]), task_id, fingerprint))
                    return fingerprint, method, params
                return fingerprint, saved[0], saved[1]
            request = json.dumps([method, params])
            self.db.execute("INSERT INTO actions(task_id,fingerprint,status,request) VALUES (?,?,'pending',?) "
                            "ON CONFLICT(task_id,fingerprint) DO UPDATE SET request=excluded.request",
                            (task_id, fingerprint, request))
            return fingerprint, method, params

    def offset(self):
        with self.lock:
            row = self.db.execute("SELECT value FROM metadata WHERE key='offset'").fetchone()
            return row[0] if row else 0

    def ingest(self, updates):
        """Stocker les événements AVANT de les confirmer auprès de Telegram."""
        with self.lock, self.db:
            offset = self.offset()
            for update in updates:
                if update["update_id"] < offset:
                    continue
                callback = update.get("callback_query", {})
                message = callback.get("message", {}) if callback else update.get("message", {})
                chat = message.get("chat", {})
                user_id = (callback if callback else message).get("from", {}).get("id", chat.get("id"))
                media = "audio" in message or "document" in message
                limited = False
                if chat.get("type") == "private" and user_id != self.owner_user_id:
                    table, limit = None, self.command_limit
                    if callback or "text" in message:
                        table = "command_limits"
                    elif media and user_id not in self.trusted_media_ids:
                        table, limit = "media_limits", self.media_limit
                    if table:
                        now = time.time()
                        row = self.db.execute(f"SELECT started,count FROM {table} WHERE user_id=?", (user_id,)).fetchone()
                        if row and now - row[0] < self.command_window:
                            limited = row[1] >= limit
                            if not limited:
                                self.db.execute(f"UPDATE {table} SET count=count+1 WHERE user_id=?", (user_id,))
                        else:
                            self.db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?,1)", (user_id, now))
                if limited:
                    offset = max(offset, update["update_id"] + 1)
                    continue
                self.db.execute("INSERT OR IGNORE INTO tasks(update_id,kind,payload,lane,updated) VALUES (?,?,?,?,?)",
                                (update["update_id"], "update", json.dumps(update), 0, time.time()))
                offset = max(offset, update["update_id"] + 1)
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('offset',?)", (offset,))

    def submit(self, function, user):
        kind = function.__name__
        if kind not in ("notify_new_visitor", "promote_known"):
            raise ValueError("Tâche secondaire inconnue")
        # Pas de copie des messages : seules les informations utiles à l'accueil.
        payload = {key: user[key] for key in ("id", "first_name", "last_name", "username", "is_bot") if key in user}
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO tasks(kind,payload,dedupe,lane,updated) VALUES (?,?,?,?,?)",
                            (kind, json.dumps(payload), f"{kind}:{user.get('id')}", 1, time.time()))

    def claim(self, lane):
        with self.lock, self.db:
            row = self.db.execute("SELECT id,kind,payload,attempts FROM tasks WHERE lane=? "
                                  "AND status='pending' AND ready<=? ORDER BY id LIMIT 1", (lane, time.time())).fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE tasks SET status='running',attempts=attempts+1,updated=? WHERE id=?",
                            (time.time(), row[0]))
            return {"id": row[0], "kind": row[1], "payload": json.loads(row[2]), "attempts": row[3] + 1}

    def finish(self, task_id, status="done"):
        with self.lock, self.db:
            # Effacer les données du visiteur dès que la tâche est terminée.
            self.db.execute("UPDATE tasks SET status=?,payload='{}',updated=? WHERE id=?",
                            (status, time.time(), task_id))
            self.db.execute("UPDATE actions SET request=NULL WHERE task_id=?", (task_id,))

    def retry(self, task, delay):
        if task["attempts"] >= 8:
            self.finish(task["id"], "failed")
            LOG.error("Tâche %s abandonnée après 8 tentatives.", task["id"])
            return
        with self.lock, self.db:
            self.db.execute("UPDATE tasks SET status='pending',ready=?,updated=? WHERE id=?",
                            (time.time() + max(1, delay), time.time(), task["id"]))

    def pending_count(self):
        with self.lock:
            return self.db.execute("SELECT count(*) FROM tasks WHERE status IN ('pending','running')").fetchone()[0]

    def cleanup(self):
        # Garder le diagnostic 30 jours. Les updates plus anciens restent exclus
        # par l'offset persistant, même une fois leur journal nettoyé.
        with self.lock, self.db:
            self.db.execute("DELETE FROM command_limits WHERE started<?", (time.time() - 86400,))
            self.db.execute("DELETE FROM media_limits WHERE started<?", (time.time() - 86400,))
            self.db.execute("DELETE FROM actions WHERE task_id IN (SELECT id FROM tasks "
                            "WHERE status NOT IN ('pending','running') AND updated<?)", (time.time() - 30 * 86400,))
            self.db.execute("DELETE FROM tasks WHERE status NOT IN ('pending','running') AND updated<?",
                            (time.time() - 30 * 86400,))

    def begin_action(self, task_id, fingerprint):
        with self.lock, self.db:
            row = self.db.execute("SELECT status,result,code FROM actions WHERE task_id=? AND fingerprint=?",
                                  (task_id, fingerprint)).fetchone()
            if row and row[0] == "done":
                return True, json.loads(row[1])
            if row and row[0] in ("sending", "uncertain"):
                raise UncertainAction
            if row and row[0] == "failed":
                raise TelegramError(row[2])
            self.db.execute("INSERT INTO actions(task_id,fingerprint,status) VALUES (?,?,'sending') "
                            "ON CONFLICT(task_id,fingerprint) DO UPDATE SET status='sending',result=NULL,code=NULL",
                            (task_id, fingerprint))
            return False, None

    def action_result(self, task_id, fingerprint, status, result=None, code=None):
        with self.lock, self.db:
            self.db.execute("UPDATE actions SET status=?,result=?,code=? WHERE task_id=? AND fingerprint=?",
                            (status, json.dumps(result), code, task_id, fingerprint))


class JournalAPI:
    def __init__(self, api, store):
        self.api = api
        self.store = store
        self.local = threading.local()

    def call(self, method, **params):
        if method in READ_METHODS:
            return self.api.call(method, **params)
        task_id = getattr(self.local, "task_id", None)
        if task_id is None:
            raise RuntimeError("Action hors d'une tâche persistante")
        fingerprint = hashlib.sha256(json.dumps([method, params], sort_keys=True).encode()).hexdigest()
        return self._execute(task_id, fingerprint, method, params)

    def action(self, step, method, **params):
        task_id = getattr(self.local, "task_id", None)
        if task_id is None:
            raise RuntimeError("Action hors d'une tâche persistante")
        fingerprint, method, params = self.store.prepare_request(task_id, step, method, params)
        return self._execute(task_id, fingerprint, method, params)

    def completed(self, step, method, **params):
        task_id = getattr(self.local, "task_id", None)
        if task_id is None:
            raise RuntimeError("Action hors d'une tâche persistante")
        fingerprint, _, _ = self.store.prepare_request(task_id, step, method, params)
        with self.store.lock:
            row = self.store.db.execute("SELECT status FROM actions WHERE task_id=? AND fingerprint=?",
                                        (task_id, fingerprint)).fetchone()
        return row is not None and row[0] == "done"

    def acknowledge_callback(self, callback_id):
        # Acquittement éphémère, sans effet sur l'envoi substantiel des règles.
        return self.api.call("answerCallbackQuery", callback_query_id=callback_id)

    def _execute(self, task_id, fingerprint, method, params):
        done, result = self.store.begin_action(task_id, fingerprint)
        if done:
            return result
        try:
            result = self.api.call(method, **params)
        except TelegramError as error:
            if error.code == 429:
                self.store.action_result(task_id, fingerprint, "pending", code=429)
                raise
            if error.code == 0 or error.code >= 500:
                self.store.action_result(task_id, fingerprint, "uncertain", code=error.code)
                raise UncertainAction from None
            self.store.action_result(task_id, fingerprint, "failed", code=error.code)
            raise
        # N'enregistrer ni texte, ni profil complet de la réponse Telegram.
        saved = {"message_id": result.get("message_id")} if isinstance(result, dict) else result
        self.store.action_result(task_id, fingerprint, "done", saved)
        return result


class Worker:
    def __init__(self, store, api, handler, lane, stop):
        self.store, self.api, self.handler = store, api, handler
        self.lane, self.stop = lane, stop
        self.thread = threading.Thread(target=self.run, name=f"tasks-{lane}", daemon=True)

    def process_one(self):
        task = self.store.claim(self.lane)
        if task is None:
            return False
        self.api.local.task_id = task["id"]
        try:
            self.handler(task["kind"], task["payload"])
        except UncertainAction:
            self.store.finish(task["id"], "uncertain")
            LOG.error("Tâche %s : résultat d'envoi incertain, pas de renvoi automatique. Vérifiez Telegram.", task["id"])
        except TelegramError as error:
            if error.code == 429 or error.code == 0 or error.code >= 500:
                self.store.retry(task, error.retry_after if error.code == 429 else min(60, 2 ** task["attempts"]))
            else:
                self.store.finish(task["id"], "failed")
            LOG.warning("Tâche %s : erreur Telegram %s.", task["id"], error.code)
        except Exception as error:
            self.store.finish(task["id"], "failed")
            LOG.error("Tâche %s : erreur %s.", task["id"], type(error).__name__)
        else:
            self.store.finish(task["id"])
        finally:
            self.api.local.task_id = None
        return True

    def run(self):
        try:
            while not self.stop.is_set():
                if not self.process_one():
                    self.stop.wait(0.1)
        finally:
            self.api.api.close()
