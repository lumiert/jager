import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ai.ollama_client import OllamaError
from config.settings import Settings
from core.assistant import Assistant
from storage.database import Database
from tools.permissions import ALLOW, ASK, POLICY_LABELS, PermissionStore

REQUEST = (
    "Crie um checklist de tarefas diárias na raiz do meu Vault chamado Rechronic.md, "
    "e ponha uma tarefa pendente: Suporte fita isolante para cinto"
)
EXPECTED = "# Rechronic\n\n## Checklist Diário\n- [ ] Suporte fita isolante para cinto\n"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.database = Database(self.root / "jager.sqlite3")
        self.settings = Settings(self.database)
        self.settings.set("vault_path", str(self.vault))
        self.ollama = Mock()
        self.assistant = Assistant(self.database, self.ollama, self.settings)
        self.conversation = self.assistant.new_conversation()

    def tearDown(self) -> None:
        self.database.close()
        self.directory.cleanup()

    def allow(self, *names: str) -> None:
        for name in names:
            self.assistant.tools.permissions.set_policy(name, ALLOW)


class PermissionTests(Base):
    def test_default_is_ask_for_every_tool_and_labels_are_exact(self) -> None:
        entries = self.assistant.tools.entries()
        self.assertTrue(entries)
        self.assertTrue(all(entry["policy"] == ASK for entry in entries))
        self.assertEqual(list(POLICY_LABELS.values()), ["Perguntar sempre", "Permitir sempre"])

    def test_invalid_or_unreadable_setting_falls_back_to_ask(self) -> None:
        self.database.set_setting("tool_permission.system_info", "talvez")
        self.assertEqual(PermissionStore(self.database).policy("system_info"), ASK)
        broken = Mock()
        broken.get_setting.side_effect = RuntimeError("falha")
        self.assertEqual(PermissionStore(broken).policy("system_info"), ASK)
        with self.assertRaises(ValueError):
            PermissionStore(self.database).set_policy("system_info", "talvez")

    def test_policy_persists_across_restart(self) -> None:
        self.allow("system_info")
        reopened = Assistant(self.database, self.ollama, self.settings)
        self.assertEqual(reopened.tools.permissions.policy("system_info"), ALLOW)
        self.assertEqual(reopened.tools.permissions.policy("run_terminal_command"), ASK)

    def test_ask_prompts_on_every_call_and_allow_never_prompts(self) -> None:
        tools = self.assistant.tools
        confirm = Mock(return_value=True)
        tools.execute("system_info", {}, confirm=confirm)
        tools.execute("system_info", {}, confirm=confirm)
        self.assertEqual(confirm.call_count, 2)
        request = confirm.call_args.args[0]
        self.assertIn("title", request)
        self.assertIn("arguments", request)
        self.allow("system_info")
        confirm.reset_mock()
        self.assertTrue(tools.execute("system_info", {}, confirm=confirm)["ok"])
        confirm.assert_not_called()

    def test_without_confirm_callback_ask_is_denied(self) -> None:
        result = self.assistant.tools.execute("system_info", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "denied")

    def test_allowing_one_tool_does_not_authorize_another(self) -> None:
        self.allow("run_terminal_command")
        for other in ("create_file", "web_fetch", "create_vault_note", "system_info"):
            self.assertEqual(self.assistant.tools.permissions.policy(other), ASK)
            self.assertEqual(self.assistant.tools.execute(other, {})["status"] != "completed", True)

    def test_no_tool_can_change_permissions(self) -> None:
        names = {entry["id"].lower() for entry in self.assistant.tools.entries()}
        self.assertFalse([n for n in names if "permission" in n or "policy" in n])

    def test_every_listed_tool_is_exposed_only_when_available(self) -> None:
        offered = {d["function"]["name"] for d in self.assistant.tools.definitions()}
        available = {e["id"] for e in self.assistant.tools.entries() if e["available"]}
        self.assertEqual(offered, available)
        self.settings.set("vault_path", "")
        offered = {d["function"]["name"] for d in self.assistant.tools.definitions()}
        self.assertNotIn("create_vault_note", offered)
        entry = next(e for e in self.assistant.tools.entries() if e["id"] == "create_vault_note")
        self.assertFalse(entry["available"])
        self.assertTrue(entry["reason"])

    def test_audit_log_records_tool_policy_and_outcome(self) -> None:
        self.assistant.tools.execute("system_info", {}, confirm=lambda _r: True)
        rows = self.database.list_tool_audit()
        self.assertEqual(rows[0]["tool"], "system_info")
        self.assertEqual(rows[0]["policy"], ASK)
        self.assertEqual(rows[0]["authorization"], "granted")
        self.assertEqual(rows[0]["status"], "completed")


