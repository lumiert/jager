import tempfile
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
        self.assistant = Assistant(
            self.database, self.ollama, Settings(self.database)
        )
        self.conversation_id = self.assistant.new_conversation()

    def tearDown(self) -> None:
        self.database.close()
        self.directory.cleanup()

    def test_note_and_task_commands_use_local_storage(self) -> None:
        note = self.assistant.respond(self.conversation_id, "Anote que comprar PETG")
        task = self.assistant.respond(self.conversation_id, "Crie uma tarefa revisar código")
        notes = self.assistant.respond(self.conversation_id, "Minhas notas")
        tasks = self.assistant.respond(self.conversation_id, "Tarefas pendentes")

        self.assertIn("Anotei", note)
        self.assertIn("Tarefa criada", task)
        self.assertIn("comprar PETG", notes)
        self.assertIn("revisar código", tasks)
        self.ollama.chat.assert_not_called()

    def test_regular_chat_uses_ollama_and_is_saved(self) -> None:
        self.ollama.chat.return_value = "Olá!"
        answer = self.assistant.respond(self.conversation_id, "Oi Jager")

        self.assertEqual(answer, "Olá!")
        self.assertEqual(
            [message["role"] for message in self.assistant.history(self.conversation_id)],
            ["user", "assistant"],
        )

    def test_unlisted_application_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            open_application("aplicativo inventado")

    def test_web_search_opens_encoded_query_in_browser(self) -> None:
        with patch("tools.windows.webbrowser.open", return_value=True) as open_url:
            search_web("modelos locais")

        self.assertIn("q=modelos+locais", open_url.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
