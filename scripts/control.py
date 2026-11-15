"""Diagnose notifications, manage delivery, and ask a scoped Telegram question."""
import argparse
import base64
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
import xml.etree.ElementTree as ET

from state import Store, state_dir
from telegram_notify import api_request, excerpt, project_name, read_credential
from worker import ensure_worker

TASK_NAME = "Codex Telegram Notifications"
TASK_DESCRIPTION = "Codex Telegram Notifications background delivery"


def notifier_commands(command, depth=0):
    if depth >= 16 or not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise ValueError("Invalid notify chain")
    yield command
    if "--previous-notify" in command:
        index = command.index("--previous-notify")
        yield from notifier_commands(json.loads(command[index + 1]), depth + 1)
    if "--chain-command" in command:
        index = command.index("--chain-command")
        yield from notifier_commands(json.loads(base64.b64decode(command[index + 1], validate=True).decode("utf-8")), depth + 1)


def doctor(*, network=False):
    checks = []

    def add(name, status, detail):
        checks.append({"check": name, "status": status, "detail": detail})

    add("python", "ok" if sys.version_info >= (3, 11) else "error", f"Python {sys.version_info.major}.{sys.version_info.minor}")
    config_path = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        commands = list(notifier_commands(config.get("notify")))
        missing = [Path(command[0]).name for command in commands if not (Path(command[0]).is_file() if Path(command[0]).is_absolute() else shutil.which(command[0]))]
        routed = any(len(command) >= 3 and Path(command[1]).name == "notify.py" and command[2] == "main" and Path(command[1]).is_file() for command in commands)
        add("notify", "ok" if routed and not missing else "error", "Telegram completion route is present" if routed and not missing else "Missing Telegram route or executable; use configure.py repair-notify")
        hooks = config.get("hooks", {})
        for event in ("PermissionRequest", "PreToolUse"):
            found = any("notify.py" in str(handler.get("command", "")) for group in hooks.get(event, []) for handler in group.get("hooks", []))
            add(event, "ok" if found else "error", "Hook definition found; trust is managed in Codex /hooks" if found else "Hook missing; use configure.py install-hooks")
    except Exception:
        add("config", "error", "Cannot read a valid user config and notify chain")
    credential = None
    try:
        credential = read_credential()
        add("credential", "ok" if credential else "error", "OS credential is available" if credential else "Credential not available in this logon context")
    except Exception:
        add("credential", "error", "Cannot access the approved OS credential backend")
    database = state_dir() / "state.sqlite3"
    if database.is_file():
        connection = None
        try:
            connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
            counts = dict(connection.execute("SELECT status,COUNT(*) FROM outbox GROUP BY status"))
            add("queue", "warning" if counts.get("dead", 0) else "ok", json.dumps(counts, sort_keys=True))
            lease = connection.execute("SELECT expires FROM leases WHERE name='worker'").fetchone()
            add("worker", "ok" if lease and lease[0] > time.time() else "warning", "Worker heartbeat is current" if lease and lease[0] > time.time() else "Worker is stopped; use control.py start")
            errors = connection.execute("SELECT key,value FROM meta WHERE key IN ('credential_error','input_error') AND value!=''").fetchall()
            for key, value in errors:
                add(key, "warning", value)
        except Exception:
            add("queue", "error", "Cannot read the queue schema")
        finally:
            if connection is not None:
                connection.close()
    else:
        add("queue", "warning", "Queue has not been initialized; use control.py start")
    if os.name == "nt":
        try:
            registered = owned_service()
            add("logon_service", "ok" if registered else "warning", "User logon task is registered" if registered else "Use control.py install-service to resume queued delivery after login")
        except Exception:
            add("logon_service", "warning", "Cannot inspect the user logon task")
    if network and credential:
        try:
            api_request(credential["token"], "getMe")
            add("telegram_api", "ok", "Telegram API is reachable; no message was sent")
            webhook = api_request(credential["token"], "getWebhookInfo")
            active = not isinstance(webhook, dict) or bool(webhook.get("url"))
            add("answers", "warning" if active else "ok", "An existing webhook prevents reply polling; it was not changed" if active else "Reply polling is available for a dedicated bot")
        except Exception:
            add("telegram_api", "error", "Telegram API check failed; no message was sent")
    return {"ok": not any(check["status"] == "error" for check in checks), "checks": checks}


def start():
    with Store() as store:
        credential = read_credential()
        if not credential:
            raise RuntimeError("No OS credential is available in this logon context.")
        store.set("chat_id", credential["chat_id"])
        store.set("stop_owner", "")
        started = ensure_worker(store)
    return {"started": started}


def stop():
    with Store() as store:
        worker = store.worker()
        if worker:
            store.set("stop_owner", worker["owner"])
    return {"stop_requested": bool(worker)}


def ask(question, options, *, timeout=900, thread_id=None):
    """Return the answer only to this waiting process, never execute Telegram text."""
    credential = read_credential()
    if not credential:
        raise RuntimeError("No OS credential is available in this logon context.")
    webhook = api_request(credential["token"], "getWebhookInfo")
    if not isinstance(webhook, dict) or webhook.get("url"):
        raise RuntimeError("This bot has an active webhook; use a dedicated bot for answers.")
    question = excerpt(question, 3000)
    if not question:
        raise ValueError("Question cannot be empty.")
    options = [excerpt(option, 80) for option in options]
    prompt = f"❓ Codex: нужен ответ\nПроект: {project_name({'cwd': os.getcwd()})}\nВопрос: {question}"
    with Store() as store:
        store.set("chat_id", credential["chat_id"])
        identifier = store.create_question(prompt, options, credential["chat_id"], thread_id=thread_id or os.environ.get("CODEX_THREAD_ID"), ttl=timeout)
        try:
            ensure_worker(store)
            while True:
                store.expire()
                request = store.question(identifier)
                if request["status"] != "pending":
                    result = {"request_id": identifier, "thread_id": request["thread_id"], "status": request["status"]}
                    if request["answer"]:
                        result["answer"] = json.loads(request["answer"])
                    return result
                time.sleep(0.5)
        except BaseException:
            store.cancel(identifier)
            raise


