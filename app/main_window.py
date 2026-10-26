from __future__ import annotations

import logging
from html import escape

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTextBrowser,
    QInputDialog,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from ai.ollama_client import OllamaClient
from app.theme import THEME
from config.settings import Settings
from core.assistant import Assistant
from storage.database import Database

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
        self._chat_worker: Worker | None = None
        self._probe_worker: Worker | None = None
        self.setWindowTitle("Jager")
        self.resize(1050, 720)
        self.setStyleSheet(THEME)
        self._build_ui()
        self._load_history()
        self._load_conversations()
        self.refresh_models()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(225)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(16, 22, 16, 18)
        brand = QLabel("◈ JAGER")
        brand.setObjectName("brand")
        sidebar_layout.addWidget(brand)
        subtitle = QLabel("Assistente pessoal local")
        subtitle.setObjectName("subtitle")
        sidebar_layout.addWidget(subtitle)
        sidebar_layout.addSpacing(22)
        new_chat = QPushButton("＋  Nova conversa")
        new_chat.clicked.connect(self.new_conversation)
        sidebar_layout.addWidget(new_chat)
        notes_button = QPushButton("▤  Notas")
        notes_button.clicked.connect(lambda: self._show_local_query("Minhas notas"))
        sidebar_layout.addWidget(notes_button)
        tasks_button = QPushButton("☑  Tarefas")
        tasks_button.clicked.connect(lambda: self._show_local_query("Minhas tarefas"))
        sidebar_layout.addWidget(tasks_button)
        settings_button = QPushButton("⚙  Configurações")
        settings_button.clicked.connect(self.edit_ollama_url)
        sidebar_layout.addWidget(settings_button)
        sidebar_layout.addSpacing(14)
        sidebar_layout.addWidget(QLabel("CONVERSAS"))
        self.conversation_list = QListWidget()
        self.conversation_list.setMaximumHeight(190)
        self.conversation_list.currentItemChanged.connect(self._select_conversation)
        sidebar_layout.addWidget(self.conversation_list)
        sidebar_layout.addStretch()
        sidebar_layout.addWidget(QLabel("INTEGRAÇÕES"))
        self.ollama_status = QLabel("● Ollama: verificando…")
        self.ollama_status.setObjectName("muted")
        sidebar_layout.addWidget(self.ollama_status)
        telegram_status = QLabel("○ Telegram: fase futura")
        telegram_status.setObjectName("muted")
        sidebar_layout.addWidget(telegram_status)
        audio_status = QLabel("○ Áudio: fase futura")
        audio_status.setObjectName("muted")
        sidebar_layout.addWidget(audio_status)
        layout.addWidget(sidebar)

        main = QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        header = QFrame()
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(22, 14, 22, 14)
        heading = QVBoxLayout()
        title = QLabel("Jager")
        title.setStyleSheet("font-size: 14pt; font-weight: 600;")
        heading.addWidget(title)
        hint = QLabel("Suas conversas e dados permanecem neste computador.")
        hint.setObjectName("muted")
        heading.addWidget(hint)
        header_layout.addLayout(heading)
        header_layout.addStretch()
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setMinimumWidth(170)
        self.model_combo.setCurrentText(self.settings.ollama_model)
        self.model_combo.currentTextChanged.connect(self._save_model)
        header_layout.addWidget(QLabel("Modelo"))
        header_layout.addWidget(self.model_combo)
        refresh = QPushButton("Atualizar")
        refresh.clicked.connect(self.refresh_models)
        header_layout.addWidget(refresh)
        main.addWidget(header)

        self.chat = QTextBrowser()
        self.chat.setOpenExternalLinks(False)
        main.addWidget(self.chat, 1)

        composer = QFrame()
        composer.setObjectName("composer")
        composer_layout = QHBoxLayout(composer)
        composer_layout.setContentsMargins(20, 14, 20, 16)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Escreva uma mensagem…")
        self.input.returnPressed.connect(self.send_message)
        composer_layout.addWidget(self.input, 1)
        self.send_button = QPushButton("Enviar")
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self.send_message)
        composer_layout.addWidget(self.send_button)
        main.addWidget(composer)
        layout.addLayout(main, 1)
        self.setCentralWidget(root)

    def _load_history(self) -> None:
        for message in self.assistant.history(self.conversation_id):
            self._append_message(message["role"], message["content"])
        if not self.chat.toPlainText():
            self._append_message("assistant", "Olá! Sou o Jager. Como posso ajudar?")

    def _append_message(self, role: str, content: str) -> None:
        label = "Você" if role == "user" else "Jager"
        color = "#60A5FA" if role == "user" else "#34D399"
        body = escape(content).replace("\n", "<br>")
        self.chat.append(
            f'<p style="margin: 12px 0 4px; color: {color}; font-weight: 600;">{label}</p>'
            f'<p style="margin: 0 0 16px; line-height: 1.5;">{body}</p>'
        )

    def send_message(self) -> None:
        text = self.input.text().strip()
        if not text or self._chat_worker is not None:
            return
        conversation_id = self.conversation_id
        self.input.clear()
        self._append_message("user", text)
        self._set_busy(True)
        self._chat_worker = Worker(
            lambda: (
                conversation_id,
                self.assistant.respond(conversation_id, text),
            ),
            context=conversation_id,
        )
        self._chat_worker.succeeded.connect(self._on_answer)
        self._chat_worker.failed.connect(self._on_error)
        self._chat_worker.finished.connect(self._chat_worker_finished)
        self._chat_worker.start()

    def _on_answer(self, result: tuple[int, str]) -> None:
        conversation_id, answer = result
        if conversation_id != self.conversation_id:
            return
        self._append_message("assistant", answer)

    def _on_error(self, conversation_id: int, _message: str) -> None:
        if conversation_id != self.conversation_id:
            return
        self._append_message(
            "assistant",
            "Não consegui responder agora. Verifique se o Ollama está funcionando "
            "e tente novamente.",
        )

    def _chat_worker_finished(self) -> None:
        self._chat_worker = None
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        self.send_button.setEnabled(not busy)
        self.input.setEnabled(not busy)
        self.send_button.setText("Processando…" if busy else "Enviar")

    def refresh_models(self) -> None:
        if self._probe_worker is not None:
            return
        self.ollama_status.setText("● Ollama: verificando…")
        self._probe_worker = Worker(self.ollama.list_models)
        self._probe_worker.succeeded.connect(self._models_loaded)
        self._probe_worker.failed.connect(self._ollama_unavailable)
        self._probe_worker.finished.connect(self._probe_worker_finished)
        self._probe_worker.start()

    def _probe_worker_finished(self) -> None:
        self._probe_worker = None

    def _models_loaded(self, models: list[str]) -> None:
        self.ollama_status.setText("● Ollama: conectado")
        self.ollama_status.setStyleSheet("color: #34D399;")
        selected = self.settings.ollama_model
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItems(models)
        if selected in models:
            self.model_combo.setCurrentText(selected)
        else:
            self.model_combo.setCurrentText(selected)
        self.model_combo.blockSignals(False)
        if not models:
            self.ollama_status.setText("● Ollama: sem modelos instalados")
            self.ollama_status.setStyleSheet("color: #FBBF24;")

    def _ollama_unavailable(self, _context, _message: str) -> None:
        self.ollama_status.setText("● Ollama: desconectado")
        self.ollama_status.setStyleSheet("color: #F87171;")

    def _save_model(self, model: str) -> None:
        model = model.strip()
        if model:
            self.settings.set("ollama_model", model)

    def new_conversation(self) -> None:
        self.conversation_id = self.assistant.new_conversation()
        self.chat.clear()
        self._append_message("assistant", "Nova conversa iniciada. Como posso ajudar?")
        self._load_conversations()

    def _load_conversations(self) -> None:
        self.conversation_list.blockSignals(True)
        self.conversation_list.clear()
        current = None
        for conversation in self.database.list_conversations():
            item = QListWidgetItem(f"Conversa #{conversation['id']}")
            item.setData(256, conversation["id"])
            self.conversation_list.addItem(item)
            if conversation["id"] == self.conversation_id:
                current = item
        self.conversation_list.setCurrentItem(current)
        self.conversation_list.blockSignals(False)

    def _select_conversation(self, current: QListWidgetItem | None, _previous) -> None:
        if current is None:
            return
        conversation_id = current.data(256)
        if conversation_id == self.conversation_id:
            return
        self.conversation_id = conversation_id
        self.chat.clear()
        self._load_history()

    def edit_ollama_url(self) -> None:
        url, accepted = QInputDialog.getText(
            self,
            "Configurações",
            "Endereço do Ollama:",
            text=self.settings.ollama_url,
        )
        if not accepted:
            return
        try:
            self.settings.set("ollama_url", url)
        except ValueError as error:
            QMessageBox.warning(self, "Endereço inválido", str(error))
            return
        self.ollama.base_url = self.settings.ollama_url
        self.refresh_models()

    def _show_local_query(self, prompt: str) -> None:
        self.input.setText(prompt)
        self.send_message()
