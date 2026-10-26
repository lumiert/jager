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


if __name__ == "__main__":
    unittest.main()
