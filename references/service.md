# Delivery, diagnostics, and Telegram answers

All commands below use the skill's isolated Python and installed scripts. Bot tokens are read only from the OS keychain, never from command-line arguments or the queue.

## Diagnose without sending a message

```text
<skill-python> <skill-dir>/scripts/control.py doctor
<skill-python> <skill-dir>/scripts/control.py doctor --network --json
```

Local checks inspect Python, user TOML, executable paths throughout the notify chain, hook definitions, credential access, the queue, the worker heartbeat, and Windows logon registration. The network option calls only `getMe` and `getWebhookInfo`. It does not send a test message, read user messages, change config, repair trust, or remove webhooks. Results omit tokens, chat content, and Telegram error descriptions. Hook trust must still be reviewed through Codex `/hooks`.

When a root notify executable is missing after an app update, use `configure.py repair-notify` and restart Codex. Diagnose keychain access in the same interactive user session as the desktop app; sandboxed shells may have a different credential context.

## Durable outbox

`notify.py` formats and filters the event, saves the message, and wakes the worker. A cached destination chat ID lets normal hooks enqueue without querying the keychain or network. The worker reads the bot credential from the keychain and refuses to redirect queued content if the configured chat has changed.

State lives in `CODEX_HOME/telegram-notifications/state.sqlite3`, using SQLite WAL and full synchronization. The bot token is not stored there. Files contain task excerpts, questions, and answers; on POSIX the directory is mode 0700 and database is mode 0600. On Windows the directory inherits the user's Codex directory permissions.

Delivery uses a worker lease and recoverable per-message claims. Network errors and server failures retry with exponential delays, capped at one hour; Telegram `retry_after` pauses all sending. There are at most 12 attempts per message. Main-turn alerts expire after 24 hours, ordinary attention alerts after 10 minutes, and questions at their requested timeout. Permanent API errors become `dead`; `control.py retry` requeues eligible failed deliveries after the cause is fixed. It does not reopen failed or expired questions.

Event IDs and message content deduplicate repeated callbacks. This is at-least-once delivery: if Telegram accepted a message but the HTTP response was lost, a retry can produce a duplicate. Sent/dead/expired rows are removed after seven days; stored answer text is cleared one day after its request expires.

```text
<skill-python> <skill-dir>/scripts/control.py status
<skill-python> <skill-dir>/scripts/control.py start
<skill-python> <skill-dir>/scripts/control.py stop
<skill-python> <skill-dir>/scripts/control.py retry
```

The worker runs hidden on Windows and detached on macOS/Linux. Only one worker holds the database lease. Stop is cooperative and never kills an arbitrary PID. A new notification or question can wake a stopped worker. To detach the feature completely, stop the worker, remove its startup registration, and remove notify/hook configuration as described in SKILL.md.

On Windows:

```text
<skill-python> <skill-dir>/scripts/control.py install-service
<skill-python> <skill-dir>/scripts/control.py uninstall-service
```

The task `Codex Telegram Notifications` runs at this user's logon with InteractiveToken and LeastPrivilege, using the stable isolated Python path. It has no password, token, or elevated run level. Install/uninstall refuse to overwrite or remove a task with an unrelated description. macOS/Linux can use `start`; automatic logon registration on those systems is left to the user's service manager.

## Ask and wait for a reply

Use only when the user has requested questions through Telegram. Example:

```text
<skill-python> <skill-dir>/scripts/control.py ask --question "Which check should run?" --option "Quick" --option "Full" --timeout 900
```

With no `--option`, the bot requests a free-text reply. With options, it shows inline buttons; replying to that same message with text also works. The question binds to a random request ID and, when present, `CODEX_THREAD_ID` or `--thread-id`. The answer is returned only to the waiting command as JSON:

```json
{"request_id":"...","thread_id":"...","status":"answered","answer":{"text":"Full","choice":1,"source":"telegram"}}
```

The worker accepts replies only from the configured private chat AND that user's ID. Buttons include a request ID and option index, and free text must be a reply to the specific bot message. It rejects another sender, the wrong message, invalid options, expired requests, and repeated answers. Answering or expiring a question queues removal of its buttons. Telegram text is data, never a shell command. Timeouts and cancellation return no approval. Permanent delivery failure ends the waiter with `status: failed`.

Timeouts are 10 to 3600 seconds. Run long waits through the normal resumable shell session and keep polling it; interrupting the command cancels its pending question. Use a dedicated bot because `getUpdates` acknowledges consumed updates; another polling client conflicts, and a webhook prevents polling. The skill reports the conflict without deleting a webhook or stealing another poller's credentials.

This flow answers questions explicitly sent by `control.py ask`. It does not answer existing `request_user_input` cards or approval windows in the desktop app. Both native question and approval responses require a separate App Server connection to the owning request.
