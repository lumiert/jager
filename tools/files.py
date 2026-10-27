from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tools.errors import ToolError

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".log", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".xml", ".html", ".htm", ".css", ".js", ".ts", ".py", ".rst",
    ".tsv",
}
MAX_READ_BYTES = 128 * 1024
MAX_WRITE_CHARS = 200_000
MAX_ENTRIES = 200


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _metadata(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "type": "directory" if path.is_dir() else "file",
        "size_bytes": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(
            timespec="seconds"
        ),
    }


class FileTools:
    """Real file-system operations confined to the authorized roots.

    Nothing here overwrites, deletes or silently replaces existing data: creation
    refuses existing targets, editing needs an exact unique match, and moves refuse
    existing destinations. Every mutation is verified by reading it back.
    """

    def __init__(self, roots: Callable[[], list[Path]]) -> None:
        self._roots = roots

    def _within_roots(self, resolved: Path) -> bool:
        return any(resolved == root or resolved.is_relative_to(root) for root in self._roots())

    @staticmethod
    def _reject_links(path: Path) -> None:
        cursor = path
        while cursor != cursor.parent:
            if cursor.is_symlink():
                raise ToolError("Links simbólicos não são permitidos.", status="denied")
            cursor = cursor.parent

    def _checked(self, value: str, *, must_exist: bool = True) -> Path:
        if not value.strip() or "\x00" in value:
            raise ToolError("Informe um caminho válido.", status="invalid")
        requested = Path(value.strip()).expanduser()
        if ".." in requested.parts:
            raise ToolError("O caminho não pode conter '..'.", status="denied")
        self._reject_links(requested)
        try:
            resolved = requested.resolve(strict=must_exist)
        except FileNotFoundError as error:
            raise ToolError("O caminho não existe.", status="failed") from error
        if not self._within_roots(resolved):
            raise ToolError("O caminho não está em um local autorizado.", status="denied")
        return resolved

    def authorized_directory(self, value: str) -> Path:
        path = self._checked(value)
        if not path.is_dir():
            raise ToolError("O caminho informado não é uma pasta.")
        return path

    def _text_file(self, value: str) -> Path:
        path = self._checked(value)
        if not path.is_file() or path.suffix.lower() not in TEXT_EXTENSIONS:
            raise ToolError("Somente arquivos de texto permitidos podem ser lidos.")
        if path.stat().st_size > MAX_READ_BYTES:
            raise ToolError("O arquivo excede o limite de leitura de 128 KiB.")
        return path

    @staticmethod
    def _read(path: Path) -> str:
        try:
            with open(path, "r", encoding="utf-8", newline="") as handle:
                return handle.read()
        except UnicodeDecodeError as error:
            raise ToolError("O arquivo não está codificado em UTF-8.") from error

    def list_files(self, args: dict[str, Any]) -> dict[str, Any]:
        directory = self.authorized_directory(args["directory"])
        entries = []
        for item in sorted(directory.iterdir(), key=lambda entry: entry.name.casefold()):
            if item.is_symlink():
                continue
            try:
                info = _metadata(item)
            except OSError:
                continue
            entries.append(
                {
                    "name": item.name,
                    "type": info["type"],
                    "size_bytes": info["size_bytes"],
                    "modified": info["modified"],
                }
            )
        return {
            "directory": str(directory),
            "entries": entries[:MAX_ENTRIES],
            "truncated": len(entries) > MAX_ENTRIES,
        }

    def search_files(
        self, args: dict[str, Any], cancelled: threading.Event | None = None
    ) -> dict[str, Any]:
        root = self.authorized_directory(args["directory"])
        query = args["query"].casefold()
        matches: list[str] = []
        truncated = False
        for directory, dirs, filenames in os.walk(root, followlinks=False):
            if cancelled is not None and cancelled.is_set():
                raise ToolError("Pesquisa cancelada.", status="cancelled")
            dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
            for name in [*dirs, *filenames]:
                path = Path(directory) / name
                if query in name.casefold() and not path.is_symlink():
                    matches.append(path.relative_to(root).as_posix())
            if len(matches) >= MAX_ENTRIES:
                truncated = True
                break
        return {"directory": str(root), "matches": matches[:MAX_ENTRIES], "truncated": truncated}

    def read_text_file(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self._text_file(args["path"])
        content = self._read(path)
        return {
            "path": str(path),
            "content": content,
            "version": _sha256(content),
            "size_bytes": path.stat().st_size,
        }

    def file_info(self, args: dict[str, Any]) -> dict[str, Any]:
        return _metadata(self._checked(args["path"]))

    def create_folder(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self._checked(args["path"], must_exist=False)
        if path.exists():
            if path.is_dir():
                return {**_metadata(path), "already_existed": True}
            raise ToolError("Já existe um arquivo com esse nome.")
        path.mkdir(parents=True)
        if not path.is_dir():
            raise ToolError("A pasta não foi criada.")
        return {**_metadata(path), "already_existed": False}

    def create_file(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self._checked(args["path"], must_exist=False)
        content = args["content"]
        if len(content) > MAX_WRITE_CHARS:
            raise ToolError("O conteúdo excede o limite permitido.", status="invalid")
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            raise ToolError("Somente arquivos de texto com extensão conhecida podem ser criados.")
        if path.exists():
            # Re-running a completed creation (e.g. after a timeout) is not a conflict.
            if path.is_file() and self._read(path) == content:
                return {**_metadata(path), "version": _sha256(content), "already_existed": True}
            raise ToolError(
                "O arquivo já existe com outro conteúdo; ele não foi sobrescrito.",
                details={"path": str(path)},
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        if not self._within_roots(path.parent.resolve(strict=True)):
            raise ToolError("O caminho não está em um local autorizado.", status="denied")
        try:
            with open(path, "x", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as error:
            raise ToolError("O arquivo já existe; ele não foi sobrescrito.") from error
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        if self._read(path) != content:
            raise ToolError("A gravação não pôde ser verificada.")
        return {**_metadata(path), "version": _sha256(content), "already_existed": False}

    def edit_file(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self._text_file(args["path"])
        old_text, new_text = args["old_text"], args["new_text"]
        if not old_text:
            raise ToolError("old_text não pode ficar vazio.", status="invalid")
        content = self._read(path)
        expected = args.get("expected_hash")
        if expected and expected != _sha256(content):
            raise ToolError("O arquivo foi alterado desde a leitura; releia-o antes de editar.")
        if "\r\n" in content and "\r\n" not in old_text:
            old_text = old_text.replace("\n", "\r\n")
            new_text = new_text.replace("\n", "\r\n")
        count = content.count(old_text)
        if count == 0:
            if new_text and new_text in content:
                return {**_metadata(path), "version": _sha256(content), "already_applied": True}
            raise ToolError("O trecho informado não foi encontrado; nada foi alterado.")
        if count > 1:
            raise ToolError(
                "O trecho aparece mais de uma vez; informe um trecho único. Nada foi alterado."
            )
        updated = content.replace(old_text, new_text, 1)
        if len(updated) > MAX_WRITE_CHARS * 2:
            raise ToolError("O resultado excede o limite permitido.", status="invalid")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", newline="", dir=path.parent,
                prefix=".jager-", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            if self._read(path) != content:
                raise ToolError("O arquivo mudou durante a edição; nada foi alterado.")
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        if self._read(path) != updated:
            raise ToolError("A edição não pôde ser verificada.")
        return {**_metadata(path), "version": _sha256(updated), "already_applied": False}

    def move_path(self, args: dict[str, Any]) -> dict[str, Any]:
        source_value, destination_value = args["source"], args["destination"]
        destination = self._checked(destination_value, must_exist=False)
        try:
            source = self._checked(source_value)
        except ToolError as error:
            if error.status == "failed" and destination.exists():
                raise ToolError(
                    "A origem não existe, mas o destino já existe; a operação pode já ter "
                    "sido concluída. Nada foi alterado.",
                    details={"destination": str(destination)},
                ) from error
            raise
        if destination.exists():
            raise ToolError(
                "O destino já existe; nada foi sobrescrito.",
                details={"destination": str(destination)},
            )
        if source == destination:
            raise ToolError("Origem e destino são iguais.", status="invalid")
        if source.is_dir() and destination.is_relative_to(source):
            raise ToolError("Não é possível mover uma pasta para dentro dela mesma.")
        if any(source == root for root in self._roots()):
            raise ToolError("Os diretórios autorizados não podem ser movidos.", status="denied")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not self._within_roots(destination.parent.resolve(strict=True)):
            raise ToolError("O caminho não está em um local autorizado.", status="denied")
        shutil.move(str(source), str(destination))
        if source.exists() or not destination.exists():
            raise ToolError("A movimentação não pôde ser verificada.")
        return {"source": str(source), "destination": str(destination), **_metadata(destination)}
