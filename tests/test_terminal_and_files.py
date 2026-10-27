import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from config.settings import Settings
from storage.database import Database
from tools.errors import ToolError
from tools.files import FileTools
from tools.registry import ToolRegistry
from tools.terminal import TerminalTool, is_elevation, redact, risk_reason

POSIX = os.name != "nt"


class TerminalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.terminal = TerminalTool()
        self.directory = tempfile.TemporaryDirectory()
        self.cwd = Path(self.directory.name) / "pasta com espaço e acentuação ç"
        self.cwd.mkdir()

    def tearDown(self) -> None:
        self.terminal.terminate_all()
        self.directory.cleanup()

    def run_command(self, command, **extra):
        return self.terminal.run({"command": command, "working_directory": str(self.cwd), **extra})

    def test_captures_stdout_stderr_exit_code_duration_and_cwd(self) -> None:
        with self.assertRaises(ToolError) as caught:
            self.run_command("echo saida; echo erro 1>&2; exit 3")
        details = caught.exception.details
        self.assertEqual(details["exit_code"], 3)
        self.assertIn("saida", details["stdout"])
        self.assertIn("erro", details["stderr"])
        self.assertIn("duration_seconds", details)
        self.assertIn("código de saída 3", str(caught.exception))
        result = self.run_command("pwd" if POSIX else "(Get-Location).Path")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("acentuação", result["stdout"])

    def test_missing_working_directory_is_an_error(self) -> None:
        with self.assertRaises(ToolError):
            self.terminal.run({"command": "echo x", "working_directory": str(self.cwd / "nao")})

    @unittest.skipUnless(POSIX, "usa utilitários POSIX")
    def test_timeout_kills_child_processes(self) -> None:
        marker = self.cwd / "pid"
        started = time.monotonic()
        with self.assertRaises(ToolError) as caught:
            self.run_command(f"sleep 60 & echo $! > '{marker}'; wait", timeout_seconds=1)
        self.assertEqual(caught.exception.status, "timeout")
        self.assertTrue(caught.exception.details["timed_out"])
        self.assertLess(time.monotonic() - started, 10)
        child = int(marker.read_text().strip())
        time.sleep(0.3)
        try:
            os.kill(child, 0)
            alive = Path(f"/proc/{child}/stat").exists() and "Z" not in Path(f"/proc/{child}/stat").read_text().split(")")[-1][:3]
        except OSError:
            alive = False
        self.assertFalse(alive)

    @unittest.skipUnless(POSIX, "usa utilitários POSIX")
    def test_cancellation_stops_the_command(self) -> None:
        cancelled = threading.Event()
        threading.Timer(0.5, cancelled.set).start()
        started = time.monotonic()
        with self.assertRaises(ToolError) as caught:
            self.terminal.run({"command": "sleep 60", "working_directory": str(self.cwd)}, cancelled)
        self.assertEqual(caught.exception.status, "cancelled")
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(self.terminal._active, set())

    @unittest.skipUnless(POSIX, "usa utilitários POSIX")
    def test_large_output_is_truncated_and_flagged(self) -> None:
        result = self.run_command("yes abcdefghij | head -c 400000")
        self.assertTrue(result["truncated"])
        self.assertIn("limitada", result["truncation_note"])
        self.assertLess(len(result["stdout"]), 400000)

    def test_elevation_is_refused_without_running(self) -> None:
        marker = self.cwd / "ran"
        for command in (f"sudo touch '{marker}'", "Start-Process cmd -Verb RunAs", "runas /user:Administrator cmd"):
            self.assertTrue(is_elevation(command), command)
            with self.assertRaises(ToolError) as caught:
                self.run_command(command)
            self.assertEqual(caught.exception.status, "denied")
        self.assertFalse(marker.exists())

    def test_secrets_are_redacted_and_risky_commands_flagged(self) -> None:
        self.assertNotIn("abc123", redact("token=abc123 password: hunter2"))
        self.assertNotIn("hunter2", redact("password: hunter2"))
        self.assertIsNotNone(risk_reason("Remove-Item -Recurse C:\\x"))
        self.assertIsNotNone(risk_reason("git push origin main"))
        self.assertIsNone(risk_reason("Get-Process"))

    def test_registry_applies_permission_policy_and_forces_confirmation_for_risky_commands(self) -> None:
        database = Database(Path(self.directory.name) / "j.sqlite3")
        self.addCleanup(database.close)
        registry = ToolRegistry(database, Settings(database))
        args = {"command": "echo ok", "working_directory": str(self.cwd)}
        self.assertEqual(registry.execute("run_terminal_command", args)["status"], "denied")
        self.assertTrue(registry.execute("run_terminal_command", args, confirm=lambda r: True)["ok"])
        registry.permissions.set_policy("run_terminal_command", "allow")
        self.assertTrue(registry.execute("run_terminal_command", args)["ok"])
        risky = {"command": "rm -rf nada", "working_directory": str(self.cwd)}
        asked = []
        registry.execute("run_terminal_command", risky, confirm=lambda r: asked.append(r) or False)
        self.assertEqual(len(asked), 1)
        self.assertTrue(asked[0]["reason"])
        failed = registry.execute("run_terminal_command", {**args, "command": "exit 4"})
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["details"]["exit_code"], 4)


class FileToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve() / "área de trabalho"
        self.root.mkdir()
        self.outside = Path(self.directory.name).resolve() / "fora"
        self.outside.mkdir()
        self.files = FileTools(lambda: [self.root])

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_create_list_search_read_info_edit_move_real_files(self) -> None:
        folder = self.root / "Relatórios 2024"
        self.assertTrue(self.files.create_folder({"path": str(folder)})["type"] == "directory")
        note = folder / "resumo final.md"
        created = self.files.create_file({"path": str(note), "content": "linha um\nlinha dois\n"})
        self.assertTrue(note.is_file())
        self.assertFalse(created["already_existed"])
        listing = self.files.list_files({"directory": str(folder)})
        self.assertEqual([e["name"] for e in listing["entries"]], ["resumo final.md"])
        found = self.files.search_files({"directory": str(self.root), "query": "resumo"})
        self.assertEqual(len(found["matches"]), 1)
        self.assertEqual(self.files.read_text_file({"path": str(note)})["content"], "linha um\nlinha dois\n")
        info = self.files.file_info({"path": str(note)})
        self.assertEqual(info["size_bytes"], note.stat().st_size)
        self.assertIn("modified", info)
        edited = self.files.edit_file({"path": str(note), "old_text": "linha dois", "new_text": "linha 2"})
        self.assertEqual(note.read_text(encoding="utf-8"), "linha um\nlinha 2\n")
        self.assertTrue(edited)
        destination = self.root / "arquivado.md"
        self.files.move_path({"source": str(note), "destination": str(destination)})
        self.assertFalse(note.exists())
        self.assertEqual(destination.read_text(encoding="utf-8"), "linha um\nlinha 2\n")

    def test_paths_outside_authorized_roots_and_traversal_are_blocked(self) -> None:
        target = self.outside / "x.txt"
        for attempt in (
            lambda: self.files.create_file({"path": str(target), "content": "x"}),
            lambda: self.files.create_file({"path": str(self.root / ".." / "fora" / "y.txt"), "content": "x"}),
            lambda: self.files.list_files({"directory": str(self.outside)}),
        ):
            with self.assertRaises(ToolError):
                attempt()
        self.assertEqual(list(self.outside.iterdir()), [])

    @unittest.skipUnless(POSIX, "symlinks exigem privilégios no Windows")
    def test_symlink_escape_is_blocked(self) -> None:
        (self.outside / "segredo.txt").write_text("s", encoding="utf-8")
        (self.root / "atalho").symlink_to(self.outside)
        with self.assertRaises(ToolError):
            self.files.read_text_file({"path": str(self.root / "atalho" / "segredo.txt")})

    def test_nothing_is_overwritten_silently(self) -> None:
        path = self.root / "a.txt"
        path.write_text("original", encoding="utf-8")
        with self.assertRaises(ToolError):
            self.files.create_file({"path": str(path), "content": "novo"})
        self.assertEqual(path.read_text(encoding="utf-8"), "original")
        other = self.root / "b.txt"
        other.write_text("b", encoding="utf-8")
        with self.assertRaises(ToolError):
            self.files.move_path({"source": str(other), "destination": str(path)})
        self.assertEqual(path.read_text(encoding="utf-8"), "original")
        self.assertTrue(other.exists())

    def test_retry_of_completed_operations_is_idempotent(self) -> None:
        path = self.root / "r.txt"
        self.files.create_file({"path": str(path), "content": "mesmo"})
        again = self.files.create_file({"path": str(path), "content": "mesmo"})
        self.assertTrue(again["already_existed"])
        self.assertEqual(path.read_text(encoding="utf-8"), "mesmo")
        self.files.edit_file({"path": str(path), "old_text": "mesmo", "new_text": "outro"})
        repeated = self.files.edit_file({"path": str(path), "old_text": "mesmo", "new_text": "outro"})
        self.assertTrue(repeated["already_applied"])
        self.assertEqual(path.read_text(encoding="utf-8"), "outro")
        source, destination = self.root / "s.txt", self.root / "d.txt"
        source.write_text("s", encoding="utf-8")
        self.files.move_path({"source": str(source), "destination": str(destination)})
        with self.assertRaises(ToolError) as caught:
            self.files.move_path({"source": str(source), "destination": str(destination)})
        self.assertIn("já", str(caught.exception).lower())

    def test_ambiguous_edit_and_missing_file_fail_without_changes(self) -> None:
        path = self.root / "dup.txt"
        path.write_text("x x", encoding="utf-8")
        with self.assertRaises(ToolError):
            self.files.edit_file({"path": str(path), "old_text": "x", "new_text": "y"})
        self.assertEqual(path.read_text(encoding="utf-8"), "x x")
        with self.assertRaises(ToolError):
            self.files.read_text_file({"path": str(self.root / "nao-existe.txt")})

    def test_binary_files_are_not_read_as_text(self) -> None:
        path = self.root / "bin.txt"
        path.write_bytes(b"\xff\xfe\x00\x01")
        with self.assertRaises((ToolError, UnicodeError)):
            self.files.read_text_file({"path": str(path)})

    def test_no_delete_tool_exists(self) -> None:
        database = Database(Path(self.directory.name) / "j.sqlite3")
        self.addCleanup(database.close)
        names = {e["id"] for e in ToolRegistry(database, Settings(database)).entries()}
        self.assertFalse([n for n in names if "delete" in n or "remove" in n])
        self.assertFalse(hasattr(self.files, "delete_path"))

    def test_failed_write_leaves_no_partial_file(self) -> None:
        path = self.root / "falha.txt"
        with patch("tools.files.os.fsync", side_effect=OSError("disco cheio")):
            with self.assertRaises(OSError):
                self.files.create_file({"path": str(path), "content": "x"})
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
