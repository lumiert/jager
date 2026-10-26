from __future__ import annotations

import re

from ai.ollama_client import OllamaClient
from config.settings import Settings
from storage.database import Database
from tools.windows import open_application, open_folder, search_web


class Assistant:
    def __init__(self, database: Database, ollama: OllamaClient, settings: Settings) -> None:
        self.database = database
        self.ollama = ollama
        self.settings = settings

    def new_conversation(self) -> int:
        return self.database.create_conversation()

    def history(self, conversation_id: int) -> list[dict[str, str]]:
        return self.database.conversation_messages(conversation_id)

    def respond(self, conversation_id: int, text: str) -> str:
        text = text.strip()
        if not text:
            return "Digite uma mensagem para conversar comigo."
        self.database.add_message(conversation_id, "user", text)
        answer = self._local_action(text)
        if answer is None:
            answer = self.ollama.chat(
                self.settings.ollama_model,
                self.database.conversation_messages(conversation_id)[-12:],
            )
        self.database.add_message(conversation_id, "assistant", answer)
        return answer

    def _local_action(self, text: str) -> str | None:
        note_match = re.search(
            r"(?:anote|crie uma nota|adicione uma nota|salve uma nota)\s*(?:que|:)?\s*(.+)",
            text,
            re.IGNORECASE,
        )
        if note_match:
            note_id = self.database.create_note(note_match.group(1))
            return f"Anotei: “{note_match.group(1).strip()}” (nota {note_id})."
        if re.search(r"\b(minhas notas|listar notas|mostre as notas)\b", text, re.IGNORECASE):
            notes = self.database.list_notes()
            if not notes:
                return "Você ainda não tem notas salvas."
            return "Suas notas:\n" + "\n".join(
                f'{note["id"]}. {note["content"]}' for note in notes
            )

        task_match = re.search(
            r"(?:crie uma tarefa|adicione uma tarefa|nova tarefa|preciso fazer)\s*(?:que|:)?\s*(.+)",
            text,
            re.IGNORECASE,
        )
        if task_match:
            task_id = self.database.create_task(task_match.group(1))
            return f"Tarefa criada: “{task_match.group(1).strip()}” (tarefa {task_id})."
        completion_match = re.search(
            r"(?:conclua|concluir|marque como conclu[ií]da)\s+(?:a\s+)?tarefa\s+#?(\d+)",
            text,
            re.IGNORECASE,
        )
        if completion_match:
            if self.database.complete_task(int(completion_match.group(1))):
                return f"Tarefa {completion_match.group(1)} concluída."
            return f"Não encontrei uma tarefa pendente com o número {completion_match.group(1)}."
        if re.search(r"\b(minhas tarefas|tarefas pendentes|o que tenho para fazer)\b", text, re.IGNORECASE):
            tasks = self.database.list_tasks(pending_only=True)
            if not tasks:
                return "Você não tem tarefas pendentes."
            return "Tarefas pendentes:\n" + "\n".join(
                f'{task["id"]}. {task["title"]}' for task in tasks
            )

        app_match = re.search(
            r"\b(?:abra|abrir|inicie)\s+(?:o\s+|a\s+)?(navegador|explorador|vs code|mixxx)\b",
            text,
            re.IGNORECASE,
        )
        if app_match:
            try:
                open_application(app_match.group(1))
                return f"Abri {app_match.group(1)}."
            except (RuntimeError, ValueError, FileNotFoundError) as error:
                return str(error)
        folder_match = re.search(
            r"\b(?:abra|abrir)\s+(?:a\s+)?pasta\s+(downloads|documentos|área de trabalho|desktop|músicas)\b",
            text,
            re.IGNORECASE,
        )
        if folder_match:
            try:
                open_folder(folder_match.group(1))
                return f"Abri a pasta {folder_match.group(1)}."
            except (RuntimeError, ValueError, FileNotFoundError) as error:
                return str(error)
        search_match = re.search(
            r"\b(?:pesquise|pesquisar|busque|buscar)\s+(?:na internet\s+)?(?:por\s+)?(.+)",
            text,
            re.IGNORECASE,
        )
        if search_match:
            try:
                search_web(search_match.group(1))
                return f"Abri uma pesquisa no navegador por “{search_match.group(1)}”."
            except (RuntimeError, ValueError) as error:
                return str(error)
        return None
