from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select, update

from .config import get_settings
from .database import SessionLocal
from .models import BrowserLease, User, as_utc, utcnow


def acquire_browser_lease(
    user_id: str,
    owner: str,
    mode: str,
    *,
    allow_inactive: bool = False,
) -> str | None:
    settings = get_settings()
    now = utcnow()
    with SessionLocal() as db, db.begin():
        user = db.scalar(select(User).where(User.id == user_id).with_for_update())
        if not user or (not user.active and not allow_inactive):
            return None
        lease = db.get(BrowserLease, user_id)
        if lease and as_utc(lease.expires_at) > now:
            return None
        token = uuid.uuid4().hex
        if not lease:
            lease = BrowserLease(
                user_id=user_id,
                owner=owner,
                token=token,
                mode=mode,
                acquired_at=now,
                heartbeat_at=now,
                expires_at=now + timedelta(seconds=settings.browser_lease_seconds),
            )
            db.add(lease)
        else:
            lease.owner = owner
            lease.token = token
            lease.mode = mode
            lease.acquired_at = now
            lease.heartbeat_at = now
            lease.expires_at = now + timedelta(seconds=settings.browser_lease_seconds)
        return token


def renew_browser_lease(user_id: str, owner: str, token: str) -> bool:
    settings = get_settings()
    now = utcnow()
    with SessionLocal() as db:
        if not db.scalar(select(User.active).where(User.id == user_id)):
            return False
        changed = db.execute(
            update(BrowserLease)
            .where(
                BrowserLease.user_id == user_id,
                BrowserLease.owner == owner,
                BrowserLease.token == token,
            )
            .values(
                heartbeat_at=now,
                expires_at=now + timedelta(seconds=settings.browser_lease_seconds),
            )
        ).rowcount
        db.commit()
        return bool(changed)


def release_browser_lease(user_id: str, owner: str, token: str) -> None:
    with SessionLocal() as db:
        lease = db.get(BrowserLease, user_id)
        if lease and lease.owner == owner and lease.token == token:
            db.delete(lease)
            db.commit()


def browser_lease_active(user_id: str) -> bool:
    with SessionLocal() as db:
        lease = db.get(BrowserLease, user_id)
        return bool(lease and as_utc(lease.expires_at) > utcnow())
