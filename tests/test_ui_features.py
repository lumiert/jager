import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QTabWidget

from ai.ollama_client import OllamaClient
from app import audio
from app.main_window import MainWindow, SpinnerWidget
from config.settings import Settings
from core.assistant import Assistant
from storage.database import Database


class FakeRecorder(QObject):
    state_changed = Signal(str)
    transcribed = Signal(str)
    failed = Signal(str)
    finished = Signal()
    instances = []

    def __init__(self, _device="default") -> None:
        super().__init__()
        self.stopped = self.cancelled = self.started = False
        FakeRecorder.instances.append(self)

    def start(self):
        self.started = True
        self.state_changed.emit("recording")

    def stop_recording(self):
        self.stopped = True

    def cancel_recording(self):
        self.cancelled = True

    def isRunning(self):
        return self.started and not getattr(self, "_done", False)

    def wait(self, *_):
        return True

    def end(self):
        self._done = True
        self.finished.emit()


class UiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.directory.name) / "jager.sqlite3")
        self.settings = Settings(self.database)
        self.ollama = Mock(spec=OllamaClient)
        self.ollama.list_models.return_value = []
        self.assistant = Assistant(self.database, self.ollama, self.settings)
        self.window = MainWindow(self.assistant, self.ollama, self.settings, self.database)
        self.window.show()
        if self.window._probe_worker is not None:
            self.window._probe_worker.wait(5000)
        self.application.processEvents()

    def tearDown(self) -> None:
        self.window.close()
        self.application.processEvents()
        self.database.close()
        self.directory.cleanup()


class PushToTalkToggleTests(UiBase):
    def setUp(self) -> None:
        super().setUp()
        FakeRecorder.instances.clear()
        patcher = patch("app.main_window.PushToTalkWorker", FakeRecorder)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sent = []
        self.window._submit_message = self.sent.append
        self.button = self.window.microphone_button

    def test_first_click_records_second_click_stops_and_transcript_fills_input_without_sending(self) -> None:
        self.button.click()
        self.assertEqual(len(FakeRecorder.instances), 1)
        self.assertEqual(self.window._ptt_state, "recording")
        self.assertEqual(self.button.property("pttState"), "recording")
        self.assertIn("parar", self.button.toolTip().lower())
        self.button.click()
        recorder = FakeRecorder.instances[0]
        self.assertTrue(recorder.stopped)
        self.assertEqual(self.window._ptt_state, "transcribing")
        recorder.transcribed.emit("olá mundo")
        recorder.end()
        self.application.processEvents()
        self.assertEqual(self.window.input.toPlainText(), "olá mundo")
        self.assertEqual(self.sent, [])
        self.ollama.chat_stream_events.assert_not_called()
        self.assertIsNone(self.window._chat_worker)
        self.assertIsNone(self.window._audio_worker)
        self.assertTrue(self.button.isEnabled())

    def test_active_state_has_red_background_white_icon_and_stop(self) -> None:
        from app.theme import THEME

        rule = THEME.split('QPushButton[pttState="recording"]', 1)[1]
        self.assertIn("#DC2626", rule)
        self.assertIn("#FFFFFF", rule)
        calls = []
        with patch("app.main_window.qta.icon", side_effect=lambda *a, **k: calls.append((a, k)) or __import__("PySide6.QtGui", fromlist=["QIcon"]).QIcon()):
            self.window._set_mic_state("recording")
        self.assertEqual(calls[-1][0][0], "fa5s.stop")
        self.assertEqual(calls[-1][1]["color"], "#FFFFFF")
        self.window._set_mic_state("idle")
        self.assertEqual(self.button.property("pttState"), "idle")

    def test_existing_text_is_preserved_and_merged_predictably(self) -> None:
        self.window.input.setPlainText("rascunho")
        self.window._start_ptt()
        recorder = FakeRecorder.instances[0]
        recorder.transcribed.emit("complemento")
        recorder.end()
        self.assertEqual(self.window.input.toPlainText(), "rascunho complemento")
        self.assertEqual(MainWindow.merge_transcription("linha\n", "x"), "linha\nx")
        self.assertEqual(MainWindow.merge_transcription("   ", "x"), "x")

    def test_empty_transcript_is_a_failure_and_never_fills_or_sends(self) -> None:
        self.window.input.setPlainText("texto")
        self.window._start_ptt()
        recorder = FakeRecorder.instances[0]
        recorder.transcribed.emit("   ")
        recorder.end()
        self.assertEqual(self.window.input.toPlainText(), "texto")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.window._ptt_state, "error")

    def test_failure_and_cancel_restore_the_button(self) -> None:
        self.window._start_ptt()
        recorder = FakeRecorder.instances[0]
        recorder.failed.emit("microfone indisponível")
        recorder.end()
        self.assertTrue(self.button.isEnabled())
        self.assertNotEqual(self.button.property("pttState"), "recording")
        self.window._reset_ptt_error()
        self.assertEqual(self.window._ptt_state, "idle")

        self.button.click()
        second = FakeRecorder.instances[1]
        self.window._cancel_ptt()
        self.assertTrue(second.cancelled)
        second.end()
        self.assertTrue(self.button.isEnabled())
        self.assertNotEqual(self.button.property("pttState"), "recording")
        self.assertEqual(self.sent, [])

    def test_no_simultaneous_recordings_or_duplicate_workers(self) -> None:
        self.button.click()
        self.window._start_ptt()
        self.assertEqual(len(FakeRecorder.instances), 1)
        self.button.click()
        self.button.click()  # ignored while transcribing
        self.assertEqual(len(FakeRecorder.instances), 1)
        self.assertEqual(self.window._ptt_state, "transcribing")

    def test_closing_the_window_cancels_recording(self) -> None:
        self.button.click()
        recorder = FakeRecorder.instances[0]
        self.window._cancel_ptt()
        recorder.end()
        self.assertTrue(recorder.cancelled)


