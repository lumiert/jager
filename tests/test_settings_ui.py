import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication, QLineEdit
from PySide6.QtGui import QTextCursor, QTextDocument
from PySide6.QtTest import QTest

from ai.ollama_client import OllamaClient
from app.main_window import MainWindow
from app.theme import THEME
from config.settings import Settings
from core.assistant import Assistant
from storage.database import Database


class SettingsAndVisualTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.directory.name) / "jager.sqlite3")
        self.settings = Settings(self.database)
        ollama = Mock(spec=OllamaClient)
        ollama.list_models.return_value = []
        self.assistant = Assistant(self.database, ollama, self.settings)
        self.window = MainWindow(self.assistant, ollama, self.settings, self.database)
        self.window.show()
        if self.window._probe_worker is not None:
            self.window._probe_worker.wait(5000)
        self.application.processEvents()
        self.application.processEvents()

    def tearDown(self) -> None:
        self.window.close()
        self.application.processEvents()
        self.database.close()
        self.directory.cleanup()

    def test_loading_indicator_is_anchored_to_message_and_lifecycle(self) -> None:
        self.window._messages = [
            {"role": "user", "content": "Olá"},
            {"role": "assistant", "content": ""},
        ]
        self.window._set_busy(True)
        self.window._render_messages()
        self.application.processEvents()

        self.assertTrue(self.window.response_status.isVisible())
        self.assertIs(
            self.window.response_status.parent(), self.window.chat.viewport()
        )
        anchor_cursor = QTextCursor(self.window.chat.document())
        anchor_cursor.movePosition(QTextCursor.MoveOperation.End)
        anchor = self.window.chat.document().find(
            "Jager", anchor_cursor, QTextDocument.FindFlag.FindBackward
        )
        self.assertIn(
            "jager-status://assistant-1", anchor.charFormat().anchorHref()
        )
        self.window._on_fragment(self.window.conversation_id, "Resposta progressiva")
        QTest.qWait(100)
        self.assertTrue(self.window.response_status.isVisible())
        self.assertIn("Resposta progressiva", self.window.chat.toPlainText())
        response_cursor = self.window.chat.document().find("Resposta progressiva")
        self.assertLessEqual(
            self.window.response_status.geometry().bottom(),
            self.window.chat.cursorRect(response_cursor).top(),
        )

        self.window._on_answer(self.window.conversation_id, "Resposta progressiva")
        self.assertFalse(self.window.response_status.isVisible())
        self.assertEqual(
            [message["role"] for message in self.window._messages],
            ["user", "assistant"],
        )

    def test_telegram_token_is_hidden_by_default_and_only_shown_while_pressed(self) -> None:
        field = QLineEdit()
        field.setEchoMode(QLineEdit.EchoMode.Password)
        self.assertEqual(field.echoMode(), QLineEdit.EchoMode.Password)
        with patch(
            "app.main_window.secure_store.load_telegram_token",
            return_value="123456789:example_token_secret",
        ):
            self.window._show_telegram_token(field)
        self.assertEqual(field.echoMode(), QLineEdit.EchoMode.Normal)
        self.assertEqual(field.text(), "123456789:example_token_secret")
        self.window._hide_telegram_token(field)
        self.assertEqual(field.echoMode(), QLineEdit.EchoMode.Password)
        self.assertEqual(field.text(), "")

        field.setText("123456789:new_secret_token")
        self.window._showing_saved_telegram_token = False
        self.window._hide_telegram_token(field)
        self.assertEqual(field.echoMode(), QLineEdit.EchoMode.Password)
        self.assertEqual(field.text(), "123456789:new_secret_token")

    def test_text_styles_do_not_apply_filled_backgrounds(self) -> None:
        self.assertIn("QLabel, QToolTip", THEME)
        self.assertIn("background-color: transparent", THEME)
        self.assertIn("QListWidget::item:selected", THEME)
        selected_style = THEME.split("QListWidget::item:selected", 1)[1].split("}", 1)[0]
        self.assertIn("background-color: transparent", selected_style)
        self.assertNotIn("background-color: #", self.window.chat.document().defaultStyleSheet())
        self.assertNotIn("pre.code-block { background", self.window.chat.document().defaultStyleSheet())

    def test_progressive_speech_queues_complete_phrases_and_flushes_remainder(self) -> None:
        queued = []
        self.window._queue_speech = queued.append
        self.window._speech_buffer = ""

        self.window._queue_progressive_speech("Uma frase. Próxima")
        self.assertEqual(queued, ["Uma frase."])
        self.assertEqual(self.window._speech_buffer, "Próxima")

        self.window._flush_progressive_speech()
        self.assertEqual(queued, ["Uma frase.", "Próxima"])
        self.assertEqual(self.window._speech_buffer, "")

    def test_speech_status_and_stop_control_clear_after_playback_finishes(self) -> None:
        self.window._speech_current_path = None
        self.window._speech_queue.clear()
        self.window._speech_worker = None
        self.window.statusBar().showMessage("Reproduzindo resposta de áudio…")
        self.window._speech_stop_button.show()

        self.window._finish_speech_file()
        self.application.processEvents()

        self.assertEqual(self.window.statusBar().currentMessage(), "")
        self.assertFalse(self.window._speech_stop_button.isVisible())


if __name__ == "__main__":
    unittest.main()