class NoteFidelityTests(Base):
    def test_rechronic_request_writes_exact_content_at_vault_root(self) -> None:
        self.allow("create_vault_note")
        fragments = []
        answer = self.assistant.respond_stream(
            self.conversation, REQUEST, fragments.append, threading.Event()
        )
        target = self.vault / "Rechronic.md"
        self.assertEqual(target.read_bytes().decode("utf-8"), EXPECTED)
        self.assertEqual([p.name for p in self.vault.iterdir()], ["Rechronic.md"])
        self.assertIn("Rechronic.md", answer)
        self.ollama.chat_stream_events.assert_not_called()

    def test_denied_authorization_writes_nothing_and_does_not_claim_success(self) -> None:
        answer = self.assistant.respond_stream(
            self.conversation, REQUEST, lambda _f: None, threading.Event(),
            confirm=lambda _request: False,
        )
        self.assertEqual(list(self.vault.iterdir()), [])
        self.assertNotIn("criada", answer)

    def test_write_failure_is_not_reported_as_success(self) -> None:
        self.allow("create_vault_note")
        with patch("storage.vault.os.fsync", side_effect=OSError("disco cheio")):
            answer = self.assistant.respond_stream(
                self.conversation, REQUEST, lambda _f: None, threading.Event()
            )
        self.assertIn("Não consegui", answer)
        self.assertNotIn("criada e verificada", answer)
        self.assertFalse((self.vault / "Rechronic.md").exists())

    def test_repeating_the_request_after_a_timeout_does_not_duplicate_or_overwrite(self) -> None:
        self.allow("create_vault_note")
        for _ in range(2):
            self.assistant.respond_stream(
                self.conversation, REQUEST, lambda _f: None, threading.Event()
            )
        self.assertEqual([p.name for p in self.vault.iterdir()], ["Rechronic.md"])
        self.assertEqual((self.vault / "Rechronic.md").read_text(encoding="utf-8"), EXPECTED)

    def test_existing_note_with_other_content_is_preserved(self) -> None:
        self.allow("create_vault_note")
        (self.vault / "Rechronic.md").write_text("meu conteúdo", encoding="utf-8")
        answer = self.assistant.respond_stream(
            self.conversation, REQUEST, lambda _f: None, threading.Event()
        )
        self.assertEqual((self.vault / "Rechronic.md").read_text(encoding="utf-8"), "meu conteúdo")
        self.assertIn("Não consegui", answer)

    def test_model_invented_tasks_are_rejected_and_never_written(self) -> None:
        self.allow("create_vault_note")
        invented = "# Rechronic\n\n- [ ] Revisar objetivos semanais\n- [ ] Suporte fita isolante para cinto\n"
        good = "# Rechronic\n\n- [ ] Suporte fita isolante para cinto\n"

        def call(content):
            return iter([{"tool_calls": [{"function": {
                "name": "create_vault_note",
                "arguments": {"name": "Rechronic", "content": content}}}]}])

        self.ollama.chat_stream_events.side_effect = [
            call(invented), call(good), iter([{"content": "Pronto."}]),
        ]
        # Phrase avoids the deterministic handler so the model path is exercised.
        text = "Quero uma nota Rechronic com a tarefa pendente: Suporte fita isolante para cinto"
        self.assistant.respond_stream(self.conversation, text, lambda _f: None, threading.Event())
        self.assertEqual((self.vault / "Rechronic.md").read_text(encoding="utf-8"), good)
        second_turn = self.ollama.chat_stream_events.call_args_list[1].args[1]
        tool_message = json.loads(next(m for m in second_turn if m["role"] == "tool")["content"])
        self.assertFalse(tool_message["ok"])
        self.assertIn("Revisar objetivos semanais", tool_message["error"])

    def test_suggestions_are_allowed_when_user_asks_for_them(self) -> None:
        error = Assistant.fidelity_error(
            "create_vault_note",
            {"content": "- [ ] Beber água"},
            "Crie uma nota com sugestões de tarefas",
        )
        self.assertIsNone(error)

    def test_path_traversal_is_blocked(self) -> None:
        self.allow("create_vault_note")
        result = self.assistant.tools.execute(
            "create_vault_note", {"name": "../fora", "content": "x"}
        )
        self.assertFalse(result["ok"])
        self.assertFalse((self.root / "fora.md").exists())

    def test_model_failure_after_a_write_reports_completed_action_and_retry_is_idempotent(self) -> None:
        self.allow("create_vault_note")
        call = iter([{"tool_calls": [{"function": {
            "name": "create_vault_note",
            "arguments": {"name": "Ideias", "content": "# Ideias\n"}}}]}])

        def failing():
            raise OllamaError("A resposta excedeu o tempo máximo configurado.")
            yield

        self.ollama.chat_stream_events.side_effect = [call, failing()]
        fragments = []
        with self.assertRaises(OllamaError):
            self.assistant.respond_stream(
                self.conversation, "Crie a nota Ideias", fragments.append, threading.Event()
            )
        self.assertIn("não serão repetidas", "".join(fragments))
        self.assertEqual((self.vault / "Ideias.md").read_text(encoding="utf-8"), "# Ideias\n")
        retry = self.assistant.tools.execute("create_vault_note", {"name": "Ideias", "content": "# Ideias\n"})
        self.assertTrue(retry["ok"])
        self.assertTrue(retry["result"]["already_existed"])
        self.assertEqual(len(list(self.vault.iterdir())), 1)

    def test_identical_repeated_call_in_one_answer_is_not_executed_twice(self) -> None:
        self.allow("create_task")
        call = lambda: iter([{"tool_calls": [{"function": {
            "name": "create_task", "arguments": {"title": "Pagar conta"}}}]}])
        self.ollama.chat_stream_events.side_effect = [call(), call(), iter([{"content": "ok"}])]
        self.assistant.respond_stream(
            self.conversation, "Faça algo", lambda _f: None, threading.Event()
        )
        self.assertEqual(len(self.database.list_tasks()), 1)

    def test_loop_without_progress_is_aborted(self) -> None:
        self.allow("system_info")
        call = lambda: iter([{"tool_calls": [{"function": {"name": "system_info", "arguments": {}}}]}])
        self.ollama.chat_stream_events.side_effect = [call() for _ in range(10)]
        with self.assertRaises(OllamaError):
            self.assistant.respond_stream(
                self.conversation, "Qual sistema?", lambda _f: None, threading.Event()
            )

    def test_iteration_limit_is_configurable(self) -> None:
        self.allow("list_tasks")
        self.settings.set("tool_iteration_limit", "1")
        calls = [
            iter([{"tool_calls": [{"function": {"name": "list_tasks", "arguments": {"pending_only": flag}}}]}])
            for flag in (True, False, True)
        ]
        self.ollama.chat_stream_events.side_effect = calls
        with self.assertRaises(OllamaError):
            self.assistant.respond_stream(
                self.conversation, "Tarefas?", lambda _f: None, threading.Event()
            )
        self.assertEqual(self.ollama.chat_stream_events.call_count, 2)

    def test_cycle_duration_limit_is_enforced(self) -> None:
        self.settings.set("tool_cycle_timeout", "10")
        self.ollama.chat_stream_events.side_effect = [iter([{"content": "oi"}])]
        with patch("core.assistant.time.monotonic", side_effect=[0, 100, 100, 100]):
            with self.assertRaises(OllamaError):
                self.assistant.respond_stream(
                    self.conversation, "Oi", lambda _f: None, threading.Event()
                )


if __name__ == "__main__":
    unittest.main()
