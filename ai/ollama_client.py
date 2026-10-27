from __future__ import annotations

import http.client
import json
import logging
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


class OllamaError(RuntimeError):
    """Base error. The message is short and safe to show to the user."""

    kind = "error"


class OllamaConnectionError(OllamaError):
    kind = "connection"


class OllamaTimeoutError(OllamaError):
    """`phase` is one of connect, response (no first byte), idle (stalled) or total."""

    kind = "timeout"

    def __init__(self, message: str, phase: str) -> None:
        super().__init__(message)
        self.phase = phase


class OllamaHTTPError(OllamaError):
    kind = "http"

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class OllamaTimeouts:
    """Seconds. Defaults suit local inference (CPU model loading, long prompt evaluation)."""

    connect: float = 10.0
    response: float = 300.0  # waiting for the first byte: model load + prompt evaluation
    idle: float = 120.0  # maximum silence between chunks of a running stream
    total: float = 1800.0  # hard ceiling for one request

    @classmethod
    def from_settings(cls, settings) -> "OllamaTimeouts":
        return cls(
            connect=settings.get_number("ollama_connect_timeout"),
            response=settings.get_number("ollama_response_timeout"),
            idle=settings.get_number("ollama_idle_timeout"),
            total=settings.get_number("ollama_total_timeout"),
        )


_TIMEOUT_MESSAGES = {
    "connect": "Não foi possível conectar ao Ollama a tempo. Verifique se ele está em execução.",
    "response": "O Ollama demorou demais para começar a responder. O modelo pode estar carregando; tente novamente.",
    "idle": "O Ollama parou de enviar dados. Tente novamente.",
    "total": "A resposta excedeu o tempo máximo configurado.",
}


