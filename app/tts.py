from __future__ import annotations

import logging
import os
import tempfile
import threading
import wave
from pathlib import Path

from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)

_voice_lock = threading.Lock()
_voice_cache: dict[str, object] = {}


def _load_voice(model_path: str):
    # Loading the ONNX model per sentence made playback slow and choppy.
    from piper import PiperVoice

    with _voice_lock:
        key = str(Path(model_path).resolve())
        voice = _voice_cache.get(key)
        if voice is None:
            _voice_cache.clear()
            voice = PiperVoice.load(model_path)
            _voice_cache[key] = voice
        return voice


class SpeechSynthesisWorker(QThread):
    ready = Signal(str)
    failed = Signal(str)

    def __init__(self, text: str, model_path: str) -> None:
        super().__init__()
        self.text = text.strip()
        self.model_path = model_path.strip()
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
            if not self.model_path:
                raise RuntimeError(
                    "Selecione um modelo de voz Piper nas configurações."
                )
            model_path = Path(self.model_path)
            if not model_path.is_file():
                raise RuntimeError("O arquivo do modelo Piper não foi encontrado.")
            if not Path(f"{model_path}.json").is_file():
                raise RuntimeError(
                    "O arquivo .onnx.json do modelo Piper não foi encontrado."
                )
            try:
                import piper  # noqa: F401
            except ImportError as error:
                raise RuntimeError("O Piper TTS não está instalado.") from error

            descriptor, path = tempfile.mkstemp(suffix=".wav")
            os.close(descriptor)
            self._temporary_path = Path(path)
            voice = _load_voice(str(model_path))
            with wave.open(str(self._temporary_path), "wb") as audio_file:
                voice.synthesize_wav(self.text, audio_file)
            if self._cancelled.is_set():
                return
            if not self._temporary_path.is_file() or self._temporary_path.stat().st_size <= 44:
                raise RuntimeError("O Piper não gerou áudio reproduzível.")
            self.ready.emit(str(self._temporary_path))
            self._temporary_path = None
        except Exception as error:
            if not self._cancelled.is_set():
                logger.warning("Falha na síntese Piper: %s", type(error).__name__)
                message = (
                    str(error)
                    if isinstance(error, RuntimeError)
                    else "Não foi possível gerar a resposta de áudio."
                )
                self.failed.emit(message)
        finally:
            if self._temporary_path is not None:
                self._temporary_path.unlink(missing_ok=True)
