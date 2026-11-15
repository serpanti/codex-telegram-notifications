"""Durable local delivery and question state. Bot tokens never enter this database."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid


def state_dir():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "telegram-notifications"


class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else state_dir() / "state.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.path.parent.chmod(0o700)
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS leases (name TEXT PRIMARY KEY, owner TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, dedupe TEXT NOT NULL UNIQUE,
                method TEXT NOT NULL, payload TEXT NOT NULL, kind TEXT NOT NULL,
                request_id TEXT, created REAL NOT NULL, expires REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL, lease_until REAL, owner TEXT,
                error TEXT, message_id INTEGER, sent_at REAL
            );
            CREATE INDEX IF NOT EXISTS outbox_ready ON outbox(status, next_attempt);
            CREATE TABLE IF NOT EXISTS questions (
                id TEXT PRIMARY KEY, thread_id TEXT, options TEXT NOT NULL,
                chat_id TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', message_id INTEGER,
                answer TEXT, answered_at REAL
            );
        """)
        if os.name != "nt":
            self.path.chmod(0o600)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))

    def acquire(self, owner, now=None):
        now = time.time() if now is None else now
        with self.transaction():
            row = self.db.execute("SELECT owner, expires FROM leases WHERE name='worker'").fetchone()
            if row and row["owner"] != owner and row["expires"] > now:
                return False
            self.db.execute("INSERT OR REPLACE INTO leases VALUES ('worker', ?, ?)", (owner, now + 60))
        return True

    def heartbeat(self, owner, now=None):
        now = time.time() if now is None else now
        return self.db.execute("UPDATE leases SET expires=? WHERE name='worker' AND owner=?", (now + 60, owner)).rowcount == 1

    def worker(self):
        row = self.db.execute("SELECT owner, expires FROM leases WHERE name='worker'").fetchone()
        return dict(row) if row else None

    def release(self, owner):
        self.db.execute("DELETE FROM leases WHERE name='worker' AND owner=?", (owner,))

    def enqueue(self, method, payload, *, dedupe, kind="notification", request_id=None, ttl=86400, now=None):
        now = time.time() if now is None else now
        identifier = uuid.uuid4().hex
        self.db.execute("""INSERT OR IGNORE INTO outbox
            (id,dedupe,method,payload,kind,request_id,created,expires,next_attempt)
            VALUES (?,?,?,?,?,?,?,?,?)""", (identifier, dedupe, method, json.dumps(payload, ensure_ascii=False), kind, request_id, now, now + ttl, now))
        return self.db.execute("SELECT id FROM outbox WHERE dedupe=?", (dedupe,)).fetchone()[0]

    def claim(self, owner, now=None):
        now = time.time() if now is None else now
        with self.transaction():
            self.db.execute("UPDATE outbox SET status='queued', owner=NULL WHERE status='sending' AND lease_until<=?", (now,))
            self.db.execute("UPDATE outbox SET status='expired' WHERE status IN ('queued','sending') AND expires<=?", (now,))
            row = self.db.execute("SELECT * FROM outbox WHERE status='queued' AND next_attempt<=? ORDER BY created,id LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            self.db.execute("UPDATE outbox SET status='sending',owner=?,lease_until=?,attempts=attempts+1 WHERE id=?", (owner, now + 60, row["id"]))
            return dict(self.db.execute("SELECT * FROM outbox WHERE id=?", (row["id"],)).fetchone())

    def sent(self, job, result, now=None):
        now = time.time() if now is None else now
        message_id = result.get("message_id") if isinstance(result, dict) else None
        with self.transaction():
            updated = self.db.execute("UPDATE outbox SET status='sent',sent_at=?,message_id=?,owner=NULL,error=NULL WHERE id=? AND owner=? AND status='sending'", (now, message_id, job["id"], job["owner"])).rowcount
            if updated and job["kind"] == "ask" and message_id is not None:
                self.db.execute("UPDATE questions SET message_id=? WHERE id=?", (message_id, job["request_id"]))
                question = self.question(job["request_id"])
                if question and question["status"] != "pending":
                    self.close_buttons(question, now=now)

    def failed(self, job, code, *, retry=True, retry_after=None, now=None):
        now = time.time() if now is None else now
        delay = retry_after if retry_after is not None else min(3600, 5 * 2 ** min(job["attempts"] - 1, 10))
        status = "queued" if retry and job["attempts"] < 12 and now + delay < job["expires"] else "dead"
        updated = self.db.execute("UPDATE outbox SET status=?,next_attempt=?,error=?,owner=NULL WHERE id=? AND owner=? AND status='sending'", (status, now + max(1, delay), code, job["id"], job["owner"])).rowcount
        if updated and status == "dead" and job["kind"] == "ask":
            self.cancel(job["request_id"], "failed", now)

    def question(self, identifier):
        row = self.db.execute("SELECT * FROM questions WHERE id=?", (identifier,)).fetchone()
        return dict(row) if row else None

    def create_question(self, prompt, options, chat_id, *, thread_id=None, ttl=900, now=None):
        now = time.time() if now is None else now
        identifier = uuid.uuid4().hex
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 3500:
            raise ValueError("Question must contain 1 to 3500 characters.")
        if len(options) > 10 or any(not isinstance(item, str) or not item.strip() or len(item) > 80 for item in options):
            raise ValueError("Use at most 10 nonempty options of at most 80 characters.")
        if not 10 <= ttl <= 3600:
            raise ValueError("Question timeout must be between 10 and 3600 seconds.")
        if not str(chat_id).isdigit() or int(chat_id) <= 0:
            raise ValueError("Telegram answers require a private chat.")
        payload = {"chat_id": str(chat_id), "text": prompt + "\n\nОтветьте на это сообщение текстом или выберите кнопку.", "link_preview_options": {"is_disabled": True}}
        if options:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": item, "callback_data": f"ctn:{identifier}:{index}"}] for index, item in enumerate(options)]}
        else:
            payload["reply_markup"] = {"force_reply": True, "selective": True}
        with self.transaction():
            self.db.execute("INSERT INTO questions(id,thread_id,options,chat_id,created,expires) VALUES(?,?,?,?,?,?)", (identifier, thread_id, json.dumps(options, ensure_ascii=False), str(chat_id), now, now + ttl))
            self.enqueue("sendMessage", payload, dedupe="ask:" + identifier, kind="ask", request_id=identifier, ttl=ttl, now=now)
        return identifier

    def answer(self, identifier, text, *, chat_id, message_id, choice=None, now=None):
        now = time.time() if now is None else now
        with self.transaction():
            question = self.question(identifier)
            if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0 or not question or question["status"] != "pending" or question["expires"] <= now or question["chat_id"] != str(chat_id) or question["message_id"] != message_id:
                return False
            options = json.loads(question["options"])
            if choice is not None:
                if not isinstance(choice, int) or not 0 <= choice < len(options):
                    return False
                text = options[choice]
            if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                return False
            answer = json.dumps({"text": text, "choice": choice, "source": "telegram"}, ensure_ascii=False)
            self.db.execute("UPDATE questions SET status='answered',answer=?,answered_at=? WHERE id=?", (answer, now, identifier))
            self.close_buttons(question, now=now)
        return True

    def close_buttons(self, question, *, now=None):
        if question["message_id"] is not None and json.loads(question["options"]):
            self.enqueue("editMessageReplyMarkup", {"chat_id": question["chat_id"], "message_id": question["message_id"], "reply_markup": {"inline_keyboard": []}}, dedupe="close:" + question["id"], kind="close", now=now)

    def cancel(self, identifier, status="cancelled", now=None):
        now = time.time() if now is None else now
        with self.transaction():
            question = self.question(identifier)
            if question and question["status"] == "pending":
                self.db.execute("UPDATE questions SET status=? WHERE id=?", (status, identifier))
                self.db.execute("UPDATE outbox SET status='expired' WHERE request_id=? AND status='queued'", (identifier,))
                self.close_buttons(question, now=now)

    def expire(self, now=None):
        now = time.time() if now is None else now
        self.db.execute("UPDATE outbox SET status='expired' WHERE status='queued' AND expires<=?", (now,))
        ids = self.db.execute("SELECT id FROM questions WHERE status='pending' AND expires<=?", (now,)).fetchall()
        for row in ids:
            self.cancel(row[0], "expired", now)
        # Keep deduplication and diagnostics for one week; remove answered text after a day.
        self.db.execute("DELETE FROM outbox WHERE status IN ('sent','dead','expired') AND created<?", (now - 7 * 86400,))
        self.db.execute("UPDATE questions SET answer=NULL WHERE status!='pending' AND expires<?", (now - 86400,))
        self.db.execute("DELETE FROM questions WHERE status!='pending' AND expires<?", (now - 7 * 86400,))

    def summary(self):
        counts = {row[0]: row[1] for row in self.db.execute("SELECT status,COUNT(*) FROM outbox GROUP BY status")}
        counts["pending_questions"] = self.db.execute("SELECT COUNT(*) FROM questions WHERE status='pending'").fetchone()[0]
        return counts


def notification_key(event, kind, message):
    identifiers = [event.get("thread-id") or event.get("session_id"), event.get("turn-id") or event.get("turn_id"), event.get("tool_use_id")]
    # A thread alone does not identify an event. In particular, permission hooks
    # can ask for the same action twice in one turn without a tool-use ID.
    stable = bool(identifiers[0] and identifiers[1]) if kind == "main" else bool(identifiers[0] and identifiers[1] and identifiers[2])
    if not stable:
        return uuid.uuid4().hex
    return hashlib.sha256(json.dumps([kind, identifiers, message], ensure_ascii=True).encode()).hexdigest()
