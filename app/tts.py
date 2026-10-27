from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)


class SpeechSynthesisWorker(QThread):
    ready = Signal(str)
    failed = Signal(str)

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text.strip()
        self._cancelled = threading.Event()
        self._temporary_path: Path | None = None

    def cancel(self) -> None:
        self._cancelled.set()

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def run(self) -> None:
        engine = None
        try:
            if not self.text:
                raise RuntimeError("Não há texto para transformar em fala.")
            try:
                import pyttsx3
            except ImportError as error:
                raise RuntimeError("O mecanismo de voz local não está instalado.") from error

            descriptor, path = tempfile.mkstemp(suffix=".wav")
            os.close(descriptor)
            self._temporary_path = Path(path)
            self._temporary_path.unlink()
            engine = pyttsx3.init(driverName="sapi5" if os.name == "nt" else None)
            self._select_portuguese_voice(engine)
            engine.save_to_file(self.text, str(self._temporary_path))
            engine.runAndWait()
            if self._cancelled.is_set():
                return
            if not self._temporary_path.is_file() or self._temporary_path.stat().st_size <= 44:
                raise RuntimeError("O mecanismo de voz não gerou áudio reproduzível.")
            self.ready.emit(str(self._temporary_path))
            self._temporary_path = None
        except Exception as error:
            if not self._cancelled.is_set():
                logger.warning("Falha na síntese local de fala: %s", type(error).__name__)
                self.failed.emit("Não foi possível gerar a resposta de áudio.")
        finally:
            if engine is not None:
                try:
                    engine.stop()
                except Exception:
                    logger.exception("Falha ao encerrar o mecanismo de voz")
            if self._temporary_path is not None:
                self._temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _select_portuguese_voice(engine) -> None:
        for voice in engine.getProperty("voices"):
            metadata = " ".join(
                [
                    str(getattr(voice, "id", "")),
                    str(getattr(voice, "name", "")),
                    *(str(language) for language in getattr(voice, "languages", [])),
                ]
            ).casefold()
            if "pt" in metadata or "portugu" in metadata:
                engine.setProperty("voice", voice.id)
                return
