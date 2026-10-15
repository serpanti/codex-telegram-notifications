"""Offline integration tests: durable delivery, private replies, and safe diagnosis."""
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error
import xml.etree.ElementTree as ET

import configure
import control
import notify
from state import Store, notification_key
import telegram_notify as core
import worker


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "state.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(self.store.__exit__, None, None, None)
        self.credential = {"token": "123:offline-token", "chat_id": "23456"}

    def enqueue(self, dedupe="event-one", now=1000, **kwargs):
        return self.store.enqueue("sendMessage", {"chat_id": "23456", "text": "Готово"}, dedupe=dedupe, now=now, **kwargs)

    def request(self, options=None, thread_id="thread-one", now=1000):
        identifier = self.store.create_question("Какой вариант?", options if options is not None else ["Быстрый", "Полный"], "23456", thread_id=thread_id, ttl=60, now=now)
        worker.deliver_one(self.store, "worker-one", self.credential, api=Mock(return_value={"message_id": 41}), now=now)
        return identifier

    def callback(self, identifier, choice=1, *, sender=23456, message_id=41):
        return {"update_id": 7, "callback_query": {"id": "callback-one", "from": {"id": sender, "is_bot": False}, "data": f"ctn:{identifier}:{choice}", "message": {"chat": {"id": 23456, "type": "private"}, "message_id": message_id}}}

    def text_reply(self, message_id=41, text="Полный вариант"):
        return {"update_id": 8, "message": {"from": {"id": 23456, "is_bot": False}, "chat": {"id": 23456, "type": "private"}, "text": text, "reply_to_message": {"message_id": message_id}}}

    def test_queue_survives_reopen_and_retries_network_failure(self):
        self.enqueue()
        failing = Mock(side_effect=core.TelegramError("sendMessage"))
        worker.deliver_one(self.store, "one", self.credential, api=failing, now=1000)
        self.assertEqual(self.store.summary().get("queued"), 1)
        with Store(self.path) as reopened:
            sender = Mock(return_value={"message_id": 50})
            self.assertFalse(worker.deliver_one(reopened, "two", self.credential, api=sender, now=1004))
            sender.assert_not_called()
            self.assertTrue(worker.deliver_one(reopened, "two", self.credential, api=sender, now=1005))
            self.assertEqual(reopened.summary().get("sent"), 1)
            self.assertFalse(worker.deliver_one(reopened, "two", self.credential, api=sender, now=1006))
            sender.assert_called_once()
        for row in self.store.db.execute("SELECT payload,error FROM outbox"):
            self.assertNotIn(self.credential["token"], str(tuple(row)))

    def test_worker_lease_and_recoverable_delivery_claim(self):
        self.assertTrue(self.store.acquire("one", 1000))
        with Store(self.path) as other:
            self.assertFalse(other.acquire("two", 1001))
            self.enqueue()
            first = self.store.claim("one", 1000)
            self.assertIsNone(other.claim("two", 1001))
            self.assertTrue(other.acquire("two", 1061))
            self.assertFalse(self.store.heartbeat("one", 1061))
            recovered = other.claim("two", 1061)
            self.assertEqual(first["id"], recovered["id"])
            self.assertEqual(recovered["attempts"], 2)
            self.store.sent(first, {"message_id": 90}, 1062)
            self.assertEqual(other.summary()["sending"], 1)

    def test_duplicate_events_are_enqueued_once_without_dropping_unidentified_events(self):
        event = {"thread-id": "thread-one", "turn-id": "turn-one"}
        key = notification_key(event, "main", "Готово")
        self.enqueue(key)
        self.enqueue(notification_key(event, "main", "Готово"))
        self.assertEqual(self.store.summary()["queued"], 1)
        self.assertNotEqual(notification_key({}, "main", "Готово"), notification_key({}, "main", "Готово"))

    def test_same_thread_without_unique_event_id_does_not_hide_new_alerts(self):
        event = {"thread-id": "thread-one"}
        self.assertNotEqual(notification_key(event, "main", "Готово"), notification_key(event, "main", "Готово"))
        hook = {"session_id": "thread-one", "turn_id": "turn-one"}
        self.assertNotEqual(notification_key(hook, "PermissionRequest", "Разрешить"), notification_key(hook, "PermissionRequest", "Разрешить"))

    def test_hook_persists_notification_without_network_or_credential_access(self):
        self.store.set("chat_id", "23456")
        event = {"type": "agent-turn-complete", "last-assistant-message": "Изменения готовы"}
        with patch.object(worker, "Store", side_effect=lambda: Store(self.path)), patch.object(worker, "ensure_worker") as startup, patch.object(worker, "read_credential") as credential, patch.object(worker, "api_request") as api:
            self.assertEqual(notify.main(["main", json.dumps(event, ensure_ascii=False)]), 0)
            startup.assert_called_once()
            credential.assert_not_called()
            api.assert_not_called()
        row = self.store.db.execute("SELECT payload FROM outbox").fetchone()
        self.assertIn("Изменения готовы", json.loads(row[0])["text"])

    def test_rate_limit_delays_all_delivery_and_honors_retry_after(self):
        self.enqueue("one")
        self.enqueue("two", now=1001)
        limited = Mock(side_effect=core.TelegramError("sendMessage", 429, 45))
        worker.deliver_one(self.store, "one", self.credential, api=limited, now=1001)
        sender = Mock(return_value={"message_id": 40})
        self.assertFalse(worker.deliver_one(self.store, "one", self.credential, api=sender, now=1045))
        sender.assert_not_called()
        worker.deliver_one(self.store, "one", self.credential, api=sender, now=1046)
        sender.assert_called_once()

    def test_http_error_preserves_only_safe_retry_information(self):
        body = io.BytesIO(b'{"ok":false,"error_code":429,"description":"sensitive text","parameters":{"retry_after":42}}')
        error = urllib.error.HTTPError("https://example.test/private", 429, "error", None, body)
        with self.assertRaises(core.TelegramError) as raised:
            core.api_request(self.credential["token"], "sendMessage", transport=Mock(side_effect=error))
        self.assertEqual(raised.exception.retry_after, 42)
        self.assertEqual(raised.exception.code, 429)
        self.assertTrue(raised.exception.transient)
        self.assertNotIn("sensitive", str(raised.exception))
        self.assertNotIn("private", str(raised.exception))

    def test_permanent_error_and_destination_change_do_not_retry_or_send_elsewhere(self):
        self.enqueue()
        failing = Mock(side_effect=core.TelegramError("sendMessage", 403))
        worker.deliver_one(self.store, "one", self.credential, api=failing, now=1000)
        self.assertEqual(self.store.summary()["dead"], 1)
        self.enqueue("different-destination")
        sender = Mock()
        worker.deliver_one(self.store, "one", {**self.credential, "chat_id": "34567"}, api=sender, now=1000)
        sender.assert_not_called()
        self.assertEqual(self.store.summary()["dead"], 2)

    def test_queue_cannot_invoke_arbitrary_telegram_methods(self):
        self.store.enqueue("deleteWebhook", {"chat_id": "23456"}, dedupe="not-allowed", now=1000)
        sender = Mock()
        worker.deliver_one(self.store, "one", self.credential, api=sender, now=1000)
        sender.assert_not_called()
        self.assertEqual(self.store.summary()["dead"], 1)

    def test_malformed_success_is_retried_instead_of_losing_a_question(self):
        identifier = self.store.create_question("Вопрос", [], "23456", ttl=60, now=1000)
        worker.deliver_one(self.store, "one", self.credential, api=Mock(return_value={}), now=1000)
        self.assertEqual(self.store.summary()["queued"], 1)
        self.assertEqual(self.store.question(identifier)["status"], "pending")
        self.assertFalse(self.store.answer(identifier, "test", chat_id="23456", message_id=None, now=1001))

    def test_callback_answers_only_its_request_once_and_closes_buttons(self):
        identifier = self.request()
        ack = Mock(return_value=True)
        self.assertTrue(worker.handle_update(self.store, self.callback(identifier), self.credential, api=ack, now=1001))
        answer = json.loads(self.store.question(identifier)["answer"])
        self.assertEqual(answer["text"], "Полный")
        self.assertEqual(answer["choice"], 1)
        self.assertFalse(worker.handle_update(self.store, self.callback(identifier, 0), self.credential, api=ack, now=1002))
        self.assertEqual(json.loads(self.store.question(identifier)["answer"]), answer)
        sender = Mock(return_value=True)
        worker.deliver_one(self.store, "one", self.credential, api=sender, now=1002)
        self.assertEqual(sender.call_args.args[1], "editMessageReplyMarkup")
        self.assertEqual(sender.call_args.kwargs["body"]["reply_markup"], {"inline_keyboard": []})

    def test_other_sender_wrong_message_and_invalid_option_cannot_answer(self):
        identifier = self.request()
        for update in (self.callback(identifier, sender=999), self.callback(identifier, message_id=99), self.callback(identifier, choice=99)):
            self.assertFalse(worker.handle_update(self.store, update, self.credential, api=Mock(), now=1001))
            self.assertEqual(self.store.question(identifier)["status"], "pending")
        group = self.callback(identifier)
        group["callback_query"]["message"]["chat"]["type"] = "group"
        self.assertFalse(worker.handle_update(self.store, group, self.credential, api=Mock(), now=1001))

    def test_text_reply_is_data_and_requires_reply_to_the_right_message(self):
        identifier = self.request(options=[])
        update = self.text_reply(text="$(whoami); ' ; DROP TABLE questions; --")
        unscoped = self.text_reply()
        del unscoped["message"]["reply_to_message"]
        self.assertFalse(worker.handle_update(self.store, unscoped, self.credential, now=1001))
        with patch.object(worker.subprocess, "run") as runner, patch.object(worker.subprocess, "Popen") as process:
            self.assertTrue(worker.handle_update(self.store, update, self.credential, now=1001))
            runner.assert_not_called()
            process.assert_not_called()
        self.assertEqual(json.loads(self.store.question(identifier)["answer"])["text"], update["message"]["text"])

    def test_two_questions_remain_separate(self):
        first = self.request(thread_id="thread-one")
        second = self.store.create_question("Другой вопрос", ["Да", "Нет"], "23456", thread_id="thread-two", ttl=60, now=1001)
        worker.deliver_one(self.store, "one", self.credential, api=Mock(return_value={"message_id": 42}), now=1001)
        self.assertTrue(worker.handle_update(self.store, self.callback(second, 0, message_id=42), self.credential, api=Mock(), now=1002))
        self.assertEqual(self.store.question(first)["status"], "pending")
        self.assertEqual(self.store.question(second)["thread_id"], "thread-two")

    def test_expired_questions_are_not_sent_and_late_replies_are_rejected(self):
        identifier = self.store.create_question("Устаревший вопрос", [], "23456", ttl=10, now=1000)
        self.store.expire(1010)
        sender = Mock()
        self.assertFalse(worker.deliver_one(self.store, "one", self.credential, api=sender, now=1011))
        sender.assert_not_called()
        self.assertEqual(self.store.question(identifier)["status"], "expired")
        sent = self.request(now=1020)
        self.store.expire(1080)
        self.assertFalse(worker.handle_update(self.store, self.callback(sent), self.credential, api=Mock(), now=1081))

    def test_polling_keeps_offset_after_reopen_and_never_deletes_webhooks(self):
        identifier = self.request()
        calls = []
        def api(token, method, **kwargs):
            calls.append((method, kwargs))
            if method == "getWebhookInfo":
                return {"url": ""}
            if method == "getUpdates":
                return [self.callback(identifier)]
            return True
        worker.poll_answers(self.store, self.credential, api=api, now=1001)
        with Store(self.path) as reopened:
            self.assertEqual(reopened.get("update_offset"), "8")
            self.assertEqual(reopened.question(identifier)["status"], "answered")
        self.assertNotIn("deleteWebhook", [method for method, _ in calls])
        self.store.create_question("Ещё вопрос", [], "23456", ttl=60, now=1002)
        worker.poll_answers(self.store, self.credential, api=Mock(return_value={"url": "https://example.test/hook"}), now=1003)
        self.assertEqual(self.store.get("input_error"), "webhook_active")

    def test_ask_returns_answer_to_waiting_call_without_executing_it(self):
        def finish(store):
            identifier = store.db.execute("SELECT id FROM questions WHERE status='pending'").fetchone()[0]
            worker.deliver_one(store, "fake", self.credential, api=Mock(return_value={"message_id": 41}))
            worker.handle_update(store, self.callback(identifier), self.credential, api=Mock())
        with patch.object(control, "Store", side_effect=lambda: Store(self.path)), patch.object(control, "read_credential", return_value=self.credential), patch.object(control, "api_request", return_value={"url": ""}), patch.object(control, "ensure_worker", side_effect=finish):
            result = control.ask("Какой вариант?", ["Быстрый", "Полный"], thread_id="thread-one")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["thread_id"], "thread-one")
        self.assertEqual(result["answer"]["text"], "Полный")

    def test_ask_rejects_existing_webhook_before_sending(self):
        with patch.object(control, "read_credential", return_value=self.credential), patch.object(control, "api_request", return_value={"url": "https://example.test/webhook"}), patch.object(control, "Store") as store:
            with self.assertRaisesRegex(RuntimeError, "webhook"):
                control.ask("Вопрос", [])
            store.assert_not_called()

    def test_permanent_question_delivery_failure_unblocks_waiter(self):
        identifier = self.store.create_question("Вопрос", [], "23456", ttl=60, now=1000)
        worker.deliver_one(self.store, "one", self.credential, api=Mock(side_effect=core.TelegramError("sendMessage", 403)), now=1001)
        self.assertEqual(self.store.question(identifier)["status"], "failed")

    def test_doctor_is_read_only_and_does_not_reveal_messages_or_credentials(self):
        config = self.root / "config.toml"
        config.write_text(configure.append_hooks("notify = " + json.dumps(configure.python_command("main")) + "\n"), encoding="utf-8")
        self.enqueue()
        before = config.read_bytes()
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root)}), patch.object(control, "state_dir", return_value=self.root), patch.object(control, "read_credential", return_value=self.credential), patch.object(control, "owned_service", return_value=False), patch.object(control, "api_request") as api:
            result = control.doctor()
            api.assert_not_called()
        self.assertTrue(result["ok"])
        text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(self.credential["token"], text)
        self.assertNotIn("Готово", text)
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(self.store.summary()["queued"], 1)

    def test_worker_start_does_not_spawn_when_lease_is_live(self):
        self.store.acquire("one")
        with patch.object(worker.subprocess, "Popen") as process:
            self.assertFalse(worker.ensure_worker(self.store))
            process.assert_not_called()

    def test_windows_service_uses_interactive_least_privilege_and_no_secret(self):
        xml = control.task_xml(Path("C:/Python/pythonw.exe"), Path("C:/A folder/worker.py"), "S-1-5-21-123")
        root = ET.fromstring(xml)
        self.assertEqual(root.find("{*}Principals/{*}Principal/{*}LogonType").text, "InteractiveToken")
        self.assertEqual(root.find("{*}Principals/{*}Principal/{*}RunLevel").text, "LeastPrivilege")
        self.assertEqual(root.find("{*}Settings/{*}Hidden").text, "true")
        self.assertNotIn(self.credential["token"], xml)
        self.assertIn("worker.py", root.find("{*}Actions/{*}Exec/{*}Arguments").text)

    def test_windows_task_query_handles_mismatched_pipe_encoding(self):
        xml = '<?xml version="1.0" encoding="UTF-16"?><Task><RegistrationInfo><Description>' + control.TASK_DESCRIPTION + '</Description></RegistrationInfo></Task>'
        for encoding in ("utf-8", "utf-16"):
            response = Mock(returncode=0, stdout=xml.encode(encoding))
            with self.subTest(encoding=encoding), patch.object(control.subprocess, "run", return_value=response):
                self.assertTrue(control.owned_service())
        unrelated = xml.replace(control.TASK_DESCRIPTION, "Other program")
        with patch.object(control.subprocess, "run", return_value=Mock(returncode=0, stdout=unrelated.encode("utf-8"))):
            with self.assertRaisesRegex(RuntimeError, "another program"):
                control.owned_service()


if __name__ == "__main__":
    unittest.main()
