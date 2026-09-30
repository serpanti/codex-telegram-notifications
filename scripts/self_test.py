"""Offline checks; no real credentials or Telegram calls."""

import base64
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import configure
import notify
import bootstrap as installer
import telegram_notify as core


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.saved = None

        class Keyring:
            def get_password(_, service, account):
                return self.saved

            def set_password(_, service, account, value):
                self.saved = value

            def delete_password(_, service, account):
                self.saved = None

        self.ring = Keyring()
        self.patcher = patch.object(core, "secure_keyring", return_value=self.ring)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_credential_round_trip(self):
        core.save_credential("123:offline-token", "23456")
        self.assertEqual(core.read_credential()["chat_id"], "23456")
        self.assertEqual(core.read_credential()["token"], "123:offline-token")
        core.remove_credential()
        self.assertIsNone(core.read_credential())

    def test_invalid_token_cannot_enter_credential_or_request_url(self):
        for token in ("", "https://example.test/path", "123:bad/token", "123:bad?query", "123:bad token"):
            with self.subTest(token=token):
                with self.assertRaisesRegex(ValueError, "invalid format"):
                    core.save_credential(token, "23456")
                with self.assertRaisesRegex(RuntimeError, "invalid format"):
                    core.api_request(token, "getMe")
        self.assertIsNone(self.saved)

    def test_notification_redacts_common_secrets(self):
        bot_token = "123456789:" + "A" * 35
        event = {
            "type": "agent-turn-complete",
            "input-messages": [f"Настрой бота с токеном {bot_token} и password=secret123"],
            "last-assistant-message": "Ключ sk-" + "B" * 24 + " установлен; Bearer " + "C" * 24,
        }
        message = core.notification_text(event, "main")
        for secret in (bot_token, "secret123", "sk-" + "B" * 24, "C" * 24):
            self.assertNotIn(secret, message)
        self.assertIn("Настрой бота", message)
        self.assertIn("[REDACTED]", message)

    def test_backend_allowlist(self):
        class PlaintextBackend:
            pass

        class FakeKeyring:
            @staticmethod
            def get_keyring():
                return PlaintextBackend()

        with patch.dict(sys.modules, {"keyring": FakeKeyring}):
            self.patcher.stop()
            try:
                with self.assertRaisesRegex(RuntimeError, "No approved OS credential backend"):
                    core.secure_keyring()
            finally:
                self.patcher.start()

    def test_discovery_and_webhook(self):
        calls = []

        def transport(request, timeout):
            calls.append(request.full_url)
            result = {"url": ""} if request.full_url.endswith("getWebhookInfo") else [
                {"message": {"chat": {"id": 23456, "type": "private", "first_name": "Two"}}},
                {"message": {"chat": {"id": -1001, "type": "supergroup"}}},
            ]
            return FakeResponse(json.dumps({"ok": True, "result": result}).encode())

        self.assertEqual(core.discover_private_chats("123:offline", transport), {"23456": "Two"})
        self.assertEqual(len(calls), 2)
        self.assertNotIn("offset=", calls[1])

        def webhook(request, timeout):
            return FakeResponse(json.dumps({"ok": True, "result": {"url": "https://example.test/hook"}}).encode())

        with self.assertRaisesRegex(RuntimeError, "webhook"):
            core.discover_private_chats("123:offline", webhook)

    def test_context_and_request(self):
        event = {"type": "agent-turn-complete", "input-messages": ["Old request", "Сделай уведомление с контекстом"], "last-assistant-message": "Готово."}
        message = core.notification_text(event, "main")
        self.assertIn("Сделай уведомление", message)
        self.assertIn("Готово.", message)
        self.assertNotIn("Old request", message)
        requests = []

        def transport(request, timeout):
            requests.append(request)
            return FakeResponse(b'{"ok":true,"result":{}}')

        core.send_message({"token": "123:offline", "chat_id": "23456"}, message, transport)
        self.assertEqual(requests[0].get_method(), "POST")
        payload = json.loads(requests[0].data)
        self.assertEqual(payload["chat_id"], "23456")
        self.assertTrue(payload["link_preview_options"]["is_disabled"])

    def test_structured_notification_uses_git_project_and_clean_task(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root) / "sample-project"
            child = project / "src"
            child.mkdir(parents=True)
            (project / ".git").mkdir()
            event = {
                "type": "agent-turn-complete",
                "cwd": str(child),
                "input-messages": ["# Files mentioned by the user:\n\n## image.png\nImage attachment: true\n\n## My request:\nИсправь уведомления"],
                "last-assistant-message": "Формат обновлён.",
            }
            message = core.notification_text(event, "main")
            self.assertEqual(message.splitlines(), [
                "📌 Codex: ход завершён",
                "Проект: sample-project",
                "Задача: Исправь уведомления",
                "Итог: Формат обновлён.",
            ])
            self.assertNotIn("image.png", message)
        pasted_notification = "Задача: # Files mentioned by the user: ...\nИтог: Старый результат.\nВот пример текста, который приходит; исправь формат."
        self.assertEqual(core.task_excerpt(pasted_notification), "Вот пример текста, который приходит; исправь формат.")

    def test_attention_notifications_include_project_and_action(self):
        approval = {"hook_event_name": "PermissionRequest", "cwd": "project", "tool_input": {"description": "Разрешить запись"}}
        message = core.notification_text(approval, "PermissionRequest")
        self.assertIn("Проект: project", message)
        self.assertIn("Действие: Разрешить запись", message)
        self.assertIn("Ожидается: решение в Codex", message)
        question = {"hook_event_name": "PreToolUse", "cwd": "project", "tool_name": "request_user_input_async", "tool_input": {"questions": [{"title": "Какой вариант выбрать?"}]}}
        message = core.notification_text(question, "PreToolUse")
        self.assertIn("Вопрос: Какой вариант выбрать?", message)
        self.assertIn("Ожидается: ответ в Codex", message)

    def test_hook_context_and_preview(self):
        event = {"hook_event_name": "PermissionRequest", "tool_input": {"description": "Разрешить проверку"}}
        self.assertIn("Разрешить проверку", core.notification_text(event, "PermissionRequest"))
        script = __import__("pathlib").Path(notify.__file__)
        process = subprocess.run([sys.executable, str(script), "hook", "--hook-event", "PermissionRequest", "--preview"], input=json.dumps(event), text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0)
        self.assertIn("Разрешить проверку", process.stdout)

    def test_existing_notifier_chained(self):
        old = [sys.executable, "old_notifier.py", "flag"]
        encoded = base64.b64encode(json.dumps(old).encode()).decode()
        with patch.object(notify.subprocess, "run") as runner:
            notify.chain_previous(encoded, '{"type":"agent-turn-complete"}')
        self.assertEqual(runner.call_args.args[0], [*old, '{"type":"agent-turn-complete"}'])

    def test_config_snippets(self):
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            configure.print_config(False)
            configure.print_hooks()
        self.assertIn("notify = [", output.getvalue())
        self.assertIn("[[hooks.PermissionRequest]]", output.getvalue())

    def test_bootstrap_creates_isolated_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "venv"

            def create(_):
                directory.mkdir()
                (directory / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
                interpreter = installer.environment_python(directory)
                interpreter.parent.mkdir(parents=True)
                interpreter.touch()

            with patch.object(installer, "environment_dir", return_value=directory), patch.object(installer.venv, "EnvBuilder") as builder, patch.object(installer, "run") as run, patch("sys.stdout", new_callable=io.StringIO):
                builder.return_value.create.side_effect = create
                installer.bootstrap(runtime_only=True)
                self.assertEqual(run.call_count, 3)
                self.assertIn("pip", run.call_args_list[0].args[0])
                installer.bootstrap(check=True)
                self.assertEqual(run.call_count, 5)
                fake_tty = type("FakeTTY", (), {"isatty": lambda self: True})()
                with patch.object(installer.sys, "stdin", fake_tty):
                    installer.bootstrap(chat_id="23456")
                self.assertEqual(run.call_count, 9)
                self.assertEqual(run.call_args.args[0][-3:], ["install", "--chat-id", "23456"])

    @unittest.skipUnless(importlib.util.find_spec("tomlkit"), "tomlkit is installed by bootstrap")
    def test_config_preserves_settings_and_previous_notifier(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.toml"
            path.write_text('# keep this comment\ntheme = "dark"\nnotify = ["old-notifier", "flag"]\n', encoding="utf-8")
            with patch.dict("os.environ", {"CODEX_HOME": root}):
                configured, backup = configure.install_config()
                self.assertEqual(configured, path)
                self.assertIn('# keep this comment', path.read_text(encoding="utf-8"))
                self.assertEqual(backup.read_text(encoding="utf-8"), '# keep this comment\ntheme = "dark"\nnotify = ["old-notifier", "flag"]\n')
                updated = tomllib.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(updated["theme"], "dark")
                self.assertEqual(set(updated["hooks"]), {"SubagentStop", "PermissionRequest", "PreToolUse"})
                command = updated["notify"]
                self.assertEqual(json.loads(base64.b64decode(command[-1])), ["old-notifier", "flag"])
                configure.install_config()
                again = tomllib.loads(path.read_text(encoding="utf-8"))["notify"]
                self.assertEqual(json.loads(base64.b64decode(again[-1])), ["old-notifier", "flag"])
                configure.install_hooks()
                final = tomllib.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(final["notify"], again)
                self.assertTrue(all(len(groups) == 1 for groups in final["hooks"].values()))


if __name__ == "__main__":
    unittest.main()
