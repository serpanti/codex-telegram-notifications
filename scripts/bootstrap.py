"""Create and verify an isolated Python runtime for this skill."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import venv


SKILL_DIR = Path(__file__).resolve().parent.parent


def environment_dir():
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    return codex_home / "venvs" / "codex-telegram-notifications"


def environment_python(directory):
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(command):
    subprocess.run([str(part) for part in command], check=True)


def bootstrap(*, check=False, runtime_only=False, chat_id=None, system_site_packages=False):
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required. See the skill's references/setup.md.")

    directory = environment_dir()
    interpreter = environment_python(directory)
    marker = directory / "pyvenv.cfg"
    if not marker.is_file():
        if check:
            raise RuntimeError(f"Skill runtime is not installed: {directory}")
        if directory.exists():
            raise RuntimeError(f"Destination exists but is not a Python virtual environment: {directory}")
        directory.parent.mkdir(parents=True, exist_ok=True)
        venv.EnvBuilder(with_pip=True, system_site_packages=system_site_packages).create(directory)
    elif system_site_packages and "include-system-site-packages = true" not in marker.read_text(encoding="utf-8").lower():
        raise RuntimeError("Existing runtime does not expose system packages. Recreate it before using --system-site-packages.")

    if not interpreter.is_file():
        raise RuntimeError(f"Virtual environment has no Python interpreter: {interpreter}")

    if not check:
        run([interpreter, "-m", "pip", "install", "--disable-pip-version-check", "-r", SKILL_DIR / "requirements.txt"])

    run([interpreter, SKILL_DIR / "scripts" / "configure.py", "status"])
    run([interpreter, SKILL_DIR / "scripts" / "self_test.py"])
    print("Skill runtime is ready:", interpreter)
    if check or runtime_only:
        print("To finish setup interactively, run this bootstrap again without --check or --runtime-only.")
        return
    if not sys.stdin.isatty():
        raise RuntimeError("Bot setup needs an interactive terminal for the hidden token prompt.")
    command = [interpreter, SKILL_DIR / "scripts" / "configure.py", "install"]
    if chat_id is not None:
        command.extend(["--chat-id", chat_id])
    run(command)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify an existing runtime without installing packages")
    parser.add_argument("--runtime-only", action="store_true", help="Install and verify dependencies without prompting for a bot token")
    parser.add_argument("--chat-id", help="Known numeric private chat ID; otherwise discover it after /start")
    parser.add_argument("--system-site-packages", action="store_true", help="Expose system packages, useful for Linux KWallet with distro-provided dbus-python")
    args = parser.parse_args(argv)
    try:
        bootstrap(check=args.check, runtime_only=args.runtime_only, chat_id=args.chat_id, system_site_packages=args.system_site_packages)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            print("Bootstrap command failed. Check the Python, pip, network, and OS keychain setup.", file=sys.stderr)
        else:
            print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
