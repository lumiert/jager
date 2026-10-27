from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
from pathlib import Path


class VaultError(RuntimeError):
    pass


class Vault:
    def __init__(self, directory: str | Path) -> None:
        root = Path(directory).expanduser()
        if not root.is_dir():
            raise VaultError("O diretório do Vault não existe ou não é acessível.")
        self.root = root.resolve(strict=True)

    def _checked_path(self, relative_path: str | Path) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise VaultError("O caminho deve permanecer dentro do Vault.")
        path = self.root / relative
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise VaultError("Links simbólicos não são permitidos no Vault.")
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise VaultError("O caminho deve permanecer dentro do Vault.")
        if path.suffix.lower() != ".md":
            raise VaultError("Somente arquivos Markdown (.md) são suportados.")
        return path

    def list_notes(self, cancelled: threading.Event | None = None) -> list[str]:
        notes: list[str] = []
        for directory, dirs, filenames in os.walk(self.root, followlinks=False):
            if cancelled is not None and cancelled.is_set():
                break
            dirs[:] = [
                name
                for name in dirs
                if not name.startswith(".")
                and not (Path(directory) / name).is_symlink()
            ]
            for name in filenames:
                path = Path(directory) / name
                if name.lower().endswith(".md") and not path.is_symlink():
                    notes.append(path.relative_to(self.root).as_posix())
        return sorted(notes, key=str.casefold)

    def read_note(self, relative_path: str) -> tuple[str, str]:
        path = self._checked_path(relative_path)
        if not path.is_file():
            raise VaultError("A nota não foi encontrada.")
        content = path.read_text(encoding="utf-8")
        return content, self._hash(content)

    def save_note(
        self, relative_path: str, content: str, expected_hash: str | None = None
    ) -> None:
        path = self._checked_path(relative_path)
        exists = path.exists()
        if exists and not path.is_file():
            raise VaultError("O caminho selecionado não é um arquivo.")
        if exists and expected_hash is None:
            raise VaultError("A nota já existe; leia-a antes de salvar.")
        if exists:
            current_hash = self._hash(path.read_text(encoding="utf-8"))
            if current_hash != expected_hash:
                raise VaultError(
                    "A nota foi alterada fora do Jager. Reabra-a antes de salvar."
                )
        elif expected_hash is not None:
            raise VaultError("A nota foi removida fora do Jager.")

        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.parent.resolve(strict=True).is_relative_to(self.root):
            raise VaultError("O caminho deve permanecer dentro do Vault.")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=path.parent,
                prefix=".jager-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            if exists and self._hash(path.read_text(encoding="utf-8")) != expected_hash:
                raise VaultError(
                    "A nota foi alterada durante o salvamento; nenhuma alteração foi aplicada."
                )
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    def create_note(self, relative_path: str, content: str = "") -> None:
        """Create a new note; never replaces an existing one, even under a race."""
        path = self._checked_path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.parent.resolve(strict=True).is_relative_to(self.root):
            raise VaultError("O caminho deve permanecer dentro do Vault.")
        try:
            with open(path, "x", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as error:
            raise VaultError("A nota já existe; ela não foi sobrescrita.") from error
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def search(
        self,
        query: str,
        limit: int = 5,
        cancelled: threading.Event | None = None,
    ) -> list[tuple[str, str]]:
        terms = {term.casefold() for term in re.findall(r"\w+", query) if len(term) > 2}
        if not terms:
            return []
        matches: list[tuple[int, str, str]] = []
        for relative in self.list_notes(cancelled):
            if cancelled is not None and cancelled.is_set():
                break
            path = self._checked_path(relative)
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            score = sum(
                term in path.stem.casefold() or term in content.casefold()
                for term in terms
            )
            if score:
                matches.append((score, relative, content[:4000]))
        matches.sort(key=lambda result: (-result[0], result[1].casefold()))
        return [(name, content) for _, name, content in matches[:limit]]

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
