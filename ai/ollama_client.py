from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, base_url: str, timeout: float = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, endpoint: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self.base_url}{endpoint}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST" if data is not None else "GET",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            message = error.read().decode("utf-8", errors="replace")
            raise OllamaError(f"Ollama respondeu com erro: {message or error.code}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise OllamaError("Não foi possível conectar ao Ollama.") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OllamaError("O Ollama retornou uma resposta inválida.") from error
        if not isinstance(result, dict):
            raise OllamaError("O Ollama retornou uma resposta inválida.")
        return result

    def list_models(self) -> list[str]:
        result = self._request("/api/tags")
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
            "/api/chat",
            {"model": model, "messages": messages, "stream": False},
        )
        message = result.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise OllamaError("O Ollama não retornou uma resposta de texto.")
        return content.strip()
