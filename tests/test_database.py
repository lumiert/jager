import tempfile
import unittest
from pathlib import Path

from config.settings import Settings
from storage.database import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.directory.name) / "test.sqlite3")

    def tearDown(self) -> None:
        self.database.close()
        self.directory.cleanup()

    def test_notes_and_tasks_are_persisted(self) -> None:
        note_id = self.database.create_note("Comprar filamento PETG")
        task_id = self.database.create_task("Revisar o projeto")
        self.database.complete_task(task_id)

        self.assertEqual(self.database.list_notes()[0]["id"], note_id)
        self.assertEqual(self.database.list_notes()[0]["content"], "Comprar filamento PETG")
        self.assertEqual(self.database.list_tasks()[0]["completed"], 1)
        self.assertEqual(self.database.list_tasks(pending_only=True), [])

    def test_settings_survive_database_reopen(self) -> None:
        self.database.set_setting("ollama_model", "qwen3:1.7b")
        path = self.database.path
        self.database.close()

        reopened = Database(path)
        self.assertEqual(Settings(reopened).ollama_model, "qwen3:1.7b")
        reopened.close()

    def test_empty_notes_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.database.create_note("  ")

    def test_conversation_titles_and_manual_title_precedence(self) -> None:
        conversation_id = self.database.create_conversation()
        self.assertEqual(self.database.conversation(conversation_id)["title"], "Nova conversa")
        self.assertTrue(self.database.set_automatic_title(conversation_id, "Ideias de projeto"))
        self.database.set_conversation_title(conversation_id, "Meu título")
        self.assertFalse(self.database.set_automatic_title(conversation_id, "Outro título"))
        self.assertEqual(self.database.conversation(conversation_id)["title"], "Meu título")

    def test_deleting_conversation_removes_only_its_messages(self) -> None:
        first = self.database.create_conversation()
        second = self.database.create_conversation()
        self.database.add_message(first, "user", "apagar")
        self.database.add_message(second, "user", "manter")

        self.assertTrue(self.database.delete_conversation(first))
        self.assertIsNone(self.database.conversation(first))
        self.assertEqual(self.database.conversation_messages(second)[0]["content"], "manter")

    def test_old_conversation_schema_is_migrated(self) -> None:
        path = Path(self.directory.name) / "old.sqlite3"
        import sqlite3

        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE conversations (id INTEGER PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO conversations(title, created_at) VALUES ('Título antigo', 'ontem')"
        )
        connection.commit()
        connection.close()

        migrated = Database(path)
        self.assertEqual(migrated.conversation(1)["title"], "Título antigo")
        self.assertEqual(migrated.conversation(1)["title_manual"], 0)
        migrated.close()


if __name__ == "__main__":
    unittest.main()
