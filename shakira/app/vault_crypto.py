"""Criptografia do cofre de senhas por utilizador."""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

VAULT_MASTER_KEY_ENV = "SHAKIRA_VAULT_MASTER_KEY"


def vault_master_key(settings_key: str = "") -> str:
    return (settings_key or os.environ.get(VAULT_MASTER_KEY_ENV, "")).strip()


def vault_configured(settings_key: str = "") -> bool:
    return bool(vault_master_key(settings_key))


def _user_fernet(master_key: str, phone: str) -> Fernet:
    digest = hashlib.sha256(f"{master_key}:{phone}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(master_key: str, phone: str, plaintext: str) -> str:
    return _user_fernet(master_key, phone).encrypt(plaintext.encode()).decode()


def decrypt_secret(master_key: str, phone: str, ciphertext: str) -> str:
    try:
        return _user_fernet(master_key, phone).decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise ValueError("nao foi possivel desencriptar o cofre") from e
