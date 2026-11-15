"""Shared, cross-platform logic for Codex Telegram notifications."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

SERVICE = "codex-telegram-notifications"
ACCOUNT = "bot"
ALLOWED_BACKENDS = {
    "win32": {"keyring.backends.Windows.WinVaultKeyring"},
    "darwin": {"keyring.backends.macOS.Keyring"},
    "linux": {
        "keyring.backends.SecretService.Keyring",
        "keyring.backends.kwallet.DBusKeyring",
        "keyring.backends.kwallet.DBusKeyringKWallet4",
    },
}
TOKEN_PATTERN = re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{1,256}")
SENSITIVE_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_-])[0-9]{6,20}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}(?![A-Za-z0-9_-])"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}", re.IGNORECASE),
    re.compile(r"\b(?:password|passwd|api[_-]?key|token|secret)\s*[:=]\s*[^\s,;]{4,}", re.IGNORECASE),
)


def secure_keyring():
    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError("Install keyring in this Python: python -m pip install keyring") from exc
    backend = keyring.get_keyring()
    identity = f"{type(backend).__module__}.{type(backend).__name__}"
    allowed = ALLOWED_BACKENDS.get(sys.platform, set())
    if identity not in allowed:
        raise RuntimeError(
            f"No approved OS credential backend for {sys.platform} ({identity}). "
            "Configure the OS keychain; plaintext and custom backends are refused."
        )
    return keyring


def valid_chat_id(value):
    return isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value) is not None


def valid_token(value):
    return isinstance(value, str) and TOKEN_PATTERN.fullmatch(value) is not None


def redact_sensitive(value):
    for pattern in SENSITIVE_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value


def read_credential():
    raw = secure_keyring().get_password(SERVICE, ACCOUNT)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not valid_token(data.get("token")) or not valid_chat_id(data.get("chat_id")):
            raise ValueError("invalid credential")
        return data
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Stored Telegram credential is invalid.") from exc


def save_credential(token, chat_id):
    if not valid_token(token):
        raise ValueError("Bot token has an invalid format.")
    if not valid_chat_id(chat_id):
        raise ValueError("Use a numeric chat ID.")
    secure_keyring().set_password(SERVICE, ACCOUNT, json.dumps({"token": token, "chat_id": chat_id}))


def remove_credential():
    ring = secure_keyring()
    if ring.get_password(SERVICE, ACCOUNT) is not None:
        ring.delete_password(SERVICE, ACCOUNT)


class TelegramError(RuntimeError):
    def __init__(self, method, code=None, retry_after=None):
        super().__init__(f"Telegram {method} failed; check configuration and connectivity.")
        self.code = code
        self.retry_after = retry_after
        self.transient = code is None or code == 408 or code == 429 or code >= 500


def api_request(token, method, *, body=None, query=None, transport=None, timeout=8):
    if not valid_token(token):
        raise RuntimeError("Telegram bot token has an invalid format.")
    url = f"https://api.telegram.org/bot{token}/{method}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json; charset=utf-8"} if data is not None else {}, method="POST" if data is not None else "GET")
    try:
        opener = transport or urllib.request.urlopen
        with opener(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            result = json.loads(error.read(65536))
        except (ValueError, OSError):
            result = {}
        if not isinstance(result, dict):
            result = {}
        result["ok"] = False
        result.setdefault("error_code", error.code)
    except Exception:
        raise TelegramError(method) from None
    if not isinstance(result, dict) or result.get("ok") is not True:
        code = result.get("error_code") if isinstance(result, dict) else None
        code = code if isinstance(code, int) and not isinstance(code, bool) else None
        parameters = result.get("parameters") if isinstance(result, dict) else None
        retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
        retry_after = min(86400, max(1, retry_after)) if isinstance(retry_after, int) else None
        raise TelegramError(method, code, retry_after)
    return result.get("result")


def discover_private_chats(token, transport=None):
    webhook = api_request(token, "getWebhookInfo", transport=transport)
    if not isinstance(webhook, dict):
        raise RuntimeError("Unexpected Telegram webhook response.")
    if webhook.get("url"):
        raise RuntimeError("This bot has an active webhook; getUpdates discovery is unavailable.")
    updates = api_request(token, "getUpdates", query={"limit": 100, "timeout": 0}, transport=transport)
    if not isinstance(updates, list):
        raise RuntimeError("Unexpected Telegram updates response.")
    chats = {}
    for update in updates:
        if not isinstance(update, dict):
            continue
        message = update.get("message")
        chat = message.get("chat") if isinstance(message, dict) else None
        if not isinstance(chat, dict) or chat.get("type") != "private":
            continue
        chat_id = str(chat.get("id", ""))
        if not valid_chat_id(chat_id):
            continue
        name = " ".join(str(chat.get(k, "")) for k in ("first_name", "last_name")).strip()
        name = "".join(c for c in name if c.isprintable())[:80]
        username = chat.get("username")
        if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            name = f"{name} @{username}".strip()
        chats[chat_id] = name
    return chats


def excerpt(value, limit):
    if not isinstance(value, str):
        return ""
    clean = redact_sensitive(" ".join(value.split()))
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def project_name(event):
    cwd = event.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        return "Не указан"
    directory = Path(cwd).expanduser()
    if not directory.is_absolute():
        return excerpt(directory.name, 80) or "Не указан"
    try:
        for candidate in (directory, *directory.parents):
            if (candidate / ".git").exists():
                return excerpt(candidate.name, 80) or "Не указан"
    except OSError:
        pass
    return excerpt(directory.name, 80) or "Не указан"


def task_excerpt(value):
    if not isinstance(value, str):
        return ""
    clean = re.sub(r"<in-app-browser-context\b[^>]*>.*?</in-app-browser-context>", "", value, flags=re.DOTALL | re.IGNORECASE)
    if "## My request:" in clean:
        clean = clean.rsplit("## My request:", 1)[1]
    elif clean.lstrip().startswith("# Files mentioned by the user:"):
        return "Сообщение с вложением"
    clean = re.sub(r"<image\b[^>]*>.*?</image>", "", clean, flags=re.DOTALL | re.IGNORECASE)
    lines = clean.strip().splitlines()
    if lines and lines[0].startswith("Задача:"):
        for index in range(len(lines) - 1, 0, -1):
            if lines[index].startswith("Итог:") and any(line.strip() for line in lines[index + 1:]):
                clean = "\n".join(lines[index + 1:])
                break
    return excerpt(clean, 450)


def session_context(event, *, include_turn=False):
    """Read routing metadata only; never put transcript contents in an alert.

    The app can override config.toml per chat, so the recorded turn context is
    the authority for the reviewer. Missing metadata does not prove a human wait.
    """
    transcript = event.get("transcript_path")
    if isinstance(transcript, str) and transcript:
        path = Path(transcript)
    else:
        session_id = event.get("thread-id") or event.get("session_id")
        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            return {}, {}
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        try:
            path = next((home / "sessions").glob(f"**/rollout-*-{session_id}.jsonl"), None)
            if path is None:
                path = next((home / "archived_sessions").glob(f"**/rollout-*-{session_id}.jsonl"), None)
        except OSError:
            return {}, {}
        if path is None:
            return {}, {}
    metadata, context = {}, {}
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                # Skip assistant text and tool outputs without decoding them.
                prefix = line[:160]
                if '"session_meta"' not in prefix and (not include_turn or '"turn_context"' not in prefix):
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                    continue
                if record.get("type") == "session_meta":
                    metadata = record["payload"]
                    if not include_turn:
                        break
                elif include_turn and record.get("type") == "turn_context":
                    context = record["payload"]
    except (OSError, UnicodeError):
        return {}, {}
    return metadata, context


def is_subagent(event):
    metadata, _ = session_context(event)
    source = metadata.get("source", event.get("source"))
    return isinstance(source, dict) and "subagent" in source


def approval_is_for_user(event):
    _, context = session_context(event, include_turn=True)
    if event.get("turn_id") and event["turn_id"] != context.get("turn_id"):
        return False
    return context.get("approvals_reviewer") == "user" and context.get("approval_policy") not in (None, "never")


def notification_text(event, kind):
    if not isinstance(event, dict):
        return None
    project = project_name(event)
    if kind == "main":
        if event.get("type") != "agent-turn-complete" or is_subagent(event):
            return None
        inputs = event.get("input-messages", [])
        task = ""
        if isinstance(inputs, list):
            for item in inputs:
                task = task_excerpt(item) or task
        answer = excerpt(event.get("last-assistant-message"), 900)
        lines = ["📌 Codex: ход завершён", f"Проект: {project}", f"Задача: {task or 'Не указана'}", f"Итог: {answer or 'Ответ не указан'}"]
        return "\n".join(lines)
    if event.get("hook_event_name") != kind:
        return None
    if kind == "SubagentStop":
        # Older installations still invoke this hook. Keep it a silent no-op.
        return None
    if kind == "PermissionRequest":
        if not approval_is_for_user(event):
            return None
        tool = event.get("tool_input")
        description = excerpt(tool.get("description") if isinstance(tool, dict) else None, 450) or excerpt(event.get("tool_name"), 160) or "Действие не указано"
        return "\n".join(("⚠️ Codex: требуется разрешение", f"Проект: {project}", f"Действие: {description}", "Ожидается: решение в Codex"))
    if kind == "PreToolUse":
        tool_name = event.get("tool_name")
        if not isinstance(tool_name, str) or not re.search(r"(^|[._])request_user_input(_async)?$", tool_name):
            return None
        tool = event.get("tool_input")
        questions = tool.get("questions") if isinstance(tool, dict) else None
        first = questions[0] if isinstance(questions, list) and questions else None
        question = excerpt((first.get("question") or first.get("title")) if isinstance(first, dict) else None, 450) or "Вопрос не указан"
        return "\n".join(("❓ Codex: нужен ответ", f"Проект: {project}", f"Вопрос: {question}", "Ожидается: ответ в Codex"))
    return None


def send_message(credential, message, transport=None):
    return api_request(credential["token"], "sendMessage", body={"chat_id": credential["chat_id"], "text": message, "link_preview_options": {"is_disabled": True}}, transport=transport)
