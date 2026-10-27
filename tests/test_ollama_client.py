import json
import threading
import unittest
from io import BytesIO
from unittest.mock import patch

from ai.ollama_client import OllamaClient


class OllamaClientTests(unittest.TestCase):
    def test_chat_stream_decodes_incremental_unicode_events(self) -> None:
        events = [
            {"message": {"content": "Olá "}, "done": False},
            {"message": {"content": "ação\n"}, "done": False},
            {"message": {"content": ""}, "done": True},
        ]
        response = BytesIO(
            b"".join(json.dumps(event, ensure_ascii=False).encode() + b"\n" for event in events)
        )
        with patch("ai.ollama_client.urlopen", return_value=response):
            fragments = list(
                OllamaClient("http://localhost:11434").chat_stream(
                    "modelo", [{"role": "user", "content": "oi"}], threading.Event()
                )
            )

        self.assertEqual(fragments, ["Olá ", "ação\n"])


if __name__ == "__main__":
    unittest.main()
