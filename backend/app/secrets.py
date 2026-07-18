from __future__ import annotations

import base64
import hashlib
import os

import keyring
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import get_settings

SERVICE_NAME = "xhs-medical-image-collector"


def set_minimax_key(value: str) -> None:
    keyring.set_password(SERVICE_NAME, "minimax-api-key", value)


def get_minimax_key() -> str:
    return keyring.get_password(SERVICE_NAME, "minimax-api-key") or ""


def delete_minimax_key() -> None:
    try:
        keyring.delete_password(SERVICE_NAME, "minimax-api-key")
    except keyring.errors.PasswordDeleteError:
        pass


def _encryption_key() -> bytes:
    settings = get_settings()
    source = settings.secrets_master_key
    if not source and settings.environment == "production":
        raise RuntimeError("生产环境缺少 SECRETS_MASTER_KEY")
    if not source:
        source = settings.site_auth_session_secret or "xhs-collector-development-only-secret"
    return hashlib.sha256(source.encode("utf-8")).digest()


def encrypt_secret(value: str) -> str:
    nonce = os.urandom(12)
    encrypted = AESGCM(_encryption_key()).encrypt(nonce, value.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")


def decrypt_secret(value: str) -> str:
    raw = base64.urlsafe_b64decode(value.encode("ascii"))
    if len(raw) < 29:
        raise ValueError("加密密钥数据无效")
    return AESGCM(_encryption_key()).decrypt(raw[:12], raw[12:], None).decode("utf-8")


def secret_hint(value: str) -> str:
    if len(value) <= 8:
        return "••••"
    return f"{value[:3]}••••{value[-4:]}"
