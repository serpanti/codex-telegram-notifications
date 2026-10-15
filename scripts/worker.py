"""One local worker delivers the durable outbox and receives private-chat answers."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from state import Store, notification_key
from telegram_notify import TelegramError, api_request, read_credential


def ensure_worker(store):
    lease = store.worker()
    if lease and lease["expires"] > time.time():
        return False
    interpreter = Path(sys.executable)
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "cwd": str(Path(__file__).parent)}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        pythonw = interpreter.with_name("pythonw.exe")
        if pythonw.is_file():
            interpreter = pythonw
    else:
        options["start_new_session"] = True
    subprocess.Popen([str(interpreter), "-B", str(Path(__file__).resolve()), "--database", str(store.path)], **options)
    return True


def enqueue_notification(event, kind, message):
    with Store() as store:
        target = store.get("chat_id")
        if not target:
            credential = read_credential()
            if not credential:
                return False
            target = credential["chat_id"]
            store.set("chat_id", target)
        store.enqueue("sendMessage", {"chat_id": target, "text": message, "link_preview_options": {"is_disabled": True}}, dedupe=notification_key(event, kind, message), ttl=86400 if kind == "main" else 600)
        ensure_worker(store)
    return True


def deliver_one(store, owner, credential, *, api=api_request, now=None):
    now = time.time() if now is None else now
    if now < float(store.get("send_after", "0")):
        return False
    job = store.claim(owner, now)
    if not job:
        return False
    payload = json.loads(job["payload"])
    if job["method"] not in ("sendMessage", "editMessageReplyMarkup"):
        store.failed(job, "unsupported_method", retry=False, now=now)
        return True
    if str(payload.get("chat_id")) != credential["chat_id"]:
        store.failed(job, "destination_changed", retry=False, now=now)
        return True
    if job["kind"] == "ask":
        question = store.question(job["request_id"])
        if not question or question["status"] != "pending" or question["expires"] <= now:
            store.failed(job, "question_closed", retry=False, now=now)
            return True
    try:
        result = api(credential["token"], job["method"], body=payload)
        if job["method"] == "sendMessage" and (not isinstance(result, dict) or not isinstance(result.get("message_id"), int)):
            raise TelegramError("sendMessage")
        store.sent(job, result, now)
        store.set("last_delivery", now)
    except TelegramError as error:
        store.failed(job, "network" if error.code is None else f"http_{error.code}", retry=error.transient, retry_after=error.retry_after, now=now)
        if error.retry_after is not None:
            store.set("send_after", now + error.retry_after)
    except Exception:
        # Keep arbitrary exceptions, API URLs, tokens, and payloads out of logs.
        store.failed(job, "sender_error", now=now)
    return True


def handle_update(store, update, credential, *, api=api_request, now=None):
    now = time.time() if now is None else now
    if not isinstance(update, dict):
        return False
    callback = update.get("callback_query")
    callback = callback if isinstance(callback, dict) else None
    message = callback.get("message") if callback else update.get("message")
    if not isinstance(message, dict):
        return False
    sender = callback.get("from") if callback else message.get("from")
    chat = message.get("chat")
    target = credential["chat_id"]
    if not isinstance(sender, dict) or not isinstance(chat, dict) or chat.get("type") != "private" or str(chat.get("id")) != target or str(sender.get("id")) != target or sender.get("is_bot") is not False:
        return False
    if callback:
        match = re.fullmatch(r"ctn:([a-f0-9]{32}):([0-9]{1,2})", str(callback.get("data", "")))
        if not match:
            return False
        accepted = store.answer(match[1], "", chat_id=target, message_id=message.get("message_id"), choice=int(match[2]), now=now)
        identifier = callback.get("id")
        if isinstance(identifier, str):
            try:
                api(credential["token"], "answerCallbackQuery", body={"callback_query_id": identifier, "text": "Ответ передан Codex" if accepted else "Запрос уже закрыт или недоступен"})
            except Exception:
                pass
        return accepted
    reply = message.get("reply_to_message")
    if not isinstance(reply, dict) or not isinstance(reply.get("message_id"), int):
        return False
    row = store.db.execute("SELECT id FROM questions WHERE message_id=? AND chat_id=? AND status='pending'", (reply["message_id"], target)).fetchone()
    if not row:
        return False
    return store.answer(row[0], message.get("text"), chat_id=target, message_id=reply["message_id"], now=now)


def poll_answers(store, credential, *, api=api_request, now=None):
    now = time.time() if now is None else now
    if not store.db.execute("SELECT 1 FROM questions WHERE status='pending' AND expires>? LIMIT 1", (now,)).fetchone():
        return
    if not credential["chat_id"].isdigit() or int(credential["chat_id"]) <= 0:
        store.set("input_error", "private_chat_required")
        return
    if now < float(store.get("poll_after", "0")):
        return
    try:
        webhook = api(credential["token"], "getWebhookInfo")
        if not isinstance(webhook, dict) or webhook.get("url"):
            store.set("input_error", "webhook_active")
            store.set("poll_after", now + 60)
            return
        body = {"timeout": 3, "limit": 100, "allowed_updates": ["message", "callback_query"]}
        offset = store.get("update_offset")
        if offset is not None:
            body["offset"] = int(offset)
        updates = api(credential["token"], "getUpdates", body=body)
        if not isinstance(updates, list):
            raise ValueError("Invalid updates response")
        for update in updates:
            if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
                continue
            update_id = update["update_id"]
            if offset is not None and update_id < int(offset):
                continue
            handle_update(store, update, credential, api=api, now=now)
            # Persist AFTER handling: replay after a crash cannot change an answered request.
            offset = str(update_id + 1)
            store.set("update_offset", offset)
        store.set("input_error", "")
        store.set("poll_after", now + 1)
    except TelegramError as error:
        store.set("input_error", "poll_conflict" if error.code == 409 else "poll_failed")
        store.set("poll_after", now + (error.retry_after or 30))
    except Exception:
        store.set("input_error", "poll_failed")
        store.set("poll_after", now + 30)


def run(database=None, *, once=False):
    owner = uuid.uuid4().hex
    with Store(database) as store:
        if not store.acquire(owner):
            return 0
        try:
            while True:
                if store.get("stop_owner") == owner or not store.heartbeat(owner):
                    return 0
                store.expire()
                try:
                    credential = read_credential()
                except Exception:
                    credential = None
                if credential:
                    store.set("credential_error", "")
                    store.set("chat_id", credential["chat_id"])
                    for _ in range(5):
                        store.heartbeat(owner)
                        if not deliver_one(store, owner, credential):
                            break
                    store.heartbeat(owner)
                    poll_answers(store, credential)
                else:
                    store.set("credential_error", "credential_unavailable")
                if once:
                    return 0
                time.sleep(2)
        finally:
            store.release(owner)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        return run(args.database, once=args.once)
    except KeyboardInterrupt:
        return 0
    except Exception:
        return 1  # Diagnostics are available through control.py doctor.


if __name__ == "__main__":
    raise SystemExit(main())