class _Watchdog(threading.Thread):
    """Enforces response/idle/total deadlines and cancellation by closing the socket.

    A blocking read cannot be interrupted portably, so the watchdog shuts the socket
    down and records why; the reader then maps the resulting error to the right type.
    """

    def __init__(self, connection, cancelled, timeouts: OllamaTimeouts, response_timeout: float) -> None:
        super().__init__(daemon=True)
        self.connection = connection
        self.cancelled = cancelled
        self.timeouts = timeouts
        self.response_timeout = response_timeout
        self.reason: str | None = None
        self.sock = None
        self.phase = "response"
        self.paused = False
        self._stop_event = threading.Event()
        self._t0 = time.monotonic()
        self._activity = self._t0

    def touch(self, phase: str | None = None) -> None:
        self._activity = time.monotonic()
        if phase:
            self.phase = phase

    def stop(self) -> None:
        self._stop_event.set()

    def _expired(self) -> str | None:
        now = time.monotonic()
        if self.paused:
            # The consumer is busy with an event; that time is not stream silence.
            self._activity = now
        if self.cancelled is not None and self.cancelled.is_set():
            return "cancelled"
        if now - self._t0 > self.timeouts.total:
            return "total"
        limit = self.response_timeout if self.phase == "response" else self.timeouts.idle
        if now - self._activity > limit:
            return self.phase if self.phase == "response" else "idle"
        return None

    def run(self) -> None:
        while not self._stop_event.wait(0.2):
            reason = self._expired()
            if reason:
                self.reason = reason
                # http.client detaches the socket from the connection once a
                # non-keepalive response starts, so keep our own reference.
                sock = self.sock or getattr(self.connection, "sock", None)
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        sock.close()
                    except OSError:
                        pass
                return


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        timeout: float | None = None,
        timeouts: OllamaTimeouts | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if timeouts is None:
            timeouts = (
                OllamaTimeouts()
                if timeout is None
                else OllamaTimeouts(
                    connect=min(10.0, timeout), response=timeout, idle=timeout,
                    total=max(timeout * 10, timeout),
                )
            )
        self.timeouts = timeouts

    @property
    def timeout(self) -> float:
        return self.timeouts.response

    @contextmanager
    def _session(
        self,
        method: str,
        endpoint: str,
        payload: dict | None,
        cancelled: threading.Event | None,
        response_timeout: float | None = None,
    ):
        """Open a request; yields (response, watchdog). Errors are typed OllamaErrors."""
        parts = urlsplit(self.base_url)
        host = parts.hostname or "localhost"
        secure = parts.scheme == "https"
        port = parts.port or (443 if secure else 80)
        connection_class = http.client.HTTPSConnection if secure else http.client.HTTPConnection
        connection = connection_class(host, port, timeout=self.timeouts.connect)
        watchdog = _Watchdog(
            connection, cancelled, self.timeouts, response_timeout or self.timeouts.response
        )
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        try:
            try:
                connection.connect()
            except (TimeoutError, socket.timeout) as error:
                raise OllamaTimeoutError(_TIMEOUT_MESSAGES["connect"], "connect") from error
            except (OSError, ssl.SSLError) as error:
                raise OllamaConnectionError(
                    "Não foi possível conectar ao Ollama. Verifique o endereço e se ele está em execução."
                ) from error
            connection.sock.settimeout(None)
            watchdog.sock = connection.sock
            watchdog.touch()
            watchdog.start()
            try:
                connection.request(
                    method,
                    parts.path.rstrip("/") + endpoint,
                    body=body,
                    headers={"Content-Type": "application/json"} if body else {},
                )
                response = connection.getresponse()
            except (OSError, http.client.HTTPException, ValueError) as error:
                raise self._translate(error, watchdog) from error
            if response.status >= 400:
                text = response.read(4096).decode("utf-8", errors="replace")
                try:
                    detail = str(json.loads(text).get("error") or "")
                except (ValueError, AttributeError):
                    detail = text.strip()
                raise OllamaHTTPError(
                    f"O Ollama respondeu com erro HTTP {response.status}"
                    + (f": {detail[:200]}" if detail else "."),
                    response.status,
                )
            yield response, watchdog
        finally:
            watchdog.stop()
            connection.close()

    @staticmethod
    def _translate(error: BaseException, watchdog: _Watchdog) -> OllamaError | "_Cancelled":
        reason = watchdog.reason
        if reason == "cancelled":
            return _Cancelled()
        if reason:
            return OllamaTimeoutError(_TIMEOUT_MESSAGES[reason], reason)
        if isinstance(error, (TimeoutError, socket.timeout)):
            return OllamaTimeoutError(_TIMEOUT_MESSAGES["idle"], "idle")
        return OllamaConnectionError("A conexão com o Ollama foi interrompida.")

    def _request(
        self,
        endpoint: str,
        payload: dict | None = None,
        response_timeout: float | None = None,
    ) -> dict:
        try:
            with self._session(
                "POST" if payload is not None else "GET", endpoint, payload, None, response_timeout
            ) as (response, watchdog):
                try:
                    raw = response.read()
                except (OSError, http.client.HTTPException, ValueError) as error:
                    raise self._translate(error, watchdog) from error
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OllamaError("O Ollama retornou uma resposta inválida.") from error
        if not isinstance(result, dict):
            raise OllamaError("O Ollama retornou uma resposta inválida.")
        return result

    def list_models(self) -> list[str]:
        result = self._request("/api/tags", response_timeout=min(15.0, self.timeouts.response))
        models = result.get("models")
        if not isinstance(models, list):
            raise OllamaError("Não foi possível interpretar a lista de modelos.")
        return [
            model["name"]
            for model in models
            if isinstance(model, dict) and isinstance(model.get("name"), str)
        ]

    def chat(self, model: str, messages: list[dict[str, str]]) -> str:
        result = self._request(
            "/api/chat", {"model": model, "messages": messages, "stream": False}
        )
        message = result.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise OllamaError("O Ollama não retornou uma resposta de texto.")
        return content.strip()

    def chat_stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        cancelled: threading.Event,
    ):
        for message in self.chat_stream_events(model, messages, cancelled):
            fragment = message.get("content")
            if isinstance(fragment, str) and fragment:
                yield fragment

    def chat_stream_events(
        self,
        model: str,
        messages: list[dict],
        cancelled: threading.Event,
        tools: list[dict] | None = None,
        stage: str = "chat",
    ):
        """Yield streamed messages. User cancellation ends the stream quietly; every
        other interruption raises a typed OllamaError (timeout phase, connection, HTTP)."""
        payload = {"model": model, "messages": messages, "stream": True}
        if tools:
            payload["tools"] = tools
        started = time.monotonic()
        first_byte: float | None = None
        outcome = "ok"
        try:
            with self._session("POST", "/api/chat", payload, cancelled) as (response, watchdog):
                while True:
                    try:
                        line = response.readline()
                    except (OSError, http.client.HTTPException, ValueError) as error:
                        raise self._translate(error, watchdog) from error
                    if watchdog.reason == "cancelled" or cancelled.is_set():
                        outcome = "cancelled"
                        return
                    if watchdog.reason:
                        raise self._translate(OSError(), watchdog)
                    if not line:
                        break
                    if first_byte is None:
                        first_byte = time.monotonic() - started
                    watchdog.touch("stream")
                    try:
                        event = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise OllamaError(
                            "O Ollama retornou um fragmento de resposta inválido."
                        ) from error
                    if not isinstance(event, dict):
                        raise OllamaError("O Ollama retornou um evento inválido.")
                    if event.get("error"):
                        raise OllamaError(str(event["error"]))
                    message = event.get("message")
                    if isinstance(message, dict):
                        watchdog.paused = True
                        try:
                            yield message
                        finally:
                            watchdog.paused = False
                            watchdog.touch()
                    if event.get("done"):
                        break
        except _Cancelled:
            outcome = "cancelled"
            return
        except OllamaError as error:
            outcome = f"{error.kind}" + (
                f"/{error.phase}" if isinstance(error, OllamaTimeoutError) else ""
            )
            raise
        finally:
            logger.log(
                logging.INFO if outcome in {"ok", "cancelled"} else logging.WARNING,
                "Ollama modelo=%s etapa=%s duração=%.1fs primeiro_byte=%s resultado=%s",
                model, stage, time.monotonic() - started,
                f"{first_byte:.1f}s" if first_byte is not None else "n/a", outcome,
            )


class _Cancelled(Exception):
    """Internal marker: the user cancelled while a blocking call was in flight."""
