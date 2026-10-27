import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from app.audio import PushToTalkWorker
from app.audio_devices import (
    apply_output_device,
    output_identifier,
    resolve_output_device,
)
from app.tts import SpeechSynthesisWorker


class FakeDevice:
    def __init__(self, identifier: bytes, name: str) -> None:
        self.identifier = identifier
        self.name = name

    def id(self) -> bytes:
        return self.identifier

    def description(self) -> str:
        return self.name


class FakeMediaDevices:
    def __init__(self) -> None:
        self.default = FakeDevice(b"default-id", "Padrão")
        self.specific = FakeDevice(b"device-id", "Fone")

    def defaultAudioOutput(self) -> FakeDevice:
        return self.default

    def audioOutputs(self) -> list[FakeDevice]:
        return [self.default, self.specific]


class AudioDeviceTests(unittest.TestCase):
    def test_default_and_specific_devices_resolve_without_changing_system_default(self) -> None:
        devices = FakeMediaDevices()

        self.assertIs(resolve_output_device(devices, "default"), devices.default)
        self.assertIs(
            resolve_output_device(devices, output_identifier(devices.specific)),
            devices.specific,
        )
        self.assertIs(resolve_output_device(devices, "removed-device"), devices.default)
        self.assertIs(devices.defaultAudioOutput(), devices.default)

    def test_selected_device_is_applied_to_audio_output(self) -> None:
        devices = FakeMediaDevices()

        class Output:
            device = None

            def setDevice(self, device) -> None:
                self.device = device

        output = Output()
        selected = apply_output_device(
            output, devices, output_identifier(devices.specific)
        )

        self.assertIs(selected, devices.specific)
        self.assertIs(output.device, devices.specific)


class AudioCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    class Buffer:
        def __init__(self, value: bytes) -> None:
            self.value = value

        def tobytes(self) -> bytes:
            return self.value

    def _audio_modules(self, stream_class, model_class):
        sounddevice = types.ModuleType("sounddevice")
        sounddevice.InputStream = stream_class
        whisper = types.ModuleType("faster_whisper")
        whisper.WhisperModel = model_class
        return {"sounddevice": sounddevice, "faster_whisper": whisper}

    def test_local_speech_synthesis_returns_a_temporary_audio_file(self) -> None:
        voices = [types.SimpleNamespace(id="pt-BR", name="Portuguese")]

        class Engine:
            selected_voice = None

            def getProperty(self, _key):
                return voices

            def setProperty(self, _key, value):
                self.selected_voice = value

            def save_to_file(self, _text, path):
                Path(path).write_bytes(b"RIFF" + b"\x00" * 50)

            def runAndWait(self):
                pass

            def stop(self):
                pass

        fake_tts = types.ModuleType("pyttsx3")
        fake_tts.init = lambda **_kwargs: Engine()
        worker = SpeechSynthesisWorker("Olá, mundo.")
        files = []
        errors = []
        worker.ready.connect(files.append)
        worker.failed.connect(errors.append)
        with patch.dict(sys.modules, {"pyttsx3": fake_tts}):
            worker.start()
            worker.wait(5000)
            self.application.processEvents()

        self.assertEqual(errors, [])
        self.assertEqual(len(files), 1)
        self.assertTrue(Path(files[0]).is_file())
        Path(files[0]).unlink()

    def test_records_transcribes_off_thread_and_releases_microphone(self) -> None:
        streams = []
        main_thread = threading.get_ident()
        model_results = {}
        started = threading.Event()

        class Stream:
            def __init__(self, **kwargs) -> None:
                self.callback = kwargs["callback"]
                self.closed = False
                streams.append(self)

            def start(self) -> None:
                self.callback(AudioCaptureTests.Buffer(b"\x01\x00" * 16000), 16000, None, None)
                started.set()

            def stop(self) -> None:
                pass

            def close(self) -> None:
                self.closed = True

        class Model:
            def __init__(self, *_args, **_kwargs) -> None:
                model_results["thread_id"] = threading.get_ident()

            def transcribe(self, path: str, **_kwargs):
                self.path = Path(path)
                model_results["file_existed"] = self.path.is_file()
                return iter([types.SimpleNamespace(text=" Olá, Jager ")]), None

        worker = PushToTalkWorker()
        transcripts, errors = [], []
        worker.transcribed.connect(transcripts.append)
        worker.failed.connect(errors.append)
        with patch.dict(sys.modules, self._audio_modules(Stream, Model)):
            worker.start()
            self.assertTrue(started.wait(2))
            worker.stop_recording()
            worker.wait(5000)
            self.application.processEvents()

        self.assertEqual(transcripts, ["Olá, Jager"])
        self.assertEqual(errors, [])
        self.assertTrue(streams[0].closed)
        self.assertNotEqual(model_results["thread_id"], main_thread)
        self.assertTrue(model_results["file_existed"])

    def test_empty_recording_fails_and_cancel_does_not_transcribe(self) -> None:
        empty_started = threading.Event()

        class EmptyStream:
            def __init__(self, **kwargs) -> None:
                self.callback = kwargs["callback"]
                self.closed = False

            def start(self) -> None:
                empty_started.set()

            def stop(self) -> None:
                pass

            def close(self) -> None:
                self.closed = True

        worker = PushToTalkWorker()
        errors = []
        worker.failed.connect(errors.append)
        with patch.dict(sys.modules, self._audio_modules(EmptyStream, Mock)):
            worker.start()
            self.assertTrue(empty_started.wait(2))
            worker.stop_recording()
            worker.wait(5000)
            self.application.processEvents()
        self.assertTrue(errors)
        self.assertIn("vazia", errors[0])

        entered = threading.Event()

        class WaitingStream(EmptyStream):
            def start(self) -> None:
                entered.set()

        worker = PushToTalkWorker()
        transcripts = []
        worker.transcribed.connect(transcripts.append)
        with patch.dict(sys.modules, self._audio_modules(WaitingStream, Mock)):
            worker.start()
            self.assertTrue(entered.wait(2))
            worker.cancel_recording()
            worker.wait(5000)
            self.application.processEvents()
        self.assertEqual(transcripts, [])

    def test_microphone_permission_and_corrupt_audio_errors_are_reported(self) -> None:
        class DeniedStream:
            def __init__(self, **_kwargs) -> None:
                raise PermissionError("permission denied")

        worker = PushToTalkWorker()
        errors = []
        worker.failed.connect(errors.append)
        with patch.dict(sys.modules, self._audio_modules(DeniedStream, Mock)):
            worker.start()
            worker.wait(5000)
            self.application.processEvents()
        self.assertTrue(errors)
        self.assertIn("permissões", errors[0])

        started = threading.Event()
        temporary_files = []

        class Stream:
            def __init__(self, **kwargs) -> None:
                self.callback = kwargs["callback"]

            def start(self) -> None:
                self.callback(AudioCaptureTests.Buffer(b"\x01\x00" * 16000), 16000, None, None)
                started.set()

            def stop(self) -> None:
                pass

            def close(self) -> None:
                pass

        class CorruptModel:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def transcribe(self, path: str, **_kwargs):
                temporary_files.append(Path(path))
                raise ValueError("corrupt audio")

        worker = PushToTalkWorker()
        errors = []
        worker.failed.connect(errors.append)
        with patch.dict(sys.modules, self._audio_modules(Stream, CorruptModel)):
            worker.start()
            self.assertTrue(started.wait(2))
            worker.stop_recording()
            worker.wait(5000)
            self.application.processEvents()
        self.assertTrue(errors)
        self.assertFalse(temporary_files[0].exists())


if __name__ == "__main__":
    unittest.main()
