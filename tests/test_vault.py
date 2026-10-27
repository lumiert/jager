import tempfile
import unittest
from pathlib import Path

from storage.vault import Vault, VaultError


class VaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.vault = Vault(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_create_read_and_preserve_obsidian_markdown(self) -> None:
        content = "---\ntags: [jager]\n---\n\n[[Projeto]] #nota\n"
        self.vault.create_note("pasta/ideias.md", content)

        loaded, version = self.vault.read_note("pasta/ideias.md")
        self.assertEqual(loaded, content)
        self.assertEqual(self.vault.list_notes(), ["pasta/ideias.md"])
        self.vault.save_note("pasta/ideias.md", content + "\nTexto", version)
        self.assertTrue((Path(self.directory.name) / "pasta/ideias.md").read_text().endswith("Texto"))

    def test_external_modification_prevents_overwrite(self) -> None:
        path = Path(self.directory.name) / "nota.md"
        path.write_text("original", encoding="utf-8")
        _content, version = self.vault.read_note("nota.md")
        path.write_text("alteração externa", encoding="utf-8")

        with self.assertRaises(VaultError):
            self.vault.save_note("nota.md", "substituir", version)
        self.assertEqual(path.read_text(encoding="utf-8"), "alteração externa")

    def test_paths_cannot_escape_or_use_non_markdown_files(self) -> None:
        with self.assertRaises(VaultError):
            self.vault.create_note("../fora.md", "não")
        with self.assertRaises(VaultError):
            self.vault.create_note("nota.txt", "não")


if __name__ == "__main__":
    unittest.main()
