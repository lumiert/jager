import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from config import secure_store
from storage.database import Database


class SecureStoreTests(unittest.TestCase):
    def test_token_is_written_to_credential_backend_not_sqlite(self) -> None:
        credentials = {}
        fake_keyring = types.SimpleNamespace(
            set_password=lambda service, key, value: credentials.__setitem__(
                (service, key), value
            ),
            get_password=lambda service, key: credentials.get((service, key)),
        )
        with patch("config.secure_store._keyring_module", return_value=fake_keyring):
            secure_store.save_telegram_token("123456789:example_token_secret")
            recovered = secure_store.load_telegram_token()

        self.assertEqual(recovered, "123456789:example_token_secret")
        self.assertEqual(set(credentials), {(secure_store.SERVICE, secure_store.TELEGRAM_TOKEN_KEY)})
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "settings.sqlite3")
            self.assertEqual(database.get_setting(secure_store.TELEGRAM_TOKEN_KEY), "")
            database.close()

    def test_plaintext_keyring_backend_is_rejected(self) -> None:
        class PlaintextBackend:
            priority = 1

        PlaintextBackend.__module__ = "keyrings.alt.file"
        module = types.ModuleType("keyring")
        module.get_keyring = lambda: PlaintextBackend()
        with patch.dict(sys.modules, {"keyring": module}):
            with self.assertRaises(secure_store.SecureStoreError):
                secure_store._keyring_module()


if __name__ == "__main__":
    unittest.main()
