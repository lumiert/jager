import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from config.settings import Settings
from core.assistant import Assistant
from storage.database import Database
from tools.windows import open_application, search_web


class AssistantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.directory.name) / "test.sqlite3")
        self.ollama = Mock()
        self.vault_path = Path(self.directory.name) / "vault"
        self.vault_path.mkdir()
        self.settings = Settings(self.database)
        self.settings.set("vault_path", str(self.vault_path))
        self.assistant = Assistant(
            self.database, self.ollama, self.settings
        )
        for entry in self.assistant.tools.entries():
            self.assistant.tools.permissions.set_policy(entry["id"], "allow")
        self.conversation_id = self.assistant.new_conversation()

    def tearDown(self) -> None:
        self.database.close()
        self.directory.cleanup()

    @staticmethod
    def tool_call(name, arguments):
        return iter([{
            "tool_calls": [{
                "function": {"name": name, "arguments": arguments},
            }],
        }])

    def test_notes_and_tasks_use_vault_markdown(self) -> None:
        note = self.assistant.respond(self.conversation_id, "Anote que comprar PETG")
        self.ollama.chat_stream_events.side_effect = [
            self.tool_call(
                "create_vault_note",
                {
                    "name": "Pendências.md",
                    "content": "# Pendências\n\n- [ ] revisar código\n",
                },
            ),
            iter([{"content": "Tarefa adicionada à lista Pendências.md."}]),
        ]
        task = self.assistant.respond(self.conversation_id, "Crie uma tarefa revisar código")
        notes = self.assistant.respond(self.conversation_id, "Minhas notas")
        tasks = self.assistant.respond(self.conversation_id, "Tarefas pendentes")

        self.assertIn("Nota criada no Vault", note)
        self.assertIn("Pendências.md", task)
        self.assertIn("comprar-petg.md", notes)
        self.assertEqual(
            (self.vault_path / "Pendências.md").read_text(encoding="utf-8"),
            "# Pendências\n\n- [ ] revisar código\n",
        )
        self.assertIn("- [ ] revisar código", tasks)
        self.assertFalse(hasattr(self.database, "create_task"))
        self.assertTrue(
            {"list_tasks", "create_task", "complete_task"}.isdisjoint(
                self.assistant.tools.tool_names()
            )
        )
        self.assertEqual(self.ollama.chat_stream_events.call_count, 2)

    def test_today_task_request_adds_full_item_to_today_markdown(self) -> None:
        self.ollama.chat_stream_events.side_effect = [
            self.tool_call(
                "create_vault_note",
                {
                    "name": "Hoje.md",
                    "content": "# Hoje\n\n- [ ] o João vem buscar as encomendas aqui na loja\n",
                },
            ),
            iter([{"content": "Tarefa adicionada à lista Hoje.md."}]),
        ]
        answer = self.assistant.respond(
            self.conversation_id,
            "Hoje o João vem buscar as encomendas aqui na loja, adicione na lista de hoje",
        )
        self.assertIn("Hoje.md", answer)
        self.assertEqual(
            (self.vault_path / "Hoje.md").read_text(encoding="utf-8"),
            "# Hoje\n\n- [ ] o João vem buscar as encomendas aqui na loja\n",
        )
        self.assertEqual(self.ollama.chat_stream_events.call_count, 2)
        tool_messages = [
            message
            for message in self.ollama.chat_stream_events.call_args_list[1].args[1]
            if message.get("role") == "tool"
        ]
        self.assertNotIn("sha256", tool_messages[0]["content"])
        self.assertNotIn("verified", tool_messages[0]["content"])
        self.assertNotRegex(answer, r"\b[a-f0-9]{64}\b")

    def test_today_task_request_preserves_existing_list_items(self) -> None:
        original = "# Hoje\n\n- [ ] Buscar pão\n"
        (self.vault_path / "Hoje.md").write_text(original, encoding="utf-8")
        self.ollama.chat_stream_events.side_effect = [
            self.tool_call("read_vault_note", {"path": "Hoje.md"}),
            self.tool_call(
                "edit_vault_note",
                {
                    "path": "Hoje.md",
                    "content": (
                        "# Hoje\n\n"
                        "- [ ] Buscar pão\n"
                        "- [ ] O João vem buscar as encomendas na loja\n"
                    ),
                },
            ),
            iter([{"content": "Tarefa adicionada à lista Hoje.md."}]),
        ]

        self.assistant.respond(
            self.conversation_id,
            "Hoje o João vem buscar as encomendas aqui na loja, adicione na lista de hoje",
        )

        self.assertEqual(
            (self.vault_path / "Hoje.md").read_text(encoding="utf-8"),
            (
                "# Hoje\n\n"
                "- [ ] Buscar pão\n"
                "- [ ] O João vem buscar as encomendas na loja\n"
            ),
        )

    def test_regular_chat_uses_ollama_and_is_saved(self) -> None:
        self.ollama.chat_stream_events.return_value = iter(
            [{"content": "Olá!"}]
        )
        answer = self.assistant.respond(self.conversation_id, "Oi Jager")

        self.assertEqual(answer, "Olá!")
        self.assertEqual(
            [message["role"] for message in self.assistant.history(self.conversation_id)],
            ["user", "assistant"],
        )

    def test_streamed_answer_is_emitted_and_saved_once(self) -> None:
        self.ollama.chat_stream_events.return_value = iter(
        [{"content": "Olá"}, {"content": " mundo!"}]
        )
        fragments = []
        answer = self.assistant.respond_stream(
            self.conversation_id, "Oi", fragments.append, threading.Event()
        )

        self.assertEqual(answer, "Olá mundo!")
        self.assertEqual(fragments, ["Olá", " mundo!"])
        history = self.assistant.history(self.conversation_id)
        self.assertEqual([message["role"] for message in history], ["user", "assistant"])
        self.assertEqual(history[-1]["content"], "Olá mundo!")

    def test_cancellation_keeps_only_fragments_already_received(self) -> None:
        cancelled = threading.Event()
        self.ollama.chat_stream_events.return_value = iter(
            [{"content": "parcial"}, {"content": "descartado"}]
        )
        fragments = []

        def receive(fragment):
            fragments.append(fragment)
            cancelled.set()

        answer = self.assistant.respond_stream(
            self.conversation_id, "Oi", receive, cancelled
        )

        self.assertEqual(answer, "parcial")
        self.assertEqual(fragments, ["parcial"])
        self.assertEqual(self.assistant.history(self.conversation_id)[-1]["content"], "parcial")

    def test_unlisted_application_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            open_application("aplicativo inventado")

    def test_web_search_opens_encoded_query_in_browser(self) -> None:
        with patch("tools.windows.webbrowser.open", return_value=True) as open_url:
            search_web("modelos locais")

        self.assertIn("q=modelos+locais", open_url.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
