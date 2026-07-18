from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, or_, select, update

from .config import get_settings
from .database import SessionLocal, engine
from .enums import TaskStatus
from .models import BrowserLease, CollectionTask, User, as_utc, utcnow


@dataclass(frozen=True)
class TaskLease:
    task_id: str
    user_id: str
    organization_id: str
    token: str
    owner: str
    browser_token: str | None = None


def worker_identity(kind: str, slot: int) -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{kind}:{slot}:{uuid.uuid4().hex[:8]}"


def recover_expired_leases() -> int:
    now = utcnow()
    recovered = 0
    with SessionLocal() as db:
        tasks = db.scalars(
            select(CollectionTask).where(
                or_(
                    and_(
                        CollectionTask.status == TaskStatus.RUNNING,
                        or_(
                            CollectionTask.lease_token.is_(None),
                            CollectionTask.lease_expires_at.is_(None),
                            CollectionTask.lease_expires_at <= now,
                        ),
                    ),
                    and_(
                        CollectionTask.lease_token.is_not(None),
                        or_(
                            CollectionTask.lease_expires_at.is_(None),
                            CollectionTask.lease_expires_at <= now,
                        ),
                    ),
                )
            )
        ).all()
        for task in tasks:
            if task.status == TaskStatus.RUNNING:
                task.status = TaskStatus.QUEUED
                task.progress_message = "上一个执行器租约已过期，任务重新排队"
            task.lease_owner = None
            task.lease_token = None
            task.lease_expires_at = None
            task.heartbeat_at = None
            task.state_version += 1
            recovered += 1
        db.commit()
    return recovered


def _claim(kind: str, owner: str) -> TaskLease | None:
    settings = get_settings()
    now = utcnow()
    excluded: set[str] = set()
    is_postgres = engine.dialect.name == "postgresql"
    for _ in range(50):
        with SessionLocal() as db, db.begin():
            browser_token = None
            if kind == "collection":
                query = select(CollectionTask).where(CollectionTask.status == TaskStatus.QUEUED)
            else:
                query = select(CollectionTask).where(
                    CollectionTask.status == TaskStatus.CLASSIFYING,
                    (
                        CollectionTask.lease_expires_at.is_(None)
                        | (CollectionTask.lease_expires_at <= now)
                    ),
                )
            if excluded:
                query = query.where(CollectionTask.id.not_in(excluded))
            task = db.scalar(
                query.order_by(CollectionTask.queue_position, CollectionTask.created_at)
                .limit(1)
                .with_for_update(skip_locked=is_postgres)
            )
            if not task:
                return None
            if kind == "collection":
                # Lock the user row so two workers cannot claim two queued tasks
                # for the same XHS browser account in concurrent transactions.
                db.scalar(
                    select(User)
                    .where(User.id == task.created_by_id)
                    .with_for_update()
                )
                conflict = db.scalar(
                    select(CollectionTask.id).where(
                        CollectionTask.created_by_id == task.created_by_id,
                        CollectionTask.id != task.id,
                        CollectionTask.status == TaskStatus.RUNNING,
                        CollectionTask.lease_expires_at > now,
                    )
                )
                if conflict:
                    excluded.add(task.id)
                    continue
                browser_lease = db.get(BrowserLease, task.created_by_id)
                if browser_lease and as_utc(browser_lease.expires_at) > now:
                    excluded.add(task.id)
                    continue
                browser_token = uuid.uuid4().hex
                if not browser_lease:
                    browser_lease = BrowserLease(
                        user_id=task.created_by_id,
                        owner=owner,
                        token=browser_token,
                        mode="collection",
                        acquired_at=now,
                        heartbeat_at=now,
                        expires_at=now
                        + timedelta(seconds=settings.browser_lease_seconds),
                    )
                    db.add(browser_lease)
                else:
                    browser_lease.owner = owner
                    browser_lease.token = browser_token
                    browser_lease.mode = "collection"
                    browser_lease.acquired_at = now
                    browser_lease.heartbeat_at = now
                    browser_lease.expires_at = now + timedelta(
                        seconds=settings.browser_lease_seconds
                    )
                task.status = TaskStatus.RUNNING
                task.started_at = task.started_at or now
                task.progress_message = "执行器已领取任务，正在检查登录状态"
            token = uuid.uuid4().hex
            task.lease_owner = owner
            task.lease_token = token
            task.lease_expires_at = now + timedelta(seconds=settings.worker_lease_seconds)
            task.heartbeat_at = now
            task.attempt_count += 1
            task.state_version += 1
            return TaskLease(
                task_id=task.id,
                user_id=task.created_by_id,
                organization_id=task.organization_id,
                token=token,
                owner=owner,
                browser_token=browser_token,
            )
    return None


def claim_collection(owner: str) -> TaskLease | None:
    return _claim("collection", owner)


def claim_classification(owner: str) -> TaskLease | None:
    return _claim("classification", owner)


def renew_lease(lease: TaskLease) -> bool:
    settings = get_settings()
    now = utcnow()
    with SessionLocal() as db:
        changed = db.execute(
            update(CollectionTask)
            .where(
                CollectionTask.id == lease.task_id,
                CollectionTask.lease_token == lease.token,
                CollectionTask.lease_owner == lease.owner,
            )
            .values(
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=settings.worker_lease_seconds),
            )
        ).rowcount
        db.commit()
        return bool(changed)


def owns_lease(task: CollectionTask, token: str | None) -> bool:
    if not token:
        return task.lease_token is None
    return task.lease_token == token and (
        not task.lease_expires_at or as_utc(task.lease_expires_at) > utcnow()
    )


def clear_lease(task: CollectionTask) -> None:
    task.lease_owner = None
    task.lease_token = None
    task.lease_expires_at = None
    task.heartbeat_at = None
