"""Codex notify and hook entry point. Notification failures never block Codex."""

import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys

from telegram_notify import notification_text, read_credential, send_message


def chain_previous(encoded, event_json):
    try:
        command = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
        if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
            return
        if len(command) > 1 and Path(command[1]).resolve() == Path(__file__).resolve():
            return
        subprocess.run([*command, event_json], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
    except Exception:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("main", "hook"))
    parser.add_argument("--hook-event", choices=("SubagentStop", "PermissionRequest", "PreToolUse"))
    parser.add_argument("--chain-command")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("event_json", nargs="?")
    args = parser.parse_args(argv)
    try:
        if args.kind == "hook":
            # Codex sends UTF-8 bytes. Windows pipe defaults can be cp1251 even
            # when the JSON labels and the Telegram HTTP request use UTF-8.
            buffer = getattr(sys.stdin, "buffer", None)
            event_json = buffer.read().decode("utf-8-sig") if buffer is not None else sys.stdin.read()
        else:
            event_json = args.event_json
    except (UnicodeError, OSError):
        return 0
    if not event_json:
        return 0
    if args.chain_command and args.kind == "main" and not args.preview:
        chain_previous(args.chain_command, event_json)
    try:
        event = json.loads(event_json)
        message = notification_text(event, args.hook_event if args.kind == "hook" else "main")
        if not message:
            return 0
        if args.preview:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            print(message)
            return 0
        credential = read_credential()
        if credential:
            send_message(credential, message)
    except Exception:
        # No event, token, URL, or exception detail may reach Codex logs.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
