from __future__ import annotations

import logging
import os
import tempfile
import threading
import wave
from pathlib import Path

from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)

# On Windows without Developer Mode the Hugging Face cache cannot use symlinks and
# silently falls back to file copies. That works without administrator rights, so the
# advisory is redundant noise; the behavior is logged once in _load_model instead.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

WHISPER_MODEL_NAME = "tiny"
_model_cache: dict[type, object] = {}
_model_lock = threading.Lock()


def _load_model(model_class, on_download=None):
    """Return the process-wide Whisper model, loading it lazily.

    The local cache is tried first (local_files_only), so a cached model causes no
    network request. Only a missing or incomplete cache triggers a download, which
    runs in the calling worker thread. No Hugging Face token is required.
    """
    with _model_lock:
        cached = _model_cache.get(model_class)
        if cached is not None:
            return cached
        options = {"device": "cpu", "compute_type": "int8"}
        try:
            model = model_class(WHISPER_MODEL_NAME, local_files_only=True, **options)
            logger.info("Modelo Whisper '%s' carregado do cache local.", WHISPER_MODEL_NAME)
        except Exception as cache_error:
            logger.info(
                "Cache local do Whisper ausente ou incompleto (%s); baixando.",
                type(cache_error).__name__,
            )
            if on_download is not None:
                on_download()
            try:
                model = model_class(WHISPER_MODEL_NAME, **options)
            except Exception as error:
                logger.warning("Falha ao baixar o modelo Whisper: %s", type(error).__name__)
                raise RuntimeError(
                    "Não foi possível obter o modelo de reconhecimento de voz. "
                    "Verifique a conexão com a internet; se já o baixou, o cache pode estar incompleto."
                ) from error
        _model_cache[model_class] = model
        return model


class PushToTalkWorker(QThread):
    """Captures microphone audio, then runs local Portuguese transcription."""

    state_changed = Signal(str)
    transcribed = Signal(str)
    failed = Signal(str)

    def __init__(self, input_device_id: str = "default") -> None:
        super().__init__()
        self.input_device_id = input_device_id
        self._stop_requested = threading.Event()
        self._cancel_requested = threading.Event()
        self._lock = threading.Lock()
        self._audio = bytearray()
        self._stream = None

    def stop_recording(self) -> None:
        self._stop_requested.set()

    def cancel_recording(self) -> None:
        self._cancel_requested.set()
        self._stop_requested.set()

    def run(self) -> None:
        temporary_path: Path | None = None
        stream = None
        try:
            try:
                import sounddevice as sd
                from app.audio_devices import resolve_input_device
            except ImportError as error:
                raise RuntimeError(
                    "Captura de áudio indisponível. Instale as dependências do Jager."
                ) from error

            def receive(indata, _frames, _time, status) -> None:
                if status:
                    self.failed.emit("O dispositivo de áudio informou uma falha na captura.")
                    self._cancel_requested.set()
                    self._stop_requested.set()
                if not self._cancel_requested.is_set():
                    with self._lock:
                        self._audio.extend(indata.tobytes())

            try:
                stream = sd.InputStream(
                    samplerate=16000,
                    channels=1,
                    dtype="int16",
                    callback=receive,
                    device=resolve_input_device(sd, self.input_device_id),
                )
                with self._lock:
                    self._stream = stream
                stream.start()
            except Exception as error:
                raise RuntimeError(
                    "Não foi possível iniciar o microfone. Verifique a conexão e as permissões."
                ) from error

            self.state_changed.emit("recording")
            while not self._stop_requested.wait(0.05):
                if self.isInterruptionRequested():
                    self._cancel_requested.set()
                    break
            stream.stop()
            stream.close()
            stream = None
            with self._lock:
                self._stream = None

            if self._cancel_requested.is_set():
                self.state_changed.emit("cancelled")
                return
            with self._lock:
                audio = bytes(self._audio)
            if len(audio) < 16000:
                raise RuntimeError("A gravação ficou vazia ou curta demais para transcrever.")

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temporary:
                temporary_path = Path(temporary.name)
            with wave.open(str(temporary_path), "wb") as recording:
                recording.setnchannels(1)
                recording.setsampwidth(2)
                recording.setframerate(16000)
                recording.writeframes(audio)

            self.state_changed.emit("transcribing")
            try:
                from faster_whisper import WhisperModel
            except ImportError as error:
                raise RuntimeError(
                    "Reconhecimento local indisponível. Instale faster-whisper."
                ) from error

            model = _load_model(
                WhisperModel, lambda: self.state_changed.emit("downloading_model")
            )
            self.state_changed.emit("transcribing")
            segments, _info = model.transcribe(
                str(temporary_path),
                language="pt",
                beam_size=1,
                vad_filter=False,
            )
            text = " ".join(segment.text.strip() for segment in segments).strip()
            if not text:
                raise RuntimeError("Não foi possível reconhecer fala nessa gravação.")
            if not self._cancel_requested.is_set():
                self.transcribed.emit(text)
        except Exception as error:
            if not self._cancel_requested.is_set():
                self.failed.emit(str(error))
        finally:
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    logger.exception("Falha ao interromper a captura do microfone")
                try:
                    stream.close()
                except Exception:
                    logger.exception("Falha ao liberar o dispositivo de microfone")
            with self._lock:
                self._stream = None
                self._audio.clear()
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