class SpinnerTests(UiBase):
    def test_spinner_replaces_progress_bar_with_no_text_and_stops(self) -> None:
        from PySide6.QtWidgets import QProgressBar

        self.assertEqual(self.window.response_status.findChildren(QProgressBar), [])
        self.assertEqual(self.window.response_status.findChildren(QLabel), [])
        self.assertIsInstance(self.window.loading_indicator, SpinnerWidget)
        self.window._messages = [{"role": "user", "content": "a"}, {"role": "assistant", "content": ""}]
        self.window._set_busy(True)
        self.window._render_messages()
        self.application.processEvents()
        self.assertTrue(self.window.loading_indicator.isVisible())
        self.assertTrue(self.window.loading_indicator.isAnimating())
        self.window._set_tool_status("create_vault_note")
        self.assertEqual(self.window.response_status.findChildren(QLabel), [])
        for finish in (
            lambda: self.window._on_answer(self.window.conversation_id, "ok"),
            lambda: self.window._on_error(self.window.conversation_id, "falha"),
        ):
            self.window._set_busy(True)
            self.application.processEvents()
            self.assertTrue(self.window.loading_indicator.isAnimating())
            finish()
            self.window._set_busy(False)
            self.application.processEvents()
            self.assertFalse(self.window.loading_indicator.isAnimating())
            self.assertFalse(self.window.response_status.isVisible())

    def test_no_progress_bar_style_remains(self) -> None:
        from app.theme import THEME

        self.assertNotIn("QProgressBar", THEME)


