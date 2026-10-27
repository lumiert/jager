from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def data_directory() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    path = root / "Jager"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else data_directory() / "jager.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._initialize()

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL DEFAULT 'Nova conversa',
                    title_manual INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tool_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    policy TEXT NOT NULL,
                    authorization TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(conversations)")
            }
            if "title_manual" not in columns:
                self._connection.execute(
                    "ALTER TABLE conversations ADD COLUMN title_manual "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            self._connection.execute("PRAGMA user_version = 2")

    def create_conversation(self, title: str = "Nova conversa") -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT INTO conversations(title, created_at) VALUES (?, ?)",
                (title, _timestamp()),
            )
            return int(cursor.lastrowid)

    def list_conversations(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, title, created_at FROM conversations ORDER BY id DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    def conversation(self, conversation_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT id, title, title_manual FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            return dict(row) if row else None

    def has_messages(self, conversation_id: int) -> bool:
        with self._lock:
            return self._connection.execute(
                "SELECT 1 FROM messages WHERE conversation_id = ? LIMIT 1",
                (conversation_id,),
            ).fetchone() is not None

    def set_conversation_title(self, conversation_id: int, title: str) -> None:
        title = title.strip()
        if not title:
            raise ValueError("O título não pode ficar vazio.")
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE conversations SET title = ?, title_manual = 1 WHERE id = ?",
                (title, conversation_id),
            )
            if not cursor.rowcount:
                raise ValueError("A conversa não existe mais.")

    def set_automatic_title(self, conversation_id: int, title: str) -> bool:
        title = title.strip()
        if not title:
            return False
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE conversations SET title = ? "
                "WHERE id = ? AND title_manual = 0 AND title = 'Nova conversa'",
                (title, conversation_id),
            )
            return cursor.rowcount > 0

    def delete_conversation(self, conversation_id: int) -> bool:
        with self._lock, self._connection:
            exists = self._connection.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if not exists:
                return False
            self._connection.execute(
                "DELETE FROM messages WHERE conversation_id = ?", (conversation_id,)
            )
            self._connection.execute(
                "DELETE FROM conversations WHERE id = ?", (conversation_id,)
            )
            return True

    def latest_conversation(self) -> int:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT id FROM conversations ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return int(row["id"]) if row else self.create_conversation()

    def add_message(self, conversation_id: int, role: str, content: str) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError("Papel de mensagem inválido.")
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO messages(conversation_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                (conversation_id, role, content, _timestamp()),
            )

    def conversation_messages(self, conversation_id: int) -> list[dict[str, str]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
            return [{"role": row["role"], "content": row["content"]} for row in rows]

    def create_note(self, content: str) -> int:
        content = content.strip()
        if not content:
            raise ValueError("A nota não pode ficar vazia.")
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT INTO notes(content, created_at) VALUES (?, ?)",
                (content, _timestamp()),
            )
            return int(cursor.lastrowid)

    def list_notes(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, content, created_at FROM notes ORDER BY id DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    def add_tool_audit(
        self,
        tool: str,
        policy: str,
        authorization: str,
        status: str,
        duration_ms: int,
        summary: str,
        error: str = "",
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO tool_audit(created_at, tool, policy, authorization, "
                "status, duration_ms, summary, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_timestamp(), tool, policy, authorization, status, duration_ms,
                 summary[:600], error[:300]),
            )

    def list_tool_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM tool_audit ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()
