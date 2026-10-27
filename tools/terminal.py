from __future__ import annotations

import base64
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from tools.errors import ToolError

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
MAX_OUTPUT_CHARS = 20000
KILL_GRACE = 3.0

# Commands that would raise privileges are never executed, whatever the policy.
_ELEVATION = re.compile(
    r"(?i)(?:^|[;&|(\s])(?:sudo|doas|runas|gsudo|pkexec|su)\b|-verb\s+runas|start-process[^\n;|]*-verb\s+['\"]?runas"
)
# High-impact patterns always ask for confirmation, even under "allow always".
_HIGH_IMPACT = re.compile(
    r"(?i)\b(?:remove-item|rm|rmdir|rd|del|erase|format\s+[a-z]:|format-volume|clear-disk|"
    r"diskpart|stop-computer|restart-computer|shutdown|reboot|stop-process|taskkill|kill|"
    r"remove-itemproperty|reg\s+delete|set-executionpolicy|net\s+user|sc\s+delete|"
    r"mkfs|dd|chmod|chown|icacls|takeown|move-item|mv|git\s+(?:reset\s+--hard|clean|push)|"
    r"pip\s+uninstall|npm\s+uninstall|winget\s+(?:install|uninstall)|choco\s+(?:install|uninstall)|"
    r"pip\s+install|npm\s+install|apt(?:-get)?\s+(?:install|remove)|invoke-expression|iex|"
    r"invoke-webrequest|iwr|curl|wget|invoke-restmethod|irm|set-content|out-file|add-content)\b|>>?\s*[^\s&]"
)
_SECRET = re.compile(
    r"(?i)((?:password|passwd|pwd|token|secret|api[_-]?key|authorization)\s*[=:]\s*)\S+"
)


def redact(text: str) -> str:
    return _SECRET.sub(r"\1***", text)


def resolve_shell() -> list[str] | None:
    """PowerShell is the default on Windows; elsewhere pwsh, then /bin/sh."""
    if os.name == "nt":
        for name in ("powershell.exe", "pwsh.exe"):
            found = shutil.which(name)
            if found:
                return [found, "-NoLogo", "-NoProfile", "-NonInteractive"]
        return None
    found = shutil.which("pwsh")
    if found:
        return [found, "-NoLogo", "-NoProfile", "-NonInteractive"]
    sh = shutil.which("sh")
    return [sh, "-c"] if sh else None


def shell_name() -> str:
    shell = resolve_shell()
    return Path(shell[0]).stem.lower() if shell else "indisponível"


def risk_reason(command: str) -> str | None:
    """Return why a command needs explicit confirmation regardless of policy."""
    if _HIGH_IMPACT.search(command):
        return "O comando pode alterar arquivos, processos, rede ou o sistema."
    return None


def is_elevation(command: str) -> bool:
    return bool(_ELEVATION.search(command))


def _build_argv(shell: list[str], command: str) -> list[str]:
    if Path(shell[0]).stem.lower() in {"powershell", "pwsh"}:
        script = (
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
            "$OutputEncoding=[Text.Encoding]::UTF8;"
            "$ProgressPreference='SilentlyContinue';" + command
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return [*shell, "-EncodedCommand", encoded]
    return [*shell, command]


def _kill_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.kill()
    except OSError:
        pass


class _Capture(threading.Thread):
    """Drains a pipe, keeping at most `limit` characters but counting everything."""

    def __init__(self, stream, limit: int) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.limit = limit
        self.parts: list[str] = []
        self.kept = 0
        self.total = 0

    def run(self) -> None:
        try:
            for chunk in iter(lambda: self.stream.read(4096), b""):
                text = chunk.decode("utf-8", errors="replace")
                self.total += len(text)
                room = self.limit - self.kept
                if room > 0:
                    self.parts.append(text[:room])
                    self.kept += min(room, len(text))
        except (OSError, ValueError):
            pass

    @property
    def text(self) -> str:
        return "".join(self.parts)

    @property
    def truncated(self) -> bool:
        return self.total > self.kept


class TerminalTool:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: set[subprocess.Popen] = set()

    @staticmethod
    def availability() -> tuple[bool, str]:
        if resolve_shell() is None:
            return False, "Nenhum shell (PowerShell) foi encontrado neste computador."
        return True, ""

    def run(
        self, args: dict[str, Any], cancelled: threading.Event | None = None
    ) -> dict[str, Any]:
        command = args["command"]
        if not command.strip():
            raise ToolError("Informe um comando.", status="invalid")
        if is_elevation(command):
            raise ToolError(
                "Elevação automática de privilégios não é permitida. Execute o Jager "
                "como administrador, por decisão sua, se isso for realmente necessário.",
                status="denied",
            )
        shell = resolve_shell()
        if shell is None:
            raise ToolError("Nenhum shell disponível.", status="unavailable")
        timeout = min(max(int(args.get("timeout_seconds", DEFAULT_TIMEOUT)), 1), MAX_TIMEOUT)
        cwd_value = args.get("working_directory")
        cwd = Path(cwd_value).expanduser() if cwd_value else Path.home()
        if not cwd.is_dir():
            raise ToolError("O diretório de trabalho não existe.", details={"working_directory": str(cwd)})
        cwd = cwd.resolve()

        popen_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            popen_kwargs["start_new_session"] = True
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                _build_argv(shell, command),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **popen_kwargs,
            )
        except OSError as error:
            raise ToolError(f"Não foi possível iniciar o shell: {error.strerror or error}") from error
        with self._lock:
            self._active.add(process)
        out = _Capture(process.stdout, MAX_OUTPUT_CHARS)
        err = _Capture(process.stderr, MAX_OUTPUT_CHARS // 2)
        out.start()
        err.start()
        timed_out = was_cancelled = False
        try:
            while process.poll() is None:
                if cancelled is not None and cancelled.is_set():
                    was_cancelled = True
                    break
                if time.monotonic() - started > timeout:
                    timed_out = True
                    break
                time.sleep(0.05)
            if timed_out or was_cancelled:
                _kill_tree(process)
            try:
                process.wait(KILL_GRACE)
            except subprocess.TimeoutExpired:
                _kill_tree(process)
            out.join(KILL_GRACE)
            err.join(KILL_GRACE)
        finally:
            _kill_tree(process)
            for pipe in (process.stdout, process.stderr):
                try:
                    pipe.close()
                except OSError:
                    pass
            with self._lock:
                self._active.discard(process)
        details = {
            "shell": shell_name(),
            "command": redact(command)[:2000],
            "working_directory": str(cwd),
            "exit_code": process.returncode,
            "stdout": redact(out.text),
            "stderr": redact(err.text),
            "duration_seconds": round(time.monotonic() - started, 2),
            "timed_out": timed_out,
            "cancelled": was_cancelled,
            "truncated": out.truncated or err.truncated,
        }
        if details["truncated"]:
            details["truncation_note"] = (
                f"A saída foi limitada a {MAX_OUTPUT_CHARS} caracteres; o restante foi descartado."
            )
        if timed_out:
            raise ToolError(
                f"O comando excedeu {timeout} s e foi encerrado com seus processos filhos.",
                status="timeout",
                details=details,
            )
        if was_cancelled:
            raise ToolError("O comando foi cancelado e encerrado.", status="cancelled", details=details)
        if process.returncode != 0:
            raise ToolError(
                f"O comando terminou com código de saída {process.returncode}.",
                details=details,
            )
        return details

    def terminate_all(self) -> None:
        with self._lock:
            processes = list(self._active)
        for process in processes:
            _kill_tree(process)
