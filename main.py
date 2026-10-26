from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from ai.ollama_client import OllamaClient
from app.main_window import MainWindow
from config.settings import Settings, load_local_environment
from core.assistant import Assistant
from storage.database import Database


def main() -> int:
    load_local_environment()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    database = Database()
    settings = Settings(database)
    client = OllamaClient(settings.ollama_url)
    assistant = Assistant(database, client, settings)

    application = QApplication(sys.argv)
    application.setApplicationName("Jager")
    window = MainWindow(assistant, client, settings, database)
    window.show()
    result = application.exec()
    database.close()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
