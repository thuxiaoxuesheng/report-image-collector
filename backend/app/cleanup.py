from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

from sqlalchemy import or_, select

from .config import get_settings
from .database import SessionLocal
from .models import (
    CollectedImage,
    CollectionTask,
    ExportRecord,
    LoginThrottle,
    Note,
    UserSession,
    utcnow,
)

logger = logging.getLogger(__name__)
settings = get_settings()


def unlink(path_value: str | None) -> bool:
    if not path_value:
        return True
    try:
        Path(path_value).unlink(missing_ok=True)
        return True
    except OSError:
        logger.warning("无法删除文件：%s", path_value)
        return False


def cleanup_expired_files() -> dict[str, int]:
    now = utcnow()
    hard_cutoff = now - timedelta(hours=settings.image_hard_limit_hours)
    deleted_images = 0
    deleted_exports = 0
    with SessionLocal() as db:
        images = db.scalars(
            select(CollectedImage)
            .join(Note)
            .join(CollectionTask)
            .where(
                CollectedImage.deleted_at.is_(None),
                or_(
                    CollectedImage.captured_at <= hard_cutoff,
                    CollectionTask.review_expires_at <= now,
                ),
            )
        ).all()
        touched_tasks: set[str] = set()
        for image in images:
            original_deleted = unlink(image.local_path)
            thumbnail_deleted = unlink(image.thumbnail_path)
            if not original_deleted or not thumbnail_deleted:
                continue
            image.deleted_at = now
            image.local_path = ""
            image.thumbnail_path = None
            image.ai_reason = None
            touched_tasks.add(image.note.task_id)
            deleted_images += 1

        for task_id in touched_tasks:
            remaining = db.scalar(
                select(CollectedImage.id)
                .join(Note)
                .where(Note.task_id == task_id, CollectedImage.deleted_at.is_(None))
                .limit(1)
            )
            if not remaining:
                notes = db.scalars(select(Note).where(Note.task_id == task_id)).all()
                for note in notes:
                    note.title = ""
                    note.author = ""
                    note.source_url = ""

        exports = db.scalars(
            select(ExportRecord).where(
                ExportRecord.deleted_at.is_(None),
                ExportRecord.expires_at <= now,
            )
        ).all()
        for record in exports:
            if not unlink(record.file_path):
                continue
            record.file_path = ""
            record.deleted_at = now
            deleted_exports += 1
        db.query(UserSession).filter(UserSession.expires_at <= now).delete(
            synchronize_session=False
        )
        throttle_cutoff = now - timedelta(days=1)
        db.query(LoginThrottle).filter(
            LoginThrottle.updated_at <= throttle_cutoff,
            or_(LoginThrottle.locked_until.is_(None), LoginThrottle.locked_until <= now),
        ).delete(synchronize_session=False)
        db.commit()
    return {"images": deleted_images, "exports": deleted_exports}


class CleanupWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="file-retention-cleanup")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(cleanup_expired_files)
                await asyncio.sleep(600)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("定时清理失败")
                await asyncio.sleep(60)


cleanup_worker = CleanupWorker()
