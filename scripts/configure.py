"""Interactive setup and Codex config snippets for Telegram notifications."""

import argparse
import base64
import getpass
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timezone

from telegram_notify import discover_private_chats, read_credential, remove_credential, save_credential, secure_keyring, valid_chat_id, valid_token

HOOKS = (
    ("SubagentStop", ".*"),
    ("PermissionRequest", ".*"),
    ("PreToolUse", ".*request_user_input(_async)?$"),
)


def python_command(*args):
    return [sys.executable, str(Path(__file__).with_name("notify.py")), *args]


def config_path():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"


def existing_notify(config_path):
    with config_path.open("rb") as stream:
        config = tomllib.load(stream)
    command = config.get("notify")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise RuntimeError("No valid top-level notify array found in config.toml.")
    if len(command) >= 2 and Path(command[1]).resolve() == Path(__file__).with_name("notify.py").resolve():
        raise RuntimeError("Existing notify already points to this skill.")
    return command


def print_config(chain_existing):
    command = python_command("main")
    if chain_existing:
        previous = existing_notify(config_path())
        encoded = base64.b64encode(json.dumps(previous).encode("utf-8")).decode("ascii")
        command.extend(["--chain-command", encoded])
    print("notify = " + json.dumps(command, ensure_ascii=False))


def hook_command(event):
    parts = python_command("hook", "--hook-event", event)
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


def hook_blocks(event, matcher):
    return "\n".join((
        f"[[hooks.{event}]]",
        "matcher = " + json.dumps(matcher),
        f"[[hooks.{event}.hooks]]",
        'type = "command"',
        "command = " + json.dumps(hook_command(event), ensure_ascii=False),
        "async = true",
        "timeout = 15",
    ))


def append_hooks(text):
    parsed = tomllib.loads(text)
    existing = parsed.get("hooks", {})
    if not isinstance(existing, dict):
        raise RuntimeError("Existing hooks configuration is invalid; config was not changed.")
    missing = []
    for event, matcher in HOOKS:
        groups = existing.get(event, [])
        if not isinstance(groups, list):
            raise RuntimeError(f"Existing {event} hooks are invalid; config was not changed.")
        if not any(
            isinstance(group, dict) and any(
                isinstance(handler, dict) and handler.get("command") == hook_command(event)
                for handler in group.get("hooks", [])
            ) for group in groups
        ):
            missing.append(hook_blocks(event, matcher))
    if not missing:
        return text
    newline = "\r\n" if "\r\n" in text else "\n"
    separator = newline + newline if text.strip() else ""
    blocks = [block.replace("\n", newline) for block in missing]
    return text.rstrip("\r\n") + separator + (newline + newline).join(blocks) + newline


def write_config(path, new_text):
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(path.name + ".bak-" + stamp)
        shutil.copy2(path, backup)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, prefix=".config-telegram-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(new_text)
        if path.exists():
            temporary.chmod(stat.S_IMODE(path.stat().st_mode))
        elif os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path, backup


def install_config():
    """Preserve all other TOML settings and chain any previous root notifier."""
    import tomlkit

    path = config_path()
    original_text = path.read_bytes().decode("utf-8") if path.exists() else ""
    document = tomlkit.parse(original_text)
    previous = document.get("notify")
    if previous is not None:
        command = list(previous) if not isinstance(previous, str) else []
        if not command or not all(isinstance(part, str) for part in command):
            raise RuntimeError("Existing top-level notify is not a string array; config was not changed.")
        notify_path = Path(__file__).with_name("notify.py").resolve()
        if len(command) >= 3 and Path(command[1]).resolve() == notify_path and command[2] == "main":
            if "--chain-command" in command:
                index = command.index("--chain-command")
                try:
                    command = json.loads(base64.b64decode(command[index + 1], validate=True).decode("utf-8"))
                except (IndexError, ValueError) as exc:
                    raise RuntimeError("Existing Telegram notifier has an invalid chain; config was not changed.") from exc
            else:
                command = None
        previous = command

    updated = python_command("main")
    if previous:
        encoded = base64.b64encode(json.dumps(previous).encode("utf-8")).decode("ascii")
        updated.extend(["--chain-command", encoded])
    if document.get("notify") is None:
        newline = "\r\n" if "\r\n" in original_text else "\n"
        new_text = "notify = " + json.dumps(updated, ensure_ascii=False) + newline + original_text
    else:
        document["notify"] = updated
        new_text = tomlkit.dumps(document)
    new_text = append_hooks(new_text)
    with io.BytesIO(new_text.encode("utf-8")) as stream:
        parsed = tomllib.load(stream)
    if parsed.get("notify") != updated:
        raise RuntimeError("Generated config did not validate; config was not changed.")

    return write_config(path, new_text)


def install_hooks():
    path = config_path()
    original_text = path.read_bytes().decode("utf-8") if path.exists() else ""
    new_text = append_hooks(original_text)
    if new_text == original_text:
        return path, None
    tomllib.loads(new_text)
    return write_config(path, new_text)


def print_hooks():
    for event, matcher in HOOKS:
        print(hook_blocks(event, matcher) + "\n")


def configure(chat_id):
    secure_keyring()  # Fail before prompting if no OS-backed keychain is available.
    token = getpass.getpass("Telegram bot token (hidden): ")
    if not valid_token(token):
        raise RuntimeError("The bot token has an invalid format.")
    if chat_id is None:
        chats = discover_private_chats(token)
        if not chats:
            raise RuntimeError("No pending private chat found. Send /start to your bot and retry.")
        print("Private chats (message contents are not shown):")
        for candidate, name in sorted(chats.items()):
            print(f"  {candidate}  {name}")
        chat_id = input("Enter the numeric chat ID to receive task details: ").strip()
        if chat_id not in chats:
            raise RuntimeError("Selected chat ID is not in the pending private chat updates.")
    if not valid_chat_id(chat_id):
        raise RuntimeError("Use a numeric chat ID.")
    save_credential(token, chat_id)
    print("Telegram credential saved in the OS keychain. No message was sent.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "install-hooks", "configure", "status", "print-config", "print-hooks", "remove"))
    parser.add_argument("--chat-id")
    parser.add_argument("--chain-existing", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "install":
            import tomlkit
            tomlkit.parse(config_path().read_bytes().decode("utf-8") if config_path().exists() else "")
            configure(args.chat_id)
            path, backup = install_config()
            print(f"Codex notifications enabled in {path}.")
            if backup:
                print(f"Previous config saved at {backup}.")
            print("Review and trust the new hooks in Codex /hooks after restart.")
            print("Restart Codex to apply the change.")
        elif args.action == "install-hooks":
            path, backup = install_hooks()
            print(f"Telegram hooks configured in {path}.")
            if backup:
                print(f"Previous config saved at {backup}.")
            print("Restart Codex, then review and trust these hooks in /hooks.")
        elif args.action == "configure":
            configure(args.chat_id)
        elif args.action == "status":
            credential = read_credential()
            print("Telegram credential is configured in the OS keychain." if credential else "Telegram credential is not configured.")
        elif args.action == "print-config":
            print_config(args.chain_existing)
        elif args.action == "print-hooks":
            print_hooks()
        else:
            remove_credential()
            print("Telegram credential removed. Remove Codex config entries separately.")
    except Exception as exc:
        # Never print an exception containing an API URL or credential.
        print(str(exc) if isinstance(exc, (RuntimeError, ValueError)) else "Operation failed; check Python keyring and OS keychain access.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
