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

    def test_chat_stream_sends_registered_tool_schemas_and_emits_tool_calls(self) -> None:
        tool = {
            "type": "function",
            "function": {
                "name": "system_info",
                "description": "System details",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
        event = {
            "message": {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "system_info", "arguments": {}}}
                ],
            },
            "done": True,
        }
        response = BytesIO(json.dumps(event).encode() + b"\n")
        with patch("ai.ollama_client.urlopen", return_value=response) as request_call:
            messages = list(
                OllamaClient("http://localhost:11434").chat_stream_events(
                    "modelo",
                    [{"role": "user", "content": "sistema"}],
                    threading.Event(),
                    tools=[tool],
                )
            )

        payload = json.loads(request_call.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(payload["tools"], [tool])
        self.assertEqual(messages[0]["tool_calls"][0]["function"]["name"], "system_info")


if __name__ == "__main__":
    unittest.main()