class SettingsTests(UiBase):
    def build(self):
        dialog, controls = self.window._build_settings_dialog()
        self.addCleanup(dialog.deleteLater)
        return dialog, controls

    def test_descriptions_sit_between_title_and_control(self) -> None:
        dialog, controls = self.build()
        dialog.show()
        self.application.processEvents()
        general = controls["tabs"].widget(0).widget()
        layout = general.layout()
        items = [layout.itemAt(i).widget() or layout.itemAt(i) for i in range(layout.count())]
        texts = [w.text() if isinstance(w, QLabel) else None for w in items]
        index = texts.index("URL do Ollama")
        self.assertEqual(texts[index + 1], "Endereço local do servidor utilizado pelo assistente.")
        self.assertIs(items[index + 2], controls["ollama_url"])
        vault = texts.index("Diretório do Vault do Obsidian")
        self.assertIn("Pasta raiz das notas", texts[vault + 1])
        self.assertEqual(len([t for t in texts if t == texts[vault + 1]]), 1)
        title = texts.index("Responder por áudio")
        self.assertIsNotNone(texts[title + 1])
        self.assertIs(items[title + 2], controls["voice_response"])

    def test_system_default_label_keeps_internal_identifier(self) -> None:
        combo = QComboBox()
        self.window._active_audio_status = QLabel()
        with patch("app.main_window.output_identifier", create=True):
            self.window._populate_audio_outputs(combo, "default")
        self.assertEqual(combo.itemText(0), "Padrão do sistema")
        self.assertEqual(combo.itemData(0), "default")
        self.assertEqual(combo.currentData(), "default")
        source = Path("app/main_window.py").read_text(encoding="utf-8")
        self.assertNotIn("Padrão do Windows", source)
        self.assertNotIn("padrão do Windows", source)

    def test_permissions_tab_lists_every_tool_with_exactly_two_options(self) -> None:
        dialog, controls = self.build()
        tabs: QTabWidget = controls["tabs"]
        self.assertEqual([tabs.tabText(i) for i in range(tabs.count())], ["Geral", "Permissões"])
        entries = self.assistant.tools.entries()
        self.assertEqual(set(controls["permissions"]), {e["id"] for e in entries})
        for combo in controls["permissions"].values():
            self.assertEqual([combo.itemText(i) for i in range(combo.count())], ["Perguntar sempre", "Permitir sempre"])
            self.assertEqual(combo.currentData(), "ask")
        for required in ("run_terminal_command", "create_file", "web_fetch", "create_vault_note"):
            self.assertIn(required, controls["permissions"])

    def test_unavailable_tools_are_labelled(self) -> None:
        self.settings.set("vault_path", "")
        dialog, controls = self.build()
        texts = [label.text() for label in dialog.findChildren(QLabel)]
        self.assertTrue(any("Indisponível" in text for text in texts))

    def test_saving_permissions_changes_real_authorization_and_persists(self) -> None:
        dialog, controls = self.build()
        combo = controls["permissions"]["system_info"]
        combo.setCurrentIndex(combo.findData("allow"))
        controls["numbers"]["ollama_idle_timeout"].setValue(77)
        self.assertTrue(self.window._apply_settings(controls))
        tools = self.assistant.tools
        self.assertTrue(tools.execute("system_info", {})["ok"])
        self.assertEqual(tools.execute("list_tasks", {})["status"], "denied")
        self.assertEqual(Assistant(self.database, self.ollama, self.settings).tools.permissions.policy("system_info"), "allow")
        self.assertEqual(self.settings.get("ollama_idle_timeout"), "77")

    def test_existing_preferences_are_loaded_into_the_dialog(self) -> None:
        self.settings.set("tool_iteration_limit", "6")
        self.settings.set("audio_output_id", "default")
        dialog, controls = self.build()
        self.assertEqual(controls["numbers"]["tool_iteration_limit"].value(), 6)
        self.assertEqual(controls["audio_output"].currentData(), "default")


class WhisperCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        audio._model_cache.clear()

    def test_cached_model_is_reused_and_loaded_offline_first(self) -> None:
        calls = []

        class Model:
            def __init__(self, name, **kwargs):
                calls.append(kwargs)

        first = audio._load_model(Model)
        second = audio._load_model(Model)
        self.assertIs(first, second)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["local_files_only"])

    def test_missing_cache_downloads_once_without_token_and_reports_state(self) -> None:
        calls, states = [], []

        class Model:
            def __init__(self, name, **kwargs):
                calls.append(kwargs)
                if kwargs.get("local_files_only"):
                    raise RuntimeError("cache incompleto")

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HF_TOKEN", None)
            audio._load_model(Model, lambda: states.append("download"))
        self.assertEqual(states, ["download"])
        self.assertEqual(len(calls), 2)
        self.assertNotIn("local_files_only", calls[1])
        self.assertNotIn("token", calls[1])

    def test_network_failure_gives_friendly_error_and_is_not_cached(self) -> None:
        class Model:
            def __init__(self, *_a, **_k):
                raise OSError("sem rede")

        with self.assertRaises(RuntimeError) as caught:
            audio._load_model(Model)
        self.assertIn("conexão", str(caught.exception))
        self.assertNotIn(Model, audio._model_cache)

    def test_symlink_warning_is_silenced_without_requiring_admin(self) -> None:
        self.assertEqual(os.environ.get("HF_HUB_DISABLE_SYMLINKS_WARNING"), "1")


if __name__ == "__main__":
    unittest.main()
