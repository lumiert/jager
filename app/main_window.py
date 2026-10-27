from __future__ import annotations

import base64
import logging
import threading
from pathlib import Path
from urllib.parse import urlsplit

import qtawesome as qta
from PySide6.QtCore import QTimer, Qt, QThread, Signal, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QKeyEvent
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
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ai.ollama_client import OllamaClient
from app.markdown_view import render_markdown
from app.theme import THEME
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

    def run(self) -> None:
        try:
            answer = self.assistant.respond_stream(
                self.conversation_id,
                self.text,
                lambda fragment: self.fragment.emit(self.conversation_id, fragment),
                self.cancelled,
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
        self._messages: list[dict[str, str]] = []
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(70)
        self._render_timer.timeout.connect(self._render_messages)
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
            a { color: #7CB7FF; }
            blockquote { border-left: 3px solid #3B82F6; margin-left: 8px; padding-left: 12px; color: #B9C9E2; }
            pre.code-block { background: #101A2B; border: 1px solid #263956; border-radius: 6px; padding: 12px; white-space: pre-wrap; }
            pre code { color: #D7E4F7; font-family: Consolas, monospace; }
            .copy-code { margin-top: -10px; font-size: 9pt; }
            table { border-collapse: collapse; } th, td { border: 1px solid #34445E; padding: 5px 8px; }
            hr { border: 0; border-top: 1px solid #34445E; }
            """
        )
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
        self.loading_indicator = QProgressBar()
        self.loading_indicator.setRange(0, 0)
        self.loading_indicator.setTextVisible(False)
        self.loading_indicator.setFixedWidth(80)
        self.loading_indicator.setFixedHeight(8)
        self.loading_indicator.hide()
        model_row.addWidget(self.loading_indicator)
        composer_layout.addLayout(model_row)

        message_row = QHBoxLayout()
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
        for message in self._messages:
            role = message["role"]
            label = "Você" if role == "user" else "Jager"
            class_name = "assistant" if role == "assistant" else "user"
            blocks.append(
                f'<p class="message-label {class_name}">{label}</p>'
                f'<div class="message-body">{render_markdown(message["content"])}</div>'
            )
        self.chat.setHtml("<html><body>" + "\n".join(blocks) + "</body></html>")
        scrollbar = self.chat.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

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
        if not text or self._chat_worker is not None:
            return
        conversation_id = self.conversation_id
        should_title = not self.database.has_messages(conversation_id)
        self.input.clear()
        self._messages.append({"role": "user", "content": text})
        self._messages.append({"role": "assistant", "content": ""})
        self._render_messages()
        self._set_busy(True)
        worker = ChatWorker(self.assistant, conversation_id, text)
        worker.fragment.connect(self._on_fragment)
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
        self.loading_indicator.hide()
        if self._messages and self._messages[-1]["role"] == "assistant":
            self._messages[-1]["content"] += fragment
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

    def _chat_worker_finished(self) -> None:
        self._chat_worker = None
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        self.send_button.setVisible(not busy)
        self.stop_button.setVisible(busy)
        self.stop_button.setEnabled(busy)
        self.input.setEnabled(not busy)
        self.model_combo.setEnabled(not busy and self.model_combo.count() > 0)
        self.conversation_list.setEnabled(not busy)
        self.loading_indicator.setVisible(busy)
        if busy:
            self.stop_button.setText("")
        else:
            self.loading_indicator.hide()

    def cancel_generation(self) -> None:
        if self._chat_worker is not None:
            self._chat_worker.cancel()
            self.stop_button.setEnabled(False)
            self.stop_button.setText("Interrompendo…")

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
        dialog.setWindowTitle("Configurações")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Endereço do Ollama"))
        ollama_url = QLineEdit(self.settings.ollama_url)
        layout.addWidget(ollama_url)
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
        progressive_audio = QCheckBox("Reproduzir áudio conforme o texto é gerado")
        progressive_audio.setChecked(
            self.settings.get("streaming_audio").lower() == "true"
        )
        progressive_audio.setEnabled(False)
        progressive_audio.setToolTip("Ativo após a integração com TTS ser implementada.")
        layout.addWidget(progressive_audio)
        layout.addWidget(
            QLabel("A reprodução de áudio ainda não está disponível nesta versão.")
        )
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
            self.settings.set("ollama_url", url)
            self.settings.set("vault_path", chosen_path)
            self.settings.set("streaming_audio", "false")
        except ValueError as error:
            QMessageBox.warning(self, "Configuração inválida", str(error))
            return
        self.ollama.base_url = self.settings.ollama_url
        self.refresh_models()

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
        if self._chat_worker is not None:
            self._chat_worker.cancel()
        workers = [*self._title_workers]
        if self._chat_worker is not None:
            workers.append(self._chat_worker)
        if self._probe_worker is not None:
            workers.append(self._probe_worker)
        if any(worker.isRunning() for worker in workers):
            self.ollama_status.setText("Aguardando operações em andamento…")
            event.ignore()
            QTimer.singleShot(100, self.close)
            return
        super().closeEvent(event)
