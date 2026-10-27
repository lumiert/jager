import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ai.ollama_client import (
    OllamaClient,
    OllamaConnectionError,
    OllamaHTTPError,
    OllamaTimeoutError,
    OllamaTimeouts,
)


def line(content="", done=False, **message):
    return json.dumps({"message": {"content": content, **message}, "done": done}).encode() + b"\n"


class _Server:
    """Local HTTP server whose behavior is a script(handler) callable."""

    def __init__(self, script):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                outer.requests.append(json.loads(self.rfile.read(length)))
                try:
                    script(self)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def ok_headers(handler):
    handler.send_response(200)
    handler.send_header("Content-Type", "application/x-ndjson")
    handler.end_headers()


class OllamaClientTests(unittest.TestCase):
    def serve(self, script):
        server = _Server(script)
        self.addCleanup(server.close)
        return server

    def client(self, server, **timeouts):
        values = dict(connect=2.0, response=5.0, idle=5.0, total=30.0)
        values.update(timeouts)
        return OllamaClient(server.url, timeouts=OllamaTimeouts(**values))

    def test_stream_decodes_unicode_and_sends_tool_schemas(self) -> None:
        def script(handler):
            ok_headers(handler)
            handler.wfile.write(line("Olá "))
            handler.wfile.write(line("ação\n"))
            handler.wfile.write(
                line("", True, tool_calls=[{"function": {"name": "system_info", "arguments": {}}}])
            )

        server = self.serve(script)
        tool = {"type": "function", "function": {"name": "system_info"}}
        messages = list(
            self.client(server).chat_stream_events(
                "modelo", [{"role": "user", "content": "oi"}], threading.Event(), tools=[tool]
            )
        )
        self.assertEqual(server.requests[0]["tools"], [tool])
        self.assertEqual("".join(m["content"] for m in messages), "Olá ação\n")
        self.assertEqual(messages[-1]["tool_calls"][0]["function"]["name"], "system_info")

    def test_connection_refused_is_connection_error_not_timeout(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        client = OllamaClient(f"http://127.0.0.1:{port}", timeouts=OllamaTimeouts(connect=1.0))
        with self.assertRaises(OllamaConnectionError):
            list(client.chat_stream_events("m", [], threading.Event()))

    def test_http_error_carries_status_and_detail(self) -> None:
        def script(handler):
            body = json.dumps({"error": "model not found"}).encode()
            handler.send_response(404)
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

        client = self.client(self.serve(script))
        with self.assertRaises(OllamaHTTPError) as caught:
            list(client.chat_stream_events("m", [], threading.Event()))
        self.assertEqual(caught.exception.status, 404)
        self.assertIn("model not found", str(caught.exception))

    def test_no_first_byte_is_a_response_timeout(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        server = self.serve(lambda handler: release.wait(10))
        started = time.monotonic()
        with self.assertRaises(OllamaTimeoutError) as caught:
            list(self.client(server, response=0.6).chat_stream_events("m", [], threading.Event()))
        self.assertEqual(caught.exception.phase, "response")
        self.assertLess(time.monotonic() - started, 4)

    def test_stalled_stream_is_an_idle_timeout(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)

        def script(handler):
            ok_headers(handler)
            handler.wfile.write(line("parcial"))
            handler.wfile.flush()
            release.wait(10)

        received = []
        with self.assertRaises(OllamaTimeoutError) as caught:
            for message in self.client(self.serve(script), idle=0.6).chat_stream_events(
                "m", [], threading.Event()
            ):
                received.append(message["content"])
        self.assertEqual(caught.exception.phase, "idle")
        self.assertEqual(received, ["parcial"])

    def test_healthy_slow_stream_is_not_interrupted_by_idle_limit(self) -> None:
        def script(handler):
            ok_headers(handler)
            for index in range(6):
                handler.wfile.write(line(str(index)))
                handler.wfile.flush()
                time.sleep(0.3)
            handler.wfile.write(line("", True))

        client = self.client(self.serve(script), idle=1.0)
        text = "".join(m["content"] for m in client.chat_stream_events("m", [], threading.Event()))
        self.assertEqual(text, "012345")

    def test_total_limit_stops_an_otherwise_active_stream(self) -> None:
        def script(handler):
            ok_headers(handler)
            for _ in range(100):
                handler.wfile.write(line("x"))
                handler.wfile.flush()
                time.sleep(0.1)

        with self.assertRaises(OllamaTimeoutError) as caught:
            list(self.client(self.serve(script), total=1.0).chat_stream_events("m", [], threading.Event()))
        self.assertEqual(caught.exception.phase, "total")

    def test_slow_consumer_is_not_reported_as_stream_silence(self) -> None:
        def script(handler):
            ok_headers(handler)
            handler.wfile.write(line("a"))
            handler.wfile.write(line("b", True))

        client = self.client(self.serve(script), idle=0.5)
        collected = []
        for message in client.chat_stream_events("m", [], threading.Event()):
            collected.append(message["content"])
            time.sleep(1.0)
        self.assertEqual(collected, ["a", "b"])

    def test_user_cancellation_ends_quietly_without_an_error(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)

        def script(handler):
            ok_headers(handler)
            handler.wfile.write(line("um"))
            handler.wfile.flush()
            release.wait(10)

        cancelled = threading.Event()
        threading.Timer(0.5, cancelled.set).start()
        started = time.monotonic()
        received = [m["content"] for m in self.client(self.serve(script)).chat_stream_events("m", [], cancelled)]
        self.assertEqual(received, ["um"])
        self.assertLess(time.monotonic() - started, 3)

    def test_cancellation_before_first_byte_does_not_raise(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        cancelled = threading.Event()
        threading.Timer(0.4, cancelled.set).start()
        server = self.serve(lambda handler: release.wait(10))
        self.assertEqual(list(self.client(server).chat_stream_events("m", [], cancelled)), [])

    def test_timeout_messages_are_short_and_user_facing(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        server = self.serve(lambda handler: release.wait(10))
        with self.assertRaises(OllamaTimeoutError) as caught:
            list(self.client(server, response=0.4).chat_stream_events("m", [], threading.Event()))
        message = str(caught.exception)
        self.assertLess(len(message), 120)
        self.assertNotIn("Traceback", message)

    def test_non_stream_chat_and_model_list(self) -> None:
        def script(handler):
            body = json.dumps({"message": {"content": " oi "}}).encode()
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

        client = self.client(self.serve(script))
        self.assertEqual(client.chat("m", [{"role": "user", "content": "x"}]), "oi")


if __name__ == "__main__":
    unittest.main()
