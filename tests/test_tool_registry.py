import json
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ai.ollama_client import OllamaError
from config.settings import Settings
from core.assistant import Assistant
from storage.database import Database
from tools.registry import ToolRegistry, parse_fallback_tool_call


class ToolRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.database = Database(self.root / "jager.sqlite3")
        self.settings = Settings(self.database)
        self.settings.set("vault_path", str(self.vault))
        self.registry = ToolRegistry(self.database, self.settings)

    def tearDown(self) -> None:
        self.database.close()
        self.directory.cleanup()

    def test_model_discovery_contains_only_registered_tools_and_schemas(self) -> None:
        definitions = self.registry.definitions()
        names = {item["function"]["name"] for item in definitions}
        descriptions = json.loads(self.registry.descriptions())

        self.assertIn("system_info", names)
        self.assertIn("create_vault_note", names)
        self.assertNotIn("web_search", names)
        self.assertTrue(all(item["function"]["parameters"]["type"] == "object" for item in definitions))
        self.assertIn(str(self.vault.resolve()), descriptions["authorized_directories"])
        system_info = next(
            item for item in descriptions["tools"] if item["name"] == "system_info"
        )
        self.assertIn("output", system_info)
        self.assertIn("risk", system_info)
        self.assertIn("confirmation_required", system_info)

    def test_validates_arguments_and_blocks_unknown_tools(self) -> None:
        self.assertFalse(self.registry.execute("missing_tool", {})["ok"])
        self.assertFalse(
            self.registry.execute("open_application", {"application": "terminal"})["ok"]
        )
        self.assertFalse(
            self.registry.execute(
                "system_info", {"command": "echo unsafe"}
            )["ok"]
        )
        self.assertTrue(self.registry.execute("system_info", {})["ok"])
        self.assertFalse(
            self.registry.execute(
                "open_url",
                {"url": "https://user:password@example.com"},
                approved=True,
            )["ok"]
        )

    def test_file_reads_are_limited_to_authorized_directories(self) -> None:
        note = self.vault / "permitted.md"
        note.write_text("conteúdo", encoding="utf-8")
        result = self.registry.execute("read_text_file", {"path": str(note)})
        outside = self.root / "outside.md"
        outside.write_text("não permitido", encoding="utf-8")
        denied = self.registry.execute("read_text_file", {"path": str(outside)})

        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["content"], "conteúdo")
        self.assertFalse(denied["ok"])

    def test_high_risk_actions_require_confirmation_and_never_overwrite(self) -> None:
        self.assertTrue(self.registry.requires_confirmation("create_vault_note"))
        unauthorized = self.registry.execute(
            "create_vault_note", {"name": "nota", "content": "primeira"}
        )
        first = self.registry.execute(
            "create_vault_note",
            {"name": "nota", "content": "primeira"},
            approved=True,
        )
        second = self.registry.execute(
            "create_vault_note",
            {"name": "nota", "content": "sobrescrever"},
            approved=True,
        )

        self.assertFalse(unauthorized["ok"])
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual((self.vault / "nota.md").read_text(encoding="utf-8"), "primeira")

    def test_vault_edits_require_confirmation_and_prevent_stale_overwrites(self) -> None:
        self.vault.joinpath("nota.md").write_text("original", encoding="utf-8")
        version = self.registry.execute(
            "read_vault_note", {"path": "nota.md"}
        )["result"]["version"]
        arguments = {
            "path": "nota.md",
            "content": "atualizada",
            "expected_hash": version,
        }
        denied = self.registry.execute("edit_vault_note", arguments)
        updated = self.registry.execute(
            "edit_vault_note", arguments, approved=True
        )
        stale = self.registry.execute(
            "edit_vault_note",
            {**arguments, "content": "substituir"},
            approved=True,
        )

        self.assertFalse(denied["ok"])
        self.assertTrue(updated["ok"])
        self.assertFalse(stale["ok"])
        self.assertEqual(
            self.vault.joinpath("nota.md").read_text(encoding="utf-8"),
            "atualizada",
        )

    def test_fallback_parser_accepts_only_exact_json_envelopes(self) -> None:
        self.assertEqual(
            parse_fallback_tool_call('{"tool":"system_info","arguments":{}}'),
            ("system_info", {}),
        )
        self.assertIsNone(parse_fallback_tool_call('{"tool":"system_info","arguments":{},"cmd":"x"}'))
        self.assertIsNone(parse_fallback_tool_call("run terminal command"))

    def test_agent_executes_tool_returns_result_and_continues(self) -> None:
        ollama = Mock()
        ollama.chat_stream_events.side_effect = [
            iter([{
                "content": "",
                "tool_calls": [
                    {"function": {"name": "system_info", "arguments": {}}}
                ],
            }]),
            iter([{"content": "Este é o sistema."}]),
        ]
        assistant = Assistant(self.database, ollama, self.settings)
        conversation_id = assistant.new_conversation()
        fragments = []

        result = assistant.respond_stream(
            conversation_id, "Qual é o sistema?", fragments.append, threading.Event()
        )

        self.assertEqual(result, "Este é o sistema.")
        self.assertEqual(fragments, ["Este é o sistema."])
        calls = ollama.chat_stream_events.call_args_list
        self.assertIn("system_info", calls[0].kwargs["tools"][0]["function"]["name"])
        second_turn = calls[1].args[1]
        tool_result = next(message for message in second_turn if message["role"] == "tool")
        self.assertTrue(json.loads(tool_result["content"])["ok"])
        self.assertEqual(assistant.history(conversation_id)[-1]["content"], result)

    def test_denied_confirmation_is_returned_to_model_without_external_action(self) -> None:
        ollama = Mock()
        ollama.chat_stream_events.side_effect = [
            iter([{
                "tool_calls": [
                    {
                        "function": {
                            "name": "open_url",
                            "arguments": {"url": "https://example.com"},
                        }
                    }
                ]
            }]),
            iter([{"content": "A ação foi cancelada."}]),
        ]
        assistant = Assistant(self.database, ollama, self.settings)
        conversation_id = assistant.new_conversation()
        confirm = Mock(return_value=False)
        with patch("tools.registry.webbrowser.open") as open_url:
            answer = assistant.respond_stream(
                conversation_id,
                "Abra o site",
                lambda _fragment: None,
                threading.Event(),
                confirm_tool=confirm,
            )

        confirm.assert_called_once()
        open_url.assert_not_called()
        self.assertEqual(answer, "A ação foi cancelada.")
        tool_message = next(
            item for item in ollama.chat_stream_events.call_args_list[1].args[1]
            if item["role"] == "tool"
        )
        self.assertIn("não autorizada", tool_message["content"])

    def test_json_fallback_executes_tools_and_returns_a_final_answer(self) -> None:
        ollama = Mock()
        ollama.chat_stream_events.side_effect = [
            OllamaError("tools indisponíveis"),
            iter([{"content": '{"tool":"system_info","arguments":{}}'}]),
            iter([{"content": '{"final":"Sistema consultado."}'}]),
        ]
        assistant = Assistant(self.database, ollama, self.settings)
        conversation_id = assistant.new_conversation()

        answer = assistant.respond_stream(
            conversation_id, "Qual sistema?", lambda _text: None, threading.Event()
        )

        self.assertEqual(answer, "Sistema consultado.")
        self.assertEqual(ollama.chat_stream_events.call_args_list[1].kwargs["tools"], None)


if __name__ == "__main__":
    unittest.main()
