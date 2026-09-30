# Install the skill runtime

The skill needs Python 3.11 or newer, `venv` with pip, and an unlocked OS credential service. Install Python first if it is absent; then one interactive `scripts/bootstrap.py` command creates a private virtual environment, installs `keyring` and `tomlkit` from `requirements.txt`, checks the selected credential backend, runs offline tests, prompts for the token, discovers the private chat, and updates Codex configuration after saving a backup. The virtual environment lives at `$CODEX_HOME/venvs/codex-telegram-notifications` or `~/.codex/venvs/codex-telegram-notifications`, outside the skill folder, so copying the skill to another computer does not copy the environment.

## Windows

1. In **cmd.exe** or PowerShell, run `python --version`. If Python is missing or older than 3.11, install the [Python Install Manager from python.org](https://www.python.org/downloads/windows/) and open a new terminal. Run `python --version` again. A current Codex bundled Python may also work, but its path can change after updates; prefer a normal user installation for a persistent notification command.
2. Run `python "<skill-dir>\scripts\bootstrap.py"`. This invokes pip only inside the skill's virtual environment; it does not modify the Python used by other projects. The command begins with `python` in both cmd.exe and PowerShell, so no PowerShell `&` operator is needed.
3. If the backend check fails, run bootstrap in the same signed-in desktop session that runs Codex. Windows Credential Manager can be unavailable in a sandbox or a different logon session.

The resulting interpreter normally is `%USERPROFILE%\.codex\venvs\codex-telegram-notifications\Scripts\python.exe`. Use the exact path printed by bootstrap if `CODEX_HOME` is set.

## macOS

1. Run `python3 --version`. If it is missing or older than 3.11, install the current [official macOS Python package](https://www.python.org/downloads/macos/) and open a new terminal. For a python.org installation, run the provided `Install Certificates.command` if HTTPS certificate validation fails. Do not modify Apple's system Python.
2. Run `python3 "<skill-dir>/scripts/bootstrap.py"`. If more than one Python is installed, use the full path of the desired 3.11+ interpreter instead of `python3`.
3. Permit macOS Keychain access if prompted. Run setup in the same account that runs Codex.

The resulting interpreter normally is `~/.codex/venvs/codex-telegram-notifications/bin/python`.

## Linux

1. Run `python3 --version`. If it is missing or older than 3.11, install a supported 3.11+ Python and its `venv` package using your distribution's package manager. For Debian/Ubuntu where that version is provided, the typical packages are `python3`, `python3-venv`, and `gnome-keyring`. If `python3 -m venv` reports that `ensurepip` is unavailable, install the matching `python3-venv` package. Do not install keyring into the distribution-managed Python; bootstrap creates a virtual environment.
2. Ensure the desktop has an unlocked Secret Service provider (often GNOME Keyring) or KWallet in the **same D-Bus user session** as Codex. Run `python3 "<skill-dir>/scripts/bootstrap.py"`.
3. If KWallet requires distribution-provided `dbus-python`, install that package (often `python3-dbus`) and create the virtual environment with `python3 "<skill-dir>/scripts/bootstrap.py" --system-site-packages`. Use the distribution's Python that can import the package. If a failed bootstrap already created an isolated environment, remove only that environment after checking its path, then rerun with this flag. See the [keyring Linux notes](https://keyring.readthedocs.io/en/stable/#installation-linux).

Headless Linux needs a running D-Bus session and an unlocked Secret Service daemon. The script does not start or unlock one automatically. Use `--check` later to verify the environment without installing anything.

## Finish configuration on any OS

Create the bot with BotFather and send `/start` from the intended private chat. Run the single bootstrap command for your OS above, then type the token at the hidden prompt and select the private chat ID shown. The token must **not** be a command-line parameter: parameters can appear in process listings and shell history. If you already know the ID, add `--chat-id <numeric-id>`; the token is still entered at the prompt. Bootstrap stores the credential in the OS keychain, backs up user `config.toml`, preserves and chains an existing root `notify`, and adds hooks for approval, input, and subagent alerts. It sends no Telegram message. Restart Codex, then review and trust the new hooks through `/hooks`.

For an existing setup that already sends completion alerts, run `<skill-python> <skill-dir>/scripts/configure.py install-hooks` to add the missing hooks without entering the token again. Run `configure.py print-hooks` to inspect them. For later verification, run `python "<skill-dir>/scripts/bootstrap.py" --check`; this checks the existing environment without installing packages or requesting a token. An agent can run `--runtime-only` beforehand to install dependencies, leaving only the final interactive token prompt for the user.

Official references: [Python on Windows](https://docs.python.org/3/using/windows.html), [Python on macOS](https://docs.python.org/3/using/mac.html), [Python on Unix](https://docs.python.org/3/using/unix.html), [Python venv](https://docs.python.org/3/library/venv.html), [keyring](https://keyring.readthedocs.io/en/stable/).
