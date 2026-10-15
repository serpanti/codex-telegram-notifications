---
name: codex-telegram-notifications
description: Send contextual main-turn completion and human-attention notifications through a Telegram bot on Windows, macOS, and Linux, suppressing subagent completions and automatic approval reviews. Use when the user wants Telegram alerts that identify the task or summarize the result.
metadata:
  short-description: Contextual Codex alerts in Telegram
---

# Codex Telegram Notifications

This skill uses one Python implementation on Windows, macOS, and Linux. A completed main turn sends a structured message with the project, latest task, and result. Hooks send separate messages for approval routed to the user and structured user input. Subagent completions and automatic approval reviews are silent. The task and result are excerpts, not generated summaries. A completed turn does not prove the task succeeded.

## Privacy and prerequisites

- Task details sent by this skill leave the computer for Telegram. Before enabling it, explain this to the user and use a private chat they control. Common token and password patterns are masked on a best-effort basis; do not put secrets in task text because other formats may pass through.
- Follow [references/setup.md](references/setup.md) for Python installation on each OS, the OS credential service, and the common bootstrap. `scripts/bootstrap.py` creates a dedicated virtual environment, installs `keyring` and `tomlkit` from `requirements.txt`, checks the system backend, runs offline tests, and configures Codex. The scripts refuse unsupported and plaintext credential backends; there is no environment-variable or file fallback.
- Never ask the user to paste a bot token into chat, a repository, `config.toml`, or a command-line parameter. Bootstrap prompts for it locally without echoing it, then stores the token and selected chat ID together in the OS credential store. Other processes in the same user account may still access that store; use a dedicated bot token and rotate it if exposed.
- The user creates a bot with BotFather and sends `/start` in the intended private chat. When the chat ID is unknown, setup reads pending bot updates and asks the user to confirm the destination. Setup sends no message.
- When the user asks to set up this skill, include Python and dependency setup in the work. Do not send a live test message without the user's authorization.

## Configure

Use [references/setup.md](references/setup.md) when installing or troubleshooting the runtime. First check for Python 3.11+; install it when needed. The user creates a bot and sends `/start` in the intended private chat. Then **one interactive command** completes the normal setup: `python <skill-dir>/scripts/bootstrap.py` (use `python3` on macOS/Linux, or an absolute 3.11+ interpreter path). Bootstrap installs dependencies in its own environment, asks for the token in a hidden prompt, discovers and confirms the private chat ID, backs up `config.toml`, and adds the root `notify` command plus notification hooks. It chains a prior `notify` automatically and preserves unrelated TOML settings. Use `--chat-id <numeric-id>` only if discovery is unavailable. Restart Codex and review and trust the new hook definitions through `/hooks`.

When acting as an agent, perform all setup steps possible without the secret; use `bootstrap.py --runtime-only` if the user must run the final interactive command in their own terminal. Do not ask for the token in chat or put it into a command. Bootstrap's `--check` verifies an existing runtime without installing or prompting. The generated `notify` command uses the isolated Python's absolute path. For an existing installation, `configure.py install-hooks` adds missing hook definitions without re-entering the token or changing the existing `notify`. Other repair actions are `configure`, `status`, `print-config`, `print-hooks`, and `remove`.

After restarting Codex, inspect the effective root `notify`. Codex may wrap this command with `codex-computer-use` and place the Telegram command in `--previous-notify`; this preserves forwarding rather than replacing the notifier. Check that the outer executable exists: an app runtime update can leave a versioned path pointing to a removed executable, preventing completion notifications while stdin hooks still work. Run `<skill-python> <skill-dir>/scripts/configure.py repair-notify` to back up the config and restore the Telegram entry point without prompting for credentials. It unwraps removed Computer Use runtimes and preserves other prior notifiers; restart Codex to reload the changed root `notify`. On Windows, Credential Manager is tied to the user's logon context. A `status` check from an isolated agent shell can report no credential even when the normal interactive user session has it. In that case, check `status` in the same interactive session used for setup and confirm delivery with a normal completed turn before changing the credential or config.

The notifier uses the Git root name when `cwd` is inside a repository, otherwise the working directory name. It removes known attachment and browser metadata wrappers from the task excerpt, includes at most 450 task characters and 900 result characters, and labels missing fields explicitly. Other notification types are ignored.

A token alone cannot identify a chat. Discovery reads at most 100 pending updates **without an offset**, so it does not confirm or discard them. It cannot work with an active webhook, another polling client consuming updates, or no recent private message. Never delete a webhook just to discover an ID. Telegram keeps undelivered updates for at most 24 hours.

## Approval and input hooks

The installer adds `PermissionRequest` and `PreToolUse` hooks. Legacy `SubagentStop` handlers are harmless silent no-ops and need not be removed from existing configurations. For an existing installation, run `<skill-python> <skill-dir>/scripts/configure.py install-hooks`. Review and trust new definitions through Codex `/hooks`; until trusted, Codex skips them. `print-hooks` shows the exact definitions. Hook inputs arrive through stdin as UTF-8 bytes; decode the binary stream explicitly rather than relying on the Windows locale, which can turn Cyrillic into mojibake. `PreToolUse` covers structured `request_user_input` and `request_user_input_async`; ordinary assistant questions have no separate event, but the main-turn notification includes the final answer.

`PermissionRequest` happens before approval routing completes. The notifier reads the active turn's `approvals_reviewer` from the session transcript and sends only when it is explicitly `user`. `auto_review`, missing context, and a mismatched turn are silent; do not infer the reviewer from global config because the app can override it per chat. This suppresses automatic reviews, including denials where the agent can still find a safer path. A denial requiring user input is covered when the agent asks a structured question or finishes its main turn with that request. The hook cannot observe another permission hook's eventual allow/deny decision or app-level Computer Use prompts. The scripts make no approval decision. If a Codex release skips asynchronous hooks, make only those hook definitions synchronous after observing the issue. Updating script contents with unchanged hook commands applies on their next invocation.

## Check or remove

- `<skill-python> <skill-dir>/scripts/configure.py status` checks whether the credential is present without printing it. Run it in the same user/keychain context as Codex before enabling notifications.
- To stop notifications, remove the skill's `notify` line and hook definitions from `config.toml`, restoring the prior `notify` command from the backup. Restart Codex.
- `<skill-python> <skill-dir>/scripts/configure.py remove` deletes the credential after configuration is detached. Revoke an exposed token with BotFather.

Official references: [Codex notifications](https://learn.chatgpt.com/docs/config-file/config-advanced#notifications), [Codex hooks](https://learn.chatgpt.com/docs/hooks), [Telegram Bot API](https://core.telegram.org/bots/api#getupdates), [Python keyring](https://keyring.readthedocs.io/en/stable/).