def task_xml(interpreter, worker, sid):
    namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", namespace)
    def element(parent, name, text=None, **attrs):
        item = ET.SubElement(parent, "{" + namespace + "}" + name, attrs)
        item.text = text
        return item
    root = ET.Element("{" + namespace + "}Task", {"version": "1.2"})
    registration = element(root, "RegistrationInfo")
    element(registration, "Description", TASK_DESCRIPTION)
    triggers = element(root, "Triggers")
    logon = element(triggers, "LogonTrigger")
    element(logon, "Enabled", "true")
    element(logon, "UserId", sid)
    principals = element(root, "Principals")
    principal = element(principals, "Principal", id="Author")
    element(principal, "UserId", sid)
    element(principal, "LogonType", "InteractiveToken")
    element(principal, "RunLevel", "LeastPrivilege")
    settings = element(root, "Settings")
    for name, value in (("MultipleInstancesPolicy", "IgnoreNew"), ("DisallowStartIfOnBatteries", "false"), ("StopIfGoingOnBatteries", "false"), ("StartWhenAvailable", "true"), ("Hidden", "true"), ("ExecutionTimeLimit", "PT0S")):
        element(settings, name, value)
    restart = element(settings, "RestartOnFailure")
    element(restart, "Interval", "PT1M")
    element(restart, "Count", "3")
    actions = element(root, "Actions", Context="Author")
    execute = element(actions, "Exec")
    element(execute, "Command", str(interpreter))
    element(execute, "Arguments", subprocess.list2cmdline(["-B", str(worker), "--database", str(state_dir() / "state.sqlite3")]))
    element(execute, "WorkingDirectory", str(worker.parent))
    return ET.tostring(root, encoding="unicode", xml_declaration=False)


def parse_task_xml(data):
    # schtasks can emit an UTF-16 declaration over an 8-bit console pipe.
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in data[:80]:
        text = data.decode("utf-16")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeError:
            text = data.decode("mbcs" if os.name == "nt" else "utf-8", errors="replace")
    return ET.fromstring(text.lstrip("\ufeff"))


def owned_service():
    existing = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"], capture_output=True, timeout=5)
    if existing.returncode != 0:
        return False
    root = parse_task_xml(existing.stdout)
    description = root.find("{*}RegistrationInfo/{*}Description")
    if description is None or description.text != TASK_DESCRIPTION:
        raise RuntimeError("A task with this name belongs to another program; it was not changed.")
    return True


def install_service():
    if os.name != "nt":
        raise RuntimeError("Automatic service registration supports Windows; use start or your user service manager on macOS/Linux.")
    import csv
    owned_service()
    identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, check=True, text=True)
    sid = next(csv.reader(identity.stdout.strip().splitlines()))[1]
    interpreter = Path(sys.executable)
    if interpreter.with_name("pythonw.exe").is_file():
        interpreter = interpreter.with_name("pythonw.exe")
    with Store() as store:
        xml_path = store.path.parent / "worker-task.xml"
        xml_path.write_text(task_xml(interpreter, Path(__file__).with_name("worker.py").resolve(), sid), encoding="utf-16")
    subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"], capture_output=True, check=True)
    subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True, check=True)
    return {"service": TASK_NAME, "registered": True, "run_requested": True}


def uninstall_service():
    if os.name != "nt":
        raise RuntimeError("Automatic service registration supports Windows only.")
    if owned_service():
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, check=True)
    result = stop()
    result["registered"] = False
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    diagnostic = subparsers.add_parser("doctor", help="Read-only local checks; --network sends no messages")
    diagnostic.add_argument("--network", action="store_true")
    diagnostic.add_argument("--json", action="store_true")
    for action in ("start", "stop", "status", "retry", "install-service", "uninstall-service"):
        subparsers.add_parser(action)
    question = subparsers.add_parser("ask", help="Send a question and return its answer as JSON")
    question.add_argument("--question", required=True)
    question.add_argument("--option", action="append", default=[])
    question.add_argument("--timeout", type=int, default=900)
    question.add_argument("--thread-id")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        if args.action == "doctor":
            result = doctor(network=args.network)
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                for check in result["checks"]:
                    print(f'{check["status"].upper():7} {check["check"]}: {check["detail"]}')
            return 0 if result["ok"] else 1
        if args.action == "ask":
            result = ask(args.question, args.option, timeout=args.timeout, thread_id=args.thread_id)
        elif args.action == "start":
            result = start()
        elif args.action == "stop":
            result = stop()
        elif args.action == "install-service":
            result = install_service()
        elif args.action == "uninstall-service":
            result = uninstall_service()
        elif args.action == "retry":
            with Store() as store:
                count = store.db.execute("UPDATE outbox SET status='queued',attempts=0,next_attempt=?,error=NULL WHERE status='dead' AND expires>? AND (kind!='ask' OR request_id IN (SELECT id FROM questions WHERE status='pending'))", (time.time(), time.time())).rowcount
                ensure_worker(store)
                result = {"requeued": count}
        else:
            with Store() as store:
                result = {"queue": store.summary(), "worker": store.worker()}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("status", "answered") == "answered" else 2
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("Operation failed. Run control.py doctor --network for safe diagnostics.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
