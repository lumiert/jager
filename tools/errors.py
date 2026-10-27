from __future__ import annotations

from typing import Any


class ToolError(RuntimeError):
    """A tool failure that carries a status and structured details for the model."""

    def __init__(
        self,
        message: str,
        *,
        status: str = "failed",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.details = details or {}
