from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime, timedelta

PASSWORD_SCHEME = "scrypt"
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_site_password(password: str, *, salt: bytes | None = None) -> str:
    if len(password) < 12:
        raise ValueError("网站密码至少需要12位")
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P
    )
    return "$".join(
        (
            PASSWORD_SCHEME,
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            _b64encode(salt),
            _b64encode(derived),
        )
    )


def verify_site_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = encoded.split("$", 5)
        if scheme != PASSWORD_SCHEME:
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
        )
        return hmac.compare_digest(derived, _b64decode(expected))
    except (ValueError, TypeError):
        return False


def create_site_session(username: str, secret: str, *, issued_at: int | None = None) -> str:
    timestamp = issued_at if issued_at is not None else int(time.time())
    payload = _b64encode(f"{username}\n{timestamp}".encode())
    signature = _b64encode(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def verify_site_session(
    token: str,
    username: str,
    secret: str,
    *,
    max_age_seconds: int,
    now: int | None = None,
) -> bool:
    try:
        payload, signature = token.split(".", 1)
        expected = _b64encode(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return False
        stored_username, issued_at_text = _b64decode(payload).decode().split("\n", 1)
        issued_at = int(issued_at_text)
        current = now if now is not None else int(time.time())
        return stored_username == username and 0 <= current - issued_at <= max_age_seconds
    except (ValueError, UnicodeDecodeError):
        return False


def create_opaque_session() -> tuple[str, str]:
    """Return the cookie token and the SHA-256 value stored in the database."""
    token = secrets.token_urlsafe(32)
    return token, hash_session_token(token)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_expiry(days: int, *, now: datetime | None = None) -> datetime:
    current = now or datetime.now(UTC)
    return current + timedelta(days=days)


def generate_temporary_password() -> str:
    # URL-safe and easy to copy once from the administrator page.
    return secrets.token_urlsafe(15)
