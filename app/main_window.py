from __future__ import annotations

import base64
import logging
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

import qtawesome as qta
from PySide6.QtCore import QEvent, QTimer, Qt, QThread, Signal, QUrl
from PySide6.QtGui import (
    QCloseEvent,
    QDesktopServices,
    QKeyEvent,
    QKeySequence,
    QShortcut,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QProgressBar,
    QSpinBox,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ai.ollama_client import OllamaClient
from app.audio import PushToTalkWorker
from app.audio_devices import (
    apply_output_device,
    input_devices,
    output_identifier,
)
from app.markdown_view import render_markdown
from app.theme import THEME
from app.tts import SpeechSynthesisWorker
from config import secure_store
from config.settings import Settings
from core.assistant import Assistant
from storage.database import Database
from storage.vault import Vault, VaultError

logger = logging.getLogger(__name__)


class Worker(QThread):
    succeeded = Signal(object)
    failed = Signal(object, str)

    def __init__(self, operation, context=None) -> None:
        super().__init__()
        self.operation = operation
        self.context = context

    def run(self) -> None:
        try:
            self.succeeded.emit(self.operation())
        except Exception as error:
            logger.exception("Falha durante operação em segundo plano")
            self.failed.emit(self.context, str(error))


class ChatWorker(QThread):
    fragment = Signal(int, str)
    tool_status = Signal(str)
    confirmation_requested = Signal(object)
    succeeded = Signal(int, str)
    failed = Signal(int, str)

    def __init__(self, assistant: Assistant, conversation_id: int, text: str) -> None:
        super().__init__()
        self.assistant = assistant
        self.conversation_id = conversation_id
        self.text = text
        self.cancelled = threading.Event()

    def cancel(self) -> None:
        self.cancelled.set()

    def confirm_tool(self, name: str, arguments: dict) -> bool:
        request = {"name": name, "arguments": arguments, "event": threading.Event()}
        self.confirmation_requested.emit(request)
        while not request["event"].wait(0.1):
            if self.cancelled.is_set():
                return False
        return bool(request.get("approved"))

    @staticmethod
    def resolve_confirmation(request: dict, approved: bool) -> None:
        request["approved"] = approved
        request["event"].set()

    def run(self) -> None:
        try:
            answer = self.assistant.respond_stream(
                self.conversation_id,
                self.text,
                lambda fragment: self.fragment.emit(self.conversation_id, fragment),
                self.cancelled,
                self.tool_status.emit,
                self.confirm_tool,
            )
            self.succeeded.emit(self.conversation_id, answer)
        except Exception as error:
            logger.exception("Falha durante geração de resposta")
            self.failed.emit(self.conversation_id, str(error))


class MessageInput(QPlainTextEdit):
    send_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setPlaceholderText("Escreva uma mensagem… (Shift+Enter para nova linha)")
        self.setFixedHeight(82)
        self.setTabChangesFocus(True)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (
            event.modifiers() & Qt.ShiftModifier
        ):
            self.send_requested.emit()
            return
        super().keyPressEvent(event)


class VaultDialog(QDialog):
    def __init__(self, parent: QWidget, vault: Vault) -> None:
        super().__init__(parent)
        self.vault = vault
        self.current_path: str | None = None
        self.current_hash: str | None = None
        self.setWindowTitle("Vault do Obsidian")
        self.resize(820, 560)
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Pesquisar notas por nome ou conteúdo")
        self.search.textChanged.connect(self._filter_notes)
        toolbar.addWidget(self.search, 1)
        new_button = QPushButton(qta.icon("fa5s.plus"), " Nova nota")
        new_button.clicked.connect(self._new_note)
        toolbar.addWidget(new_button)
        layout.addLayout(toolbar)

        content = QHBoxLayout()
        self.notes = QListWidget()
        self.notes.currentItemChanged.connect(self._open_selected)
        content.addWidget(self.notes, 1)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("Selecione ou crie uma nota Markdown")
        content.addWidget(self.editor, 3)
        layout.addLayout(content, 1)
        footer = QHBoxLayout()
        footer.addStretch()
        save_button = QPushButton(qta.icon("fa5s.save"), " Salvar")
        save_button.setObjectName("primary")
        save_button.clicked.connect(self._save_note)
        footer.addWidget(save_button)
        layout.addLayout(footer)
        self._load_notes()

    def _load_notes(self, query: str = "") -> None:
        self.notes.blockSignals(True)
        self.notes.clear()
        if query.strip():
            matches = self.vault.search(query, limit=500)
            paths = [path for path, _content in matches]
        else:
            paths = self.vault.list_notes()
        for path in paths:
            item = QListWidgetItem(path)
            item.setData(Qt.UserRole, path)
            self.notes.addItem(item)
        self.notes.blockSignals(False)

    def _filter_notes(self, query: str) -> None:
        self._load_notes(query)

    def _open_selected(self, current: QListWidgetItem | None, _previous) -> None:
        if not current:
            return
        try:
            self._discard_or_save_current()
        except VaultError:
            self.notes.blockSignals(True)
            for index in range(self.notes.count()):
                item = self.notes.item(index)
                if item.data(Qt.UserRole) == self.current_path:
                    self.notes.setCurrentItem(item)
                    break
            self.notes.blockSignals(False)
            return
        try:
            content, self.current_hash = self.vault.read_note(current.data(Qt.UserRole))
        except (OSError, UnicodeError, VaultError) as error:
            QMessageBox.warning(self, "Não foi possível abrir a nota", str(error))
            return
        self.current_path = current.data(Qt.UserRole)
        self.editor.setPlainText(content)
        self.editor.document().setModified(False)

    def _discard_or_save_current(self) -> None:
        if self.editor.document().isModified():
            answer = QMessageBox.question(
                self,
                "Alterações não salvas",
                "Salvar as alterações da nota atual antes de continuar?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if answer == QMessageBox.Cancel:
                raise VaultError("A ação foi cancelada.")
            if answer == QMessageBox.Save:
                self._save_note()
                if self.editor.document().isModified():
                    raise VaultError("As alterações não foram salvas.")

    def _new_note(self) -> None:
        try:
            self._discard_or_save_current()
        except VaultError:
            return
        relative, accepted = QInputDialog.getText(
            self, "Nova nota", "Nome ou caminho da nota (.md):"
        )
        if not accepted:
            return
        relative = relative.strip()
        if not relative:
            return
        if not relative.lower().endswith(".md"):
            relative += ".md"
        try:
            self.vault.create_note(relative)
            self.current_path = relative
            self.current_hash = self.vault.read_note(relative)[1]
            self.editor.clear()
            self.editor.document().setModified(False)
            self._load_notes()
            items = self.notes.findItems(relative, Qt.MatchExactly)
            if items:
                self.notes.setCurrentItem(items[0])
        except (OSError, UnicodeError, VaultError) as error:
            QMessageBox.warning(self, "Não foi possível criar a nota", str(error))

    def _save_note(self) -> None:
        if not self.current_path:
            QMessageBox.information(self, "Vault", "Selecione ou crie uma nota primeiro.")
            return
        try:
            self.vault.save_note(
                self.current_path,
                self.editor.toPlainText(),
                expected_hash=self.current_hash,
            )
            self.current_hash = self.vault.read_note(self.current_path)[1]
            self.editor.document().setModified(False)
        except (OSError, UnicodeError, VaultError) as error:
            QMessageBox.warning(self, "Não foi possível salvar", str(error))


class MainWindow(QMainWindow):
    def __init__(
        self,
        assistant: Assistant,
        ollama: OllamaClient,
        settings: Settings,
        database: Database,
    ) -> None:
        super().__init__()
        self.assistant = assistant
        self.ollama = ollama
        self.settings = settings
        self.database = database
        self.conversation_id = database.latest_conversation()
        self._chat_worker: ChatWorker | None = None
        self._title_workers: list[Worker] = []
        self._probe_worker: Worker | None = None
        self._audio_worker: PushToTalkWorker | None = None
        self._ptt_state = "idle"
        self._is_busy = False
        self._telegram_dialog: QDialog | None = None
        self._telegram_token_field: QLineEdit | None = None
        self._active_audio_combo: QComboBox | None = None
        self._active_audio_status: QLabel | None = None
        self._active_input_audio_combo: QComboBox | None = None
        self._active_input_audio_status: QLabel | None = None
        self._speech_supported = False
        self._speech_worker: SpeechSynthesisWorker | None = None
        self._speech_queue: list[str] = []
        self._speech_buffer = ""
        self._speech_current_path: str | None = None
        self._speech_stop_button = QToolButton(self)
        self._speech_stop_button.setIcon(self._icon("fa5s.stop"))
        self._speech_stop_button.setToolTip("Interromper resposta de áudio")
        self._speech_stop_button.setAccessibleName("Interromper resposta de áudio")
        self._speech_stop_button.clicked.connect(self._stop_speech)
        self._speech_stop_button.hide()
        self.statusBar().addPermanentWidget(self._speech_stop_button)
        self._ptt_timeout = QTimer(self)
        self._ptt_timeout.setSingleShot(True)
        self._ptt_timeout.setInterval(60000)
        self._ptt_timeout.timeout.connect(self._stop_ptt)
        self._ptt_error_reset = QTimer(self)
        self._ptt_error_reset.setSingleShot(True)
        self._ptt_error_reset.setInterval(4500)
        self._ptt_error_reset.timeout.connect(self._reset_ptt_error)
        self._cancel_ptt_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self._cancel_ptt_shortcut.activated.connect(self._cancel_ptt)
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer

            self.media_devices = QMediaDevices(self)
            self.media_devices.audioOutputsChanged.connect(self._audio_outputs_changed)
            self.media_devices.audioInputsChanged.connect(self._audio_outputs_changed)
            self._tts_output = QAudioOutput(self)
            self._tts_player = QMediaPlayer(self)
            self._tts_player.setAudioOutput(self._tts_output)
            self._tts_player.mediaStatusChanged.connect(self._speech_media_status_changed)
            self._tts_player.errorOccurred.connect(self._speech_playback_error)
            self._speech_supported = True
        except (ImportError, OSError):
            logger.warning("A enumeração de dispositivos de saída não está disponível.")
            self.media_devices = None
            self._tts_output = None
            self._tts_player = None
        self._messages: list[dict[str, str]] = []
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(70)
        self._render_timer.timeout.connect(self._render_messages)
        self.installEventFilter(self)
        self.setWindowTitle("Jager")
        self.resize(1050, 720)
        self.setStyleSheet(THEME)
        self._build_ui()
        self._load_conversations()
        self._load_history()
        self.refresh_models()

    @staticmethod
    def _icon(name: str):
        return qta.icon(name, color="#B9C9E2")

    def _button(self, icon: str, tooltip: str, callback) -> QPushButton:
        button = QPushButton(self._icon(icon), "")
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.clicked.connect(callback)
        return button

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(235)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(15, 20, 15, 16)
        brand = QLabel("JAGER")
        brand.setObjectName("brand")
        sidebar_layout.addWidget(brand)
        subtitle = QLabel("Assistente pessoal local")
        subtitle.setObjectName("subtitle")
        sidebar_layout.addWidget(subtitle)
        sidebar_layout.addSpacing(20)

        new_chat = QPushButton(self._icon("fa5s.plus"), " Nova conversa")
        new_chat.setToolTip("Iniciar uma nova conversa")
        new_chat.clicked.connect(self.new_conversation)
        sidebar_layout.addWidget(new_chat)
        vault_button = QPushButton(self._icon("fa5s.folder-open"), " Vault")
        vault_button.setToolTip("Abrir notas do Vault do Obsidian")
        vault_button.clicked.connect(self.open_vault)
        sidebar_layout.addWidget(vault_button)
        settings_button = QPushButton(self._icon("fa5s.cog"), " Configurações")
        settings_button.clicked.connect(self.open_settings)
        sidebar_layout.addWidget(settings_button)
        sidebar_layout.addSpacing(14)
        sidebar_layout.addWidget(QLabel("CONVERSAS"))
        self.conversation_list = QListWidget()
        self.conversation_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.conversation_list.customContextMenuRequested.connect(
            self._conversation_menu
        )
        self.conversation_list.currentItemChanged.connect(self._select_conversation)
        sidebar_layout.addWidget(self.conversation_list, 1)
        status_row = QHBoxLayout()
        self.ollama_status_icon = QLabel()
        status_row.addWidget(self.ollama_status_icon)
        self.ollama_status = QLabel("Ollama: verificando…")
        self.ollama_status.setObjectName("muted")
        status_row.addWidget(self.ollama_status)
        status_row.addStretch()
        sidebar_layout.addLayout(status_row)
        self._set_ollama_status("Ollama: verificando…", "sync-alt", "#94A3B8")
        layout.addWidget(sidebar)

        main = QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        header = QFrame()
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(22, 14, 22, 14)
        title_icon = QLabel()
        title_icon.setPixmap(self._icon("fa5s.comment-alt").pixmap(20, 20))
        header_layout.addWidget(title_icon)
        self.title_label = QLabel("Nova conversa")
        self.title_label.setObjectName("conversationTitle")
        header_layout.addWidget(self.title_label, 1)
        self.rename_button = self._button(
            "fa5s.pencil-alt", "Renomear conversa", self.rename_conversation
        )
        header_layout.addWidget(self.rename_button)
        main.addWidget(header)

        self.chat = QTextBrowser()
        self.chat.setOpenExternalLinks(False)
        self.chat.anchorClicked.connect(self._open_chat_link)
        self.chat.setContextMenuPolicy(Qt.CustomContextMenu)
        self.chat.customContextMenuRequested.connect(self._chat_context_menu)
        self.chat.document().setDefaultStyleSheet(
            """
            body { color: #E8EEF8; font-family: "Segoe UI", sans-serif; font-size: 10pt; }
            .message-label { color: #60A5FA; font-weight: 600; margin-top: 16px; }
            .message-label.assistant { color: #34D399; }
            .message-body { margin: 3px 0 16px; line-height: 1.5; }
            .message-loading-space { margin: 0; line-height: 1.4; }
            p, div, span, h1, h2, h3, h4, h5, h6, blockquote, li, th, td { background: transparent; }
            a { color: #7CB7FF; }
            a.assistant-name { color: #34D399; text-decoration: none; }
            blockquote { border-left: 3px solid #3B82F6; margin-left: 8px; padding-left: 12px; color: #B9C9E2; }
            pre.code-block { border-left: 2px solid #34445E; padding: 4px 12px; white-space: pre-wrap; }
            pre code { color: #D7E4F7; font-family: Consolas, monospace; }
            .copy-code { margin-top: -10px; font-size: 9pt; }
            table { border-collapse: collapse; } th, td { border: 1px solid #34445E; padding: 5px 8px; }
            hr { border: 0; border-top: 1px solid #34445E; }
            """
        )
        self.response_status = QWidget(self.chat.viewport())
        self.response_status.setStyleSheet("background: transparent;")
        status_layout = QHBoxLayout(self.response_status)
        status_layout.setContentsMargins(0, 0, 0, 0)
        self.response_status_label = QLabel("Gerando resposta")
        self.response_status_label.setStyleSheet("background: transparent; color: #94A3B8;")
        status_layout.addWidget(self.response_status_label)
        self.loading_indicator = QProgressBar()
        self.loading_indicator.setRange(0, 0)
        self.loading_indicator.setTextVisible(False)
        self.loading_indicator.setFixedSize(46, 6)
        status_layout.addWidget(self.loading_indicator)
        self.response_status.hide()
        self.chat.verticalScrollBar().valueChanged.connect(self._position_response_status)
        main.addWidget(self.chat, 1)

        composer = QFrame()
        composer.setObjectName("composer")
        composer_layout = QVBoxLayout(composer)
        composer_layout.setContentsMargins(18, 10, 18, 14)
        model_row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setEditable(False)
        self.model_combo.setMinimumWidth(140)
        self.model_combo.setMaximumWidth(260)
        self.model_combo.currentTextChanged.connect(self._save_model)
        model_row.addWidget(QLabel("Modelo"))
        model_row.addWidget(self.model_combo)
        refresh = self._button("fa5s.sync-alt", "Atualizar modelos do Ollama", self.refresh_models)
        model_row.addWidget(refresh)
        model_row.addStretch(1)
        composer_layout.addLayout(model_row)

        message_row = QHBoxLayout()
        self.microphone_button = QPushButton(self._icon("fa5s.microphone"), "")
        self.microphone_button.setToolTip("Pressione e segure para falar")
        self.microphone_button.setAccessibleName("Pressionar e segurar para falar")
        self.microphone_button.setAccessibleDescription(
            "Grava somente enquanto pressionado. Solte para transcrever; Escape cancela."
        )
        self.microphone_button.pressed.connect(self._start_ptt)
        self.microphone_button.released.connect(self._stop_ptt)
        self.microphone_button.installEventFilter(self)
        message_row.addWidget(self.microphone_button)
        self.input = MessageInput()
        self.input.send_requested.connect(self.send_message)
        message_row.addWidget(self.input, 1)
        self.send_button = QPushButton(self._icon("fa5s.paper-plane"), " Enviar")
        self.send_button.setObjectName("primary")
        self.send_button.setMinimumWidth(100)
        self.send_button.clicked.connect(self.send_message)
        message_row.addWidget(self.send_button)
        self.stop_button = self._button("fa5s.stop", "Interromper geração", self.cancel_generation)
        self.stop_button.hide()
        message_row.addWidget(self.stop_button)
        composer_layout.addLayout(message_row)
        main.addWidget(composer)
        layout.addLayout(main, 1)
        self.setCentralWidget(root)

    def _load_history(self) -> None:
        self._messages = self.assistant.history(self.conversation_id)
        if not self._messages:
            self._messages = [
                {"role": "assistant", "content": "Olá! Sou o Jager. Como posso ajudar?"}
            ]
        conversation = self.database.conversation(self.conversation_id)
        self.title_label.setText(
            conversation["title"] if conversation else "Nova conversa"
        )
        self._render_messages()

    def _render_messages(self) -> None:
        blocks = []
        for index, message in enumerate(self._messages):
            role = message["role"]
            label = "Você" if role == "user" else "Jager"
            class_name = "assistant" if role == "assistant" else "user"
            label_html = (
                f'<a class="assistant-name" href="jager-status://assistant-{index}">Jager</a>'
                if role == "assistant"
                else label
            )
            loading_space = (
                '<div class="message-loading-space"><br><br><br></div>'
                if self._is_busy
                and index == len(self._messages) - 1
                and role == "assistant"
                else ""
            )
            blocks.append(
                f'<p class="message-label {class_name}">{label_html}</p>'
                f'{loading_space}'
                f'<div class="message-body">{render_markdown(message["content"])}</div>'
            )
        self.chat.setHtml("<html><body>" + "\n".join(blocks) + "</body></html>")
        scrollbar = self.chat.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        self._position_response_status()

    def _position_response_status(self, *_args) -> None:
        if not self.response_status.isVisible():
            return
        cursor = QTextCursor(self.chat.document())
        cursor.movePosition(QTextCursor.End)
        cursor = self.chat.document().find(
            "Jager", cursor, QTextDocument.FindFlag.FindBackward
        )
        expected_anchor = (
            f"assistant-{len(self._messages) - 1}"
            if self._messages and self._messages[-1]["role"] == "assistant"
            else ""
        )
        while (
            not cursor.isNull()
            and cursor.charFormat().anchorHref() != f"jager-status://{expected_anchor}"
        ):
            cursor = self.chat.document().find(
                "Jager", cursor, QTextDocument.FindFlag.FindBackward
            )
        if cursor.isNull() or not expected_anchor:
            return
        line = self.chat.cursorRect(cursor)
        self.response_status.adjustSize()
        width = self.response_status.sizeHint().width()
        self.response_status.setGeometry(
            max(0, line.left()),
            line.bottom() + 2,
            min(width, self.chat.viewport().width() - line.left()),
            self.response_status.sizeHint().height(),
        )

    def _open_chat_link(self, url: QUrl) -> None:
        if url.scheme() == "jager-copy":
            try:
                code = base64.urlsafe_b64decode(url.path().encode("ascii")).decode("utf-8")
            except (ValueError, UnicodeError):
                QMessageBox.warning(self, "Código", "Não foi possível copiar o código.")
                return
            from PySide6.QtWidgets import QApplication

            QApplication.clipboard().setText(code)
            self.statusBar().showMessage("Código copiado.", 2500)
            return
        if url.scheme() in {"http", "https", "mailto"}:
            QDesktopServices.openUrl(url)

    def _chat_context_menu(self, position) -> None:
        menu = self.chat.createStandardContextMenu()
        menu.addSeparator()
        copy_code = menu.addAction(self._icon("fa5s.copy"), "Copiar código selecionado")
        if menu.exec(self.chat.mapToGlobal(position)) == copy_code:
            from PySide6.QtWidgets import QApplication

            QApplication.clipboard().setText(self.chat.textCursor().selectedText())

    def send_message(self) -> None:
        text = self.input.toPlainText().strip()
        self._submit_message(text)

    def _submit_message(self, text: str) -> None:
        if not text or self._chat_worker is not None:
            return
        conversation_id = self.conversation_id
        should_title = not self.database.has_messages(conversation_id)
        if self.input.toPlainText().strip() == text:
            self.input.clear()
        self._stop_speech()
        self._speech_buffer = ""
        self._messages.append({"role": "user", "content": text})
        self._messages.append({"role": "assistant", "content": ""})
        self._set_busy(True)
        self._render_messages()
        worker = ChatWorker(self.assistant, conversation_id, text)
        worker.fragment.connect(self._on_fragment)
        worker.tool_status.connect(self._set_tool_status)
        worker.confirmation_requested.connect(self._confirm_tool_request)
        worker.succeeded.connect(self._on_answer)
        worker.failed.connect(self._on_error)
        worker.finished.connect(self._chat_worker_finished)
        self._chat_worker = worker
        worker.start()
        if should_title:
            self._start_title_generation(conversation_id, text)

    def _start_title_generation(self, conversation_id: int, text: str) -> None:
        worker = Worker(
            lambda: self.assistant.generate_title(text),
            (conversation_id, text),
        )
        worker.succeeded.connect(self._title_generated)
        worker.failed.connect(self._title_failed)
        worker.finished.connect(lambda: self._remove_title_worker(worker))
        self._title_workers.append(worker)
        worker.start()

    def _remove_title_worker(self, worker: Worker) -> None:
        if worker in self._title_workers:
            self._title_workers.remove(worker)

    def _title_generated(self, result: str) -> None:
        worker = self.sender()
        context = worker.context if isinstance(worker, Worker) else None
        conversation_id = context[0] if context else None
        if conversation_id is not None and self.database.set_automatic_title(
            conversation_id, result
        ):
            self._load_conversations()
            if conversation_id == self.conversation_id:
                self.title_label.setText(result)

    def _title_failed(self, context, _message: str) -> None:
        conversation_id, first_message = context
        if conversation_id is None:
            return
        fallback = " ".join(first_message.split())[:60].strip(" .,:;!?")
        if fallback and self.database.set_automatic_title(conversation_id, fallback):
            self._load_conversations()
            if conversation_id == self.conversation_id:
                self.title_label.setText(fallback)

    def _on_fragment(self, conversation_id: int, fragment: str) -> None:
        if conversation_id != self.conversation_id:
            return
        if self._messages and self._messages[-1]["role"] == "assistant":
            self._messages[-1]["content"] += fragment
        if (
            self._speech_supported
            and self.settings.get("voice_response_enabled").lower() == "true"
            and self.settings.get("streaming_audio").lower() == "true"
        ):
            self._queue_progressive_speech(fragment)
        self._render_timer.start()

    def _on_answer(self, conversation_id: int, answer: str) -> None:
        if conversation_id != self.conversation_id:
            return
        if self._messages and self._messages[-1]["role"] == "assistant":
            if answer:
                self._messages[-1]["content"] = answer
            elif not self._messages[-1]["content"]:
                self._messages.pop()
        self._render_timer.stop()
        self._render_messages()
        self.response_status.hide()
        if (
            answer
            and self._speech_supported
            and self.settings.get("voice_response_enabled").lower() == "true"
            and self._chat_worker is not None
            and not self._chat_worker.cancelled.is_set()
        ):
            if self.settings.get("streaming_audio").lower() == "true":
                self._flush_progressive_speech()
            else:
                self._queue_speech(answer)

    def _on_error(self, conversation_id: int, message: str) -> None:
        if conversation_id != self.conversation_id:
            return
        logger.error("Falha no chat: %s", message)
        if self._messages and self._messages[-1]["role"] == "assistant":
            if self._messages[-1]["content"]:
                self._messages[-1]["content"] += (
                    "\n\n> A geração foi interrompida por um erro. "
                    "O texto recebido foi preservado."
                )
            else:
                self._messages[-1]["content"] = (
                    "Não consegui responder agora. Verifique a conexão com o Ollama "
                    "e tente novamente."
                )
        self._render_messages()
        self.response_status.hide()

    def _chat_worker_finished(self) -> None:
        self._chat_worker = None
        self._set_busy(False)
        self._render_messages()

    def _set_busy(self, busy: bool) -> None:
        self._is_busy = busy
        self.send_button.setVisible(not busy)
        self.stop_button.setVisible(busy)
        self.stop_button.setEnabled(busy)
        self.input.setEnabled(not busy)
        self.microphone_button.setEnabled(not busy and self._audio_worker is None)
        self.model_combo.setEnabled(not busy and self.model_combo.count() > 0)
        self.conversation_list.setEnabled(not busy)
        self.response_status.setVisible(busy)
        if busy:
            self.stop_button.setText("")
            self.response_status_label.setText("Gerando resposta")
            self._position_response_status()
        else:
            self.response_status.hide()

    def _set_tool_status(self, status: str) -> None:
        labels = {
            "system_info": "consultando o sistema",
            "list_applications": "consultando aplicativos disponíveis",
            "open_application": "abrindo aplicativo",
            "open_folder": "abrindo pasta",
            "open_url": "abrindo endereço externo",
            "list_files": "listando arquivos",
            "read_text_file": "lendo arquivo",
            "open_file": "abrindo arquivo",
            "search_files": "pesquisando arquivos",
            "create_vault_note": "criando nota no Vault",
            "edit_vault_note": "atualizando nota no Vault",
            "read_vault_note": "lendo nota do Vault",
            "list_vault_notes": "consultando notas do Vault",
            "search_vault_notes": "pesquisando no Vault",
            "list_tasks": "consultando tarefas",
            "create_task": "criando tarefa",
            "integration_status": "consultando integrações",
        }
        awaiting_confirmation = status.startswith("Jager solicita autorização para ")
        name = status.rsplit(" ", 1)[-1].rstrip(".")
        operation = labels.get(name, "operação do computador")
        prefix = "Aguardando confirmação: " if awaiting_confirmation else "Executando: "
        self.response_status_label.setText(prefix + operation)
        self._position_response_status()

    def _confirm_tool_request(self, request: dict) -> None:
        name = request["name"]
        arguments = request.get("arguments", {})
        safe_details = ", ".join(
            f"{key}: {str(value).replace(chr(10), ' ')[:160]}"
            for key, value in arguments.items()
            if key in {"name", "path", "url", "directory", "application", "folder", "content"}
        )
        action_names = {
            "create_vault_note": "criar uma nota no Vault",
            "edit_vault_note": "atualizar uma nota no Vault",
            "read_vault_note": "ler uma nota no Vault",
            "create_task": "criar uma tarefa",
            "open_application": "abrir um aplicativo permitido",
            "open_file": "abrir um arquivo com o aplicativo padrão",
            "open_url": "abrir um endereço externo",
        }
        details = f"\n\n{safe_details}" if safe_details else ""
        approved = QMessageBox.question(
            self,
            "Confirmar ação",
            f"O Jager deseja {action_names.get(name, name)}.{details}\n\nAutorizar?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) == QMessageBox.Yes
        ChatWorker.resolve_confirmation(request, approved)

    def _start_ptt(self) -> None:
        if self._chat_worker is not None or self._audio_worker is not None:
            return
        self._ptt_error_reset.stop()
        self._stop_speech()
        worker = PushToTalkWorker(self.settings.get("audio_input_id"))
        worker.state_changed.connect(self._set_mic_state)
        worker.transcribed.connect(self._ptt_transcribed)
        worker.failed.connect(self._ptt_failed)
        worker.finished.connect(self._ptt_finished)
        self._audio_worker = worker
        self._set_mic_state("starting")
        self._ptt_timeout.start()
        worker.start()

    def _stop_ptt(self) -> None:
        if self._audio_worker is not None:
            self._audio_worker.stop_recording()

    def _cancel_ptt(self) -> None:
        if self._audio_worker is not None:
            self._audio_worker.cancel_recording()
            self._ptt_timeout.stop()
            self._set_mic_state("cancelled")

    def _ptt_transcribed(self, text: str) -> None:
        self._ptt_timeout.stop()
        self._set_mic_state("idle")
        self._submit_message(text)

    def _ptt_failed(self, message: str) -> None:
        self._ptt_timeout.stop()
        logger.warning("Falha na captura/transcrição de áudio: %s", message)
        self.statusBar().showMessage(message, 6000)
        self._set_mic_state("error")
        self._ptt_error_reset.start()

    def _reset_ptt_error(self) -> None:
        if self._ptt_state == "error" and self._audio_worker is None:
            self._set_mic_state("idle")

    def _ptt_finished(self) -> None:
        self._ptt_timeout.stop()
        self._audio_worker = None
        self.microphone_button.setEnabled(self._chat_worker is None)
        if self.microphone_button.toolTip() in {
            "Gravando… solte para transcrever",
            "Processando fala…",
            "Iniciando microfone…",
            "Gravação cancelada",
        }:
            self._set_mic_state("idle")

    def _set_mic_state(self, state: str) -> None:
        states = {
            "idle": ("fa5s.microphone", "#B9C9E2", "Pressione e segure para falar"),
            "starting": ("fa5s.microphone", "#FBBF24", "Iniciando microfone…"),
            "recording": ("fa5s.microphone", "#F87171", "Gravando… solte para transcrever"),
            "processing": ("fa5s.spinner", "#FBBF24", "Processando fala…"),
            "cancelled": ("fa5s.microphone-slash", "#94A3B8", "Gravação cancelada"),
            "error": ("fa5s.exclamation-circle", "#F87171", "Falha na gravação; tente novamente"),
        }
        icon, color, tooltip = states.get(state, states["idle"])
        self.microphone_button.setIcon(qta.icon(icon, color=color))
        self.microphone_button.setToolTip(tooltip)
        self.microphone_button.setAccessibleName(tooltip)
        self._ptt_state = state
        if state == "cancelled":
            self.microphone_button.setEnabled(True)
        elif self._chat_worker is None and self._audio_worker is None:
            self.microphone_button.setEnabled(True)

    def eventFilter(self, watched, event) -> bool:
        if watched is self and event.type() in {
            QEvent.Type.WindowDeactivate,
            QEvent.Type.ApplicationDeactivate,
        }:
            self._cancel_ptt()
            token_field = getattr(self, "_telegram_token_field", None)
            if token_field is not None:
                self._hide_telegram_token(token_field)
        elif (
            watched is getattr(self, "_telegram_dialog", None)
            and event.type() in {
                QEvent.Type.WindowDeactivate,
                QEvent.Type.WindowStateChange,
                QEvent.Type.Hide,
            }
        ):
            token_field = getattr(self, "_telegram_token_field", None)
            if token_field is not None:
                self._hide_telegram_token(token_field)
        elif (
            watched is getattr(self, "_telegram_token_field", None)
            and event.type() == QEvent.Type.FocusOut
        ):
            self._hide_telegram_token(watched)
        elif watched is self and event.type() == QEvent.Type.WindowStateChange:
            if self.isMinimized():
                self._cancel_ptt()
                token_field = getattr(self, "_telegram_token_field", None)
                if token_field is not None:
                    self._hide_telegram_token(token_field)
        elif (
            watched is getattr(self, "microphone_button", None)
            and event.type() == QEvent.Type.FocusOut
            and self._audio_worker is not None
            and getattr(self, "_ptt_state", "idle") in {"starting", "recording"}
        ):
            self._cancel_ptt()
        elif (
            watched is self
            and event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key_Escape
            and self._audio_worker is not None
        ):
            self._cancel_ptt()
            return True
        return super().eventFilter(watched, event)

    def cancel_generation(self) -> None:
        if self._chat_worker is not None:
            self._chat_worker.cancel()
            self._stop_speech()
            self.stop_button.setEnabled(False)
            self.stop_button.setText("Interrompendo…")

    @staticmethod
    def _speech_text(text: str) -> str:
        text = re.sub(r"```.*?```", " bloco de código ", text, flags=re.DOTALL)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"[#>*_`~]", "", text)
        return " ".join(text.split())

    def _queue_progressive_speech(self, fragment: str) -> None:
        self._speech_buffer += fragment
        parts = re.split(r"(?<=[.!?])\s+|\n+", self._speech_buffer)
        if len(parts) < 2:
            return
        self._speech_buffer = parts[-1]
        for part in parts[:-1]:
            self._queue_speech(part)

    def _flush_progressive_speech(self) -> None:
        if self._speech_buffer.strip():
            self._queue_speech(self._speech_buffer)
        self._speech_buffer = ""

    def _queue_speech(self, text: str) -> None:
        spoken = self._speech_text(text)
        if spoken:
            self._speech_queue.append(spoken)
            self._start_next_speech()

    def _start_next_speech(self) -> None:
        if (
            not self._speech_supported
            or self._speech_worker is not None
            or self._speech_current_path is not None
            or not self._speech_queue
        ):
            return
        self._speech_stop_button.show()
        self.statusBar().showMessage("Gerando resposta de áudio…")
        worker = SpeechSynthesisWorker(
            self._speech_queue.pop(0),
            self.settings.get("piper_model_path"),
        )
        worker.ready.connect(self._speech_ready)
        worker.failed.connect(self._speech_failed)
        worker.finished.connect(self._speech_worker_finished)
        self._speech_worker = worker
        worker.start()

    def _speech_ready(self, path: str) -> None:
        worker = self.sender()
        if isinstance(worker, SpeechSynthesisWorker) and worker.is_cancelled():
            Path(path).unlink(missing_ok=True)
            return
        if self._tts_player is None or self._tts_output is None or self.media_devices is None:
            Path(path).unlink(missing_ok=True)
            self._speech_failed("A reprodução de áudio não está disponível.")
            return
        apply_output_device(
            self._tts_output,
            self.media_devices,
            self.settings.get("audio_output_id"),
        )
        self._speech_current_path = path
        self.statusBar().showMessage("Reproduzindo resposta de áudio…")
        self._tts_player.setSource(QUrl.fromLocalFile(path))
        self._tts_player.play()

    def _speech_failed(self, message: str) -> None:
        self._speech_stop_button.hide()
        self.statusBar().showMessage(message, 6000)

    def _speech_worker_finished(self) -> None:
        if self.sender() is self._speech_worker:
            self._speech_worker = None
        self._start_next_speech()
        if (
            self._speech_worker is None
            and self._speech_current_path is None
            and not self._speech_queue
        ):
            self._speech_stop_button.hide()
            self._clear_speech_status()

    def _speech_media_status_changed(self, status) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._finish_speech_file()

    def _speech_playback_error(self, _error, _message: str) -> None:
        self._speech_failed("Não foi possível reproduzir a resposta de áudio.")
        self._finish_speech_file()

    def _finish_speech_file(self) -> None:
        if self._tts_player is not None:
            self._tts_player.setSource(QUrl())
        if self._speech_current_path is not None:
            Path(self._speech_current_path).unlink(missing_ok=True)
            self._speech_current_path = None
        self._start_next_speech()
        if (
            self._speech_current_path is None
            and self._speech_worker is None
            and not self._speech_queue
        ):
            self._speech_stop_button.hide()
            self._clear_speech_status()

    def _stop_speech(self) -> None:
        self._speech_queue.clear()
        self._speech_buffer = ""
        if self._speech_worker is not None:
            self._speech_worker.cancel()
        if self._tts_player is not None:
            self._tts_player.stop()
            self._tts_player.setSource(QUrl())
        if self._speech_current_path is not None:
            Path(self._speech_current_path).unlink(missing_ok=True)
            self._speech_current_path = None
        self._speech_stop_button.hide()
        self._clear_speech_status()

    def _clear_speech_status(self) -> None:
        if self.statusBar().currentMessage().startswith(
            ("Gerando resposta de áudio", "Reproduzindo resposta de áudio")
        ):
            self.statusBar().clearMessage()

    def refresh_models(self) -> None:
        if self._probe_worker is not None:
            return
        self._set_ollama_status("Ollama: verificando…", "sync-alt", "#94A3B8")
        self._probe_worker = Worker(self.ollama.list_models)
        self._probe_worker.succeeded.connect(self._models_loaded)
        self._probe_worker.failed.connect(self._ollama_unavailable)
        self._probe_worker.finished.connect(self._probe_worker_finished)
        self._probe_worker.start()

    def _probe_worker_finished(self) -> None:
        self._probe_worker = None

    def _models_loaded(self, models: list[str]) -> None:
        self._set_ollama_status(
            "Ollama: conectado" if models else "Ollama: nenhum modelo instalado",
            "check-circle" if models else "exclamation-circle",
            "#34D399" if models else "#FBBF24",
        )
        selected = self.settings.ollama_model
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItems(models)
        if selected in models:
            self.model_combo.setCurrentIndex(models.index(selected))
        elif models:
            self.model_combo.setCurrentIndex(0)
            self.settings.set("ollama_model", models[0])
        else:
            self.model_combo.setCurrentIndex(-1)
        self.model_combo.setEnabled(
            bool(models) and self._chat_worker is None
        )
        self.model_combo.blockSignals(False)

    def _ollama_unavailable(self, _context, _message: str) -> None:
        self._set_ollama_status("Ollama: indisponível", "times-circle", "#F87171")
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.setEnabled(False)
        self.model_combo.blockSignals(False)

    def _save_model(self, model: str) -> None:
        if model and self.model_combo.findText(model) >= 0:
            self.settings.set("ollama_model", model)

    def _set_ollama_status(self, text: str, icon: str, color: str) -> None:
        self.ollama_status.setText(text)
        self.ollama_status.setStyleSheet(f"color: {color};")
        self.ollama_status_icon.setPixmap(
            qta.icon(f"fa5s.{icon}", color=color).pixmap(13, 13)
        )

    def new_conversation(self) -> None:
        if self._chat_worker is not None:
            return
        self.conversation_id = self.assistant.new_conversation()
        self._load_conversations()
        self._load_history()

    def _load_conversations(self) -> None:
        self.conversation_list.blockSignals(True)
        self.conversation_list.clear()
        current = None
        for conversation in self.database.list_conversations():
            item = QListWidgetItem(conversation["title"])
            item.setData(Qt.UserRole, conversation["id"])
            item.setToolTip(conversation["title"])
            self.conversation_list.addItem(item)
            if conversation["id"] == self.conversation_id:
                current = item
        self.conversation_list.setCurrentItem(current)
        self.conversation_list.blockSignals(False)

    def _select_conversation(self, current: QListWidgetItem | None, _previous) -> None:
        if current is None:
            return
        conversation_id = current.data(Qt.UserRole)
        if conversation_id == self.conversation_id or self._chat_worker is not None:
            return
        self.conversation_id = conversation_id
        self._load_history()

    def _conversation_menu(self, position) -> None:
        item = self.conversation_list.itemAt(position)
        if item is None:
            return
        menu = QMenu(self)
        rename = menu.addAction(self._icon("fa5s.pencil-alt"), "Renomear")
        delete = menu.addAction(self._icon("fa5s.trash-alt"), "Excluir")
        action = menu.exec(self.conversation_list.mapToGlobal(position))
        if action == rename:
            self.rename_conversation(item.data(Qt.UserRole))
        elif action == delete:
            self._delete_conversation(item.data(Qt.UserRole))

    def rename_conversation(self, conversation_id: int | None = None) -> None:
        conversation_id = conversation_id or self.conversation_id
        current = self.database.conversation(conversation_id)
        if not current:
            return
        title, accepted = QInputDialog.getText(
            self, "Renomear conversa", "Título:", text=current["title"]
        )
        if not accepted:
            return
        try:
            self.database.set_conversation_title(conversation_id, title)
        except (ValueError, VaultError) as error:
            QMessageBox.warning(self, "Título inválido", str(error))
            return
        if conversation_id == self.conversation_id:
            self.title_label.setText(title.strip())
        self._load_conversations()

    def _delete_conversation(self, conversation_id: int) -> None:
        answer = QMessageBox.question(
            self,
            "Excluir conversa",
            "Excluir esta conversa e suas mensagens? Notas e arquivos do Vault não serão alterados.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.database.delete_conversation(conversation_id)
        if conversation_id == self.conversation_id:
            self.conversation_id = self.database.latest_conversation()
        self._load_conversations()
        self._load_history()

    def open_settings(self) -> None:
        dialog = QDialog(self)
        dialog.installEventFilter(self)
        dialog.setWindowTitle("Configurações")
        dialog.resize(600, 780)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Endereço do Ollama"))
        ollama_url = QLineEdit(self.settings.ollama_url)
        layout.addWidget(ollama_url)

        layout.addWidget(QLabel("Dispositivo de saída de áudio"))
        audio_row = QHBoxLayout()
        audio_output = QComboBox()
        audio_output.setMinimumWidth(280)
        audio_output.setAccessibleName("Dispositivo de saída de áudio")
        audio_row.addWidget(audio_output, 1)
        refresh_audio = QPushButton(self._icon("fa5s.sync-alt"), " Atualizar")
        refresh_audio.clicked.connect(
            lambda: self._populate_audio_outputs(
                audio_output, audio_output.currentData()
            )
        )
        audio_row.addWidget(refresh_audio)
        layout.addLayout(audio_row)
        layout.addWidget(QLabel("Dispositivo de entrada de áudio"))
        input_audio_row = QHBoxLayout()
        audio_input = QComboBox()
        audio_input.setMinimumWidth(280)
        audio_input.setAccessibleName("Dispositivo de entrada de áudio")
        input_audio_row.addWidget(audio_input, 1)
        refresh_input_audio = QPushButton(self._icon("fa5s.sync-alt"), " Atualizar")
        refresh_input_audio.clicked.connect(
            lambda: self._populate_audio_inputs(
                audio_input, audio_input.currentData()
            )
        )
        input_audio_row.addWidget(refresh_input_audio)
        layout.addLayout(input_audio_row)
        input_audio_notice = QLabel(
            "A entrada escolhida será usada para gravar a fala; a captura usa PortAudio."
        )
        input_audio_notice.setObjectName("muted")
        input_audio_notice.setWordWrap(True)
        layout.addWidget(input_audio_notice)
        self._active_input_audio_combo = audio_input
        self._active_input_audio_status = QLabel("")
        self._active_input_audio_status.setObjectName("muted")
        self._active_input_audio_status.setWordWrap(True)
        layout.addWidget(self._active_input_audio_status)
        self._populate_audio_inputs(
            audio_input, self.settings.get("audio_input_id")
        )
        audio_notice = QLabel(
            "A saída escolhida será usada pelo TTS local, sem alterar o dispositivo padrão do Windows."
        )
        audio_notice.setObjectName("muted")
        audio_notice.setWordWrap(True)
        layout.addWidget(audio_notice)
        self._audio_settings_status = QLabel("")
        self._audio_settings_status.setObjectName("muted")
        self._audio_settings_status.setWordWrap(True)
        layout.addWidget(self._audio_settings_status)
        self._active_audio_combo = audio_output
        self._active_audio_status = self._audio_settings_status
        self._populate_audio_outputs(audio_output, self.settings.get("audio_output_id"))
        self._telegram_dialog = dialog

        layout.addWidget(QLabel("Modelo de voz Piper (.onnx)"))
        piper_model_row = QHBoxLayout()
        piper_model = QLineEdit(self.settings.get("piper_model_path"))
        piper_model.setPlaceholderText("Selecione um modelo Piper em português")
        piper_model.setAccessibleName("Modelo de voz Piper")
        piper_model_row.addWidget(piper_model, 1)
        browse_piper_model = QPushButton(
            self._icon("fa5s.folder-open"), " Selecionar modelo"
        )
        browse_piper_model.clicked.connect(
            lambda: self._choose_piper_model(piper_model)
        )
        piper_model_row.addWidget(browse_piper_model)
        layout.addLayout(piper_model_row)
        piper_notice = QLabel(
            "Use um modelo Piper .onnx com o arquivo .onnx.json correspondente na mesma pasta."
        )
        piper_notice.setObjectName("muted")
        piper_notice.setWordWrap(True)
        layout.addWidget(piper_notice)

        layout.addWidget(QLabel("Diretório do Vault do Obsidian"))
        path_row = QHBoxLayout()
        vault_path = QLineEdit(self.settings.get("vault_path"))
        vault_path.setPlaceholderText("Nenhum Vault configurado")
        path_row.addWidget(vault_path, 1)
        browse = QPushButton(self._icon("fa5s.folder-open"), " Selecionar pasta")
        browse.clicked.connect(
            lambda: self._choose_vault_directory(vault_path)
        )
        path_row.addWidget(browse)
        layout.addLayout(path_row)
        voice_response = QCheckBox("Responder por áudio")
        voice_response.setChecked(
            self.settings.get("voice_response_enabled").lower() == "true"
        )
        voice_response.setEnabled(self._speech_supported)
        layout.addWidget(voice_response)
        progressive_audio = QCheckBox("Reproduzir áudio conforme o texto é gerado")
        progressive_audio.setChecked(
            self.settings.get("streaming_audio").lower() == "true"
        )
        progressive_audio.setEnabled(
            self._speech_supported and voice_response.isChecked()
        )
        voice_response.toggled.connect(
            lambda checked: progressive_audio.setEnabled(
                self._speech_supported and checked
            )
        )
        progressive_audio.setToolTip(
            "Fala frases conforme chegam do modelo; requer respostas por áudio."
        )
        layout.addWidget(progressive_audio)
        if not self._speech_supported:
            speech_notice = QLabel(
                "O backend de reprodução multimídia não está disponível neste sistema."
            )
            speech_notice.setObjectName("muted")
            speech_notice.setWordWrap(True)
            layout.addWidget(speech_notice)
        layout.addWidget(QLabel("Máximo de chamadas de ferramenta por resposta"))
        tool_iterations = QSpinBox()
        tool_iterations.setRange(1, 8)
        try:
            tool_iterations.setValue(int(self.settings.get("tool_iteration_limit")))
        except ValueError:
            tool_iterations.setValue(4)
        layout.addWidget(tool_iterations)

        layout.addSpacing(8)
        layout.addWidget(QLabel("Integração com Telegram"))
        token_row = QHBoxLayout()
        telegram_token = QLineEdit()
        telegram_token.setEchoMode(QLineEdit.EchoMode.Password)
        telegram_token.setPlaceholderText("Token armazenado com segurança")
        telegram_token.setAccessibleName("Token do bot do Telegram")
        telegram_token.installEventFilter(self)
        self._telegram_token_field = telegram_token
        token_row.addWidget(telegram_token, 1)
        show_token = QPushButton(self._icon("fa5s.eye"), "")
        show_token.setToolTip("Mantenha pressionado para mostrar o token")
        show_token.setAccessibleName("Manter pressionado para revelar o token")
        show_token.setAccessibleDescription(
            "Revela o token somente enquanto o botão estiver pressionado."
        )
        show_token.pressed.connect(
            lambda: self._show_telegram_token(telegram_token)
        )
        show_token.released.connect(
            lambda: self._hide_telegram_token(telegram_token)
        )
        token_row.addWidget(show_token)
        layout.addLayout(token_row)
        try:
            configured = bool(secure_store.load_telegram_token())
            telegram_status = (
                "Token configurado. A conexão com o Telegram ainda não está implementada."
                if configured
                else "Token não configurado. Nenhuma chamada ao Telegram será feita."
            )
        except secure_store.SecureStoreError:
            telegram_status = "Armazenamento seguro indisponível neste sistema."
        self._telegram_status_label = QLabel(telegram_status)
        self._telegram_status_label.setObjectName("muted")
        self._telegram_status_label.setWordWrap(True)
        layout.addWidget(self._telegram_status_label)

        dialog.finished.connect(
            lambda _result: self._hide_telegram_token(telegram_token)
        )
        dialog.finished.connect(self._settings_dialog_finished)
        buttons = QHBoxLayout()
        buttons.addStretch()
        save = QPushButton("Salvar")
        save.setObjectName("primary")
        save.clicked.connect(dialog.accept)
        buttons.addWidget(save)
        layout.addLayout(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            url = ollama_url.text().strip().rstrip("/")
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError(
                    "A URL do Ollama deve começar com http:// ou https://."
                )
            chosen_path = vault_path.text().strip()
            if chosen_path:
                Vault(chosen_path)
            selected_audio_id = audio_output.currentData()
            selected_input_audio_id = audio_input.currentData()
            selected_piper_model = piper_model.text().strip()
            if selected_piper_model and not Path(selected_piper_model).is_file():
                raise ValueError("O arquivo do modelo Piper não foi encontrado.")
            if selected_piper_model and not selected_piper_model.lower().endswith(".onnx"):
                raise ValueError("O modelo de voz Piper deve ser um arquivo .onnx.")
            entered_token = telegram_token.text().strip()
            if entered_token:
                if not re.fullmatch(r"\d{5,12}:[A-Za-z0-9_-]{20,128}", entered_token):
                    raise ValueError("O formato do token do Telegram parece inválido.")
                secure_store.save_telegram_token(entered_token)
            self.settings.set("ollama_url", url)
            self.settings.set("vault_path", chosen_path)
            self.settings.set(
                "voice_response_enabled", str(voice_response.isChecked()).lower()
            )
            self.settings.set(
                "streaming_audio", str(progressive_audio.isChecked()).lower()
            )
            self.settings.set("tool_iteration_limit", str(tool_iterations.value()))
            if isinstance(selected_audio_id, str):
                self.settings.set("audio_output_id", selected_audio_id)
            if isinstance(selected_input_audio_id, str):
                self.settings.set("audio_input_id", selected_input_audio_id)
            self.settings.set("piper_model_path", selected_piper_model)
        except ValueError as error:
            QMessageBox.warning(self, "Configuração inválida", str(error))
            return
        except secure_store.SecureStoreError as error:
            QMessageBox.warning(self, "Armazenamento seguro indisponível", str(error))
            return
        self.ollama.base_url = self.settings.ollama_url
        self.refresh_models()

    def _audio_outputs_changed(self) -> None:
        combo = getattr(self, "_active_audio_combo", None)
        if combo is not None and self._active_audio_status is not None:
            self._populate_audio_outputs(combo, combo.currentData())
        input_combo = getattr(self, "_active_input_audio_combo", None)
        if input_combo is not None and self._active_input_audio_status is not None:
            self._populate_audio_inputs(input_combo, input_combo.currentData())

    def _settings_dialog_finished(self, _result: int) -> None:
        self._active_audio_combo = None
        self._active_audio_status = None
        self._active_input_audio_combo = None
        self._active_input_audio_status = None
        self._telegram_dialog = None
        self._telegram_token_field = None

    def _populate_audio_inputs(
        self, combo: QComboBox, selected_id: str | None
    ) -> None:
        saved_id = selected_id or "default"
        try:
            import sounddevice as sd
        except ImportError:
            available = []
            unavailable_message = (
                "A lista de microfones não está disponível; a entrada padrão será usada."
            )
        else:
            try:
                available = input_devices(sd)
                unavailable_message = ""
            except sd.PortAudioError as error:
                logger.warning(
                    "Falha ao enumerar dispositivos de entrada: %s",
                    type(error).__name__,
                )
                available = []
                unavailable_message = (
                    "Não foi possível enumerar os microfones; a entrada padrão será usada."
                )
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Padrão do sistema", "default")
        for identifier, description in available:
            combo.addItem(description, identifier)
        available_ids = {identifier for identifier, _description in available}
        if saved_id != "default" and saved_id not in available_ids:
            combo.addItem(f"Indisponível: {saved_id}", saved_id)
            unavailable_message = (
                "O dispositivo salvo não está disponível. A entrada padrão será usada temporariamente."
            )
        index = combo.findData(saved_id)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)
        status = getattr(self, "_active_input_audio_status", None)
        if status is not None:
            status.setText(
                unavailable_message
                or (
                    "Nenhum microfone foi encontrado; a entrada padrão será usada."
                    if not available
                    else ""
                )
            )

    @staticmethod
    def _choose_piper_model(field: QLineEdit) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            field.window(),
            "Selecionar modelo de voz Piper",
            field.text(),
            "Modelos Piper (*.onnx)",
        )
        if path:
            field.setText(path)

    def _populate_audio_outputs(self, combo: QComboBox, selected_id: str | None) -> None:
        saved_id = selected_id or "default"
        outputs = self.media_devices.audioOutputs() if self.media_devices is not None else []
        available: dict[str, str] = {}
        for device in outputs:
            identifier = output_identifier(device)
            available[identifier] = device.description()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Padrão do Windows", "default")
        for identifier, description in available.items():
            combo.addItem(description, identifier)
        if saved_id != "default" and saved_id not in available:
            combo.addItem(f"Indisponível: {saved_id}", saved_id)
            self._active_audio_status.setText(
                "O dispositivo salvo não está disponível. O padrão do sistema será usado temporariamente."
            )
        else:
            if self.media_devices is None:
                status = (
                    "A lista de dispositivos não está disponível; a saída padrão do sistema será usada."
                )
            else:
                status = (
                    "Nenhum dispositivo de saída foi encontrado; a saída padrão do sistema será usada."
                    if not outputs
                    else ""
                )
            self._active_audio_status.setText(status)
        index = combo.findData(saved_id)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def _show_telegram_token(self, field: QLineEdit) -> None:
        self._showing_saved_telegram_token = False
        if not field.text():
            try:
                token = secure_store.load_telegram_token()
            except secure_store.SecureStoreError:
                self.statusBar().showMessage(
                    "Não foi possível acessar o armazenamento seguro.", 5000
                )
                return
            if token:
                field.setText(token)
                self._showing_saved_telegram_token = True
        field.setEchoMode(QLineEdit.EchoMode.Normal)

    def _hide_telegram_token(self, field: QLineEdit) -> None:
        field.setEchoMode(QLineEdit.EchoMode.Password)
        if getattr(self, "_showing_saved_telegram_token", False):
            field.clear()
        self._showing_saved_telegram_token = False

    def _choose_vault_directory(self, field: QLineEdit) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Selecionar Vault do Obsidian", field.text()
        )
        if not directory:
            return
        try:
            Vault(directory)
        except VaultError as error:
            QMessageBox.warning(self, "Vault inválido", str(error))
            return
        field.setText(str(Path(directory).resolve()))

    def open_vault(self) -> None:
        directory = self.settings.get("vault_path")
        if not directory:
            answer = QMessageBox.information(
                self,
                "Configurar Vault",
                "Configure o diretório do Vault nas configurações para começar.",
                QMessageBox.Ok | QMessageBox.Cancel,
                QMessageBox.Ok,
            )
            if answer == QMessageBox.Ok:
                self.open_settings()
            return
        try:
            dialog = VaultDialog(self, Vault(directory))
        except VaultError as error:
            QMessageBox.warning(self, "Vault indisponível", str(error))
            return
        dialog.exec()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._stop_speech()
        if self._chat_worker is not None:
            self._chat_worker.cancel()
        if self._audio_worker is not None:
            self._cancel_ptt()
        workers = [*self._title_workers]
        if self._chat_worker is not None:
            workers.append(self._chat_worker)
        if self._audio_worker is not None:
            workers.append(self._audio_worker)
        if self._speech_worker is not None:
            workers.append(self._speech_worker)
        if self._probe_worker is not None:
            workers.append(self._probe_worker)
        if any(worker.isRunning() for worker in workers):
            self.ollama_status.setText("Aguardando operações em andamento…")
            event.ignore()
            QTimer.singleShot(100, self.close)
            return
        super().closeEvent(event)
