from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from storage.database import Database


def load_local_environment(path: str | Path = ".env") -> None:
    env_file = Path(path)
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() in {"JAGER_OLLAMA_URL", "JAGER_OLLAMA_MODEL"}:
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


class Settings:
    DEFAULTS = {
        "ollama_url": "http://localhost:11434",
        "ollama_model": "qwen3:1.7b",
    }

    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, key: str) -> str:
        default = self.DEFAULTS.get(key, "")
        return self.database.get_setting(key, os.environ.get(f"JAGER_{key.upper()}", default))

    def set(self, key: str, value: str) -> None:
        if key == "ollama_url":
            value = value.strip().rstrip("/")
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("A URL do Ollama deve começar com http:// ou https://.")
        self.database.set_setting(key, value)

    @property
    def ollama_url(self) -> str:
        return self.get("ollama_url")

    @property
    def ollama_model(self) -> str:
        return self.get("ollama_model")
