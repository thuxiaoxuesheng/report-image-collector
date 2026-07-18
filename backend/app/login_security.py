from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import LoginThrottle, as_utc, utcnow


@dataclass(frozen=True)
class LoginDecision:
    allowed: bool
    retry_after_seconds: int = 0


def request_ip(request: Request) -> str:
    """Trust the reverse-proxy address only when the direct peer is loopback."""
    peer = request.client.host if request.client else ""
    if peer in {"127.0.0.1", "::1", "testclient"}:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        if forwarded:
            return forwarded[:64]
    return peer[:64]


def _keys(username: str, ip_address: str) -> tuple[str, str]:
    normalized = username.strip().lower()
    return (
        hashlib.sha256(f"username:{normalized}".encode()).hexdigest(),
        hashlib.sha256(f"ip:{ip_address}".encode()).hexdigest(),
    )


def login_allowed(db: Session, username: str, ip_address: str) -> LoginDecision:
    now = utcnow()
    rows = db.scalars(
        select(LoginThrottle).where(LoginThrottle.key_hash.in_(_keys(username, ip_address)))
    ).all()
    remaining = 0
    for row in rows:
        if row.locked_until and as_utc(row.locked_until) > now:
            remaining = max(remaining, int((as_utc(row.locked_until) - now).total_seconds()) + 1)
    return LoginDecision(allowed=remaining == 0, retry_after_seconds=remaining)


def record_login_failure(db: Session, username: str, ip_address: str) -> None:
    settings = get_settings()
    now = utcnow()
    window = timedelta(seconds=settings.login_failure_window_seconds)
    lockout = timedelta(seconds=settings.login_lockout_seconds)
    for key_hash in _keys(username, ip_address):
        row = db.get(LoginThrottle, key_hash)
        if not row:
            row = LoginThrottle(key_hash=key_hash, failures=0, window_started_at=now)
            db.add(row)
        elif as_utc(row.window_started_at) + window <= now:
            row.failures = 0
            row.window_started_at = now
            row.locked_until = None
        row.failures += 1
        if row.failures >= settings.login_max_failures:
            row.locked_until = now + lockout
        row.updated_at = now


def clear_login_failures(db: Session, username: str, ip_address: str) -> None:
    for key_hash in _keys(username, ip_address):
        row = db.get(LoginThrottle, key_hash)
        if row:
            db.delete(row)
