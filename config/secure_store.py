from __future__ import annotations

import os


SERVICE = "Jager"
TELEGRAM_TOKEN_KEY = "telegram_bot_token"


class SecureStoreError(RuntimeError):
    pass


def _keyring_module():
    try:
        import keyring
    except ImportError as error:
        raise SecureStoreError(
            "O armazenamento seguro de credenciais não está instalado."
        ) from error
    backend = keyring.get_keyring()
    backend_module = backend.__class__.__module__.casefold()
    if (
        getattr(backend, "priority", 0) <= 0
        or "plaintext" in backend_module
        or "keyrings.alt.file" in backend_module
        or (os.name == "nt" and "windows" not in backend_module)
    ):
        raise SecureStoreError(
            "Não há um armazenamento seguro de credenciais disponível neste sistema."
        )
    return keyring


def save_telegram_token(token: str) -> None:
    try:
        _keyring_module().set_password(SERVICE, TELEGRAM_TOKEN_KEY, token)
    except SecureStoreError:
        raise
    except Exception as error:
        raise SecureStoreError(
            "Não foi possível salvar o token no armazenamento seguro do sistema."
        ) from error


def load_telegram_token() -> str | None:
    try:
        return _keyring_module().get_password(SERVICE, TELEGRAM_TOKEN_KEY)
    except SecureStoreError:
        raise
    except Exception as error:
        raise SecureStoreError(
            "Não foi possível recuperar a credencial do armazenamento seguro."
        ) from error


def delete_telegram_token() -> None:
    try:
        keyring = _keyring_module()
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(SERVICE, TELEGRAM_TOKEN_KEY)
        except PasswordDeleteError:
            return
    except SecureStoreError:
        raise
    except Exception as error:
        raise SecureStoreError(
            "Não foi possível remover a credencial do armazenamento seguro."
        ) from error
