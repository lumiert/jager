import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from app.audio import PushToTalkWorker
from app.audio_devices import (
    apply_output_device,
    input_devices,
    output_identifier,
    resolve_input_device,
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
    def test_input_devices_are_listed_and_saved_device_is_resolved(self) -> None:
        devices = [
            {"name": "Microfone", "max_input_channels": 1},
            {"name": "Saída", "max_input_channels": 0},
            {"name": "Headset", "max_input_channels": 2},
        ]
        sounddevice = types.SimpleNamespace(query_devices=lambda: devices)

        self.assertEqual(
            input_devices(sounddevice),
            [("0", "Microfone"), ("2", "Headset")],
        )
        self.assertIsNone(resolve_input_device(sounddevice, "default"))
        self.assertEqual(resolve_input_device(sounddevice, "2"), 2)
        self.assertIsNone(resolve_input_device(sounddevice, "1"))
        self.assertIsNone(resolve_input_device(sounddevice, "missing"))

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

    def test_piper_synthesis_returns_a_temporary_audio_file(self) -> None:
        class Voice:
            def synthesize_wav(self, text, audio_file) -> None:
                self.text = text
                audio_file.setnchannels(1)
                audio_file.setsampwidth(2)
                audio_file.setframerate(22050)
                audio_file.writeframes(b"\x00" * 64)

        fake_piper = types.ModuleType("piper")
        fake_piper.PiperVoice = types.SimpleNamespace(load=lambda _path: Voice())
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) / "pt.onnx"
            model_path.touch()
            Path(f"{model_path}.json").touch()
            worker = SpeechSynthesisWorker("Olá, mundo.", str(model_path))
            files = []
            errors = []
            worker.ready.connect(files.append)
            worker.failed.connect(errors.append)
            with patch.dict(sys.modules, {"piper": fake_piper}):
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
                self.device = kwargs["device"]
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

        worker = PushToTalkWorker("0")
        transcripts, errors = [], []
        worker.transcribed.connect(transcripts.append)
        worker.failed.connect(errors.append)
        modules = self._audio_modules(Stream, Model)
        modules["sounddevice"].query_devices = lambda: [
            {"name": "Microfone", "max_input_channels": 1}
        ]
        with patch.dict(sys.modules, modules):
            worker.start()
            self.assertTrue(started.wait(2))
            worker.stop_recording()
            worker.wait(5000)
            self.application.processEvents()

        self.assertEqual(transcripts, ["Olá, Jager"])
        self.assertEqual(errors, [])
        self.assertTrue(streams[0].closed)
        self.assertEqual(streams[0].device, 0)
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
