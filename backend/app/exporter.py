from __future__ import annotations

import csv
import re
import shutil
import zipfile
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .config import get_settings
from .database import SessionLocal
from .models import CollectedImage, CollectionTask, ExportRecord, Note, TaskKeyword, utcnow

settings = get_settings()
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def safe_name(value: str, fallback: str = "未命名") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    cleaned = (cleaned or fallback)[:80]
    if cleaned.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def safe_csv_cell(value: object) -> object:
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def build_export(task_id: str, user_id: str) -> ExportRecord:
    export_id = __import__("uuid").uuid4().hex
    staging = settings.data_dir / "exports" / f"{export_id}-staging"
    archive = settings.data_dir / "exports" / f"{export_id}.zip"
    staging.mkdir(parents=True, exist_ok=True)
    recorded = False

    try:
        with SessionLocal() as db:
            task = db.scalar(
                select(CollectionTask)
                .where(
                    CollectionTask.id == task_id,
                    CollectionTask.created_by_id == user_id,
                )
                .options(selectinload(CollectionTask.keywords))
            )
            if not task:
                raise ValueError("任务不存在")
            rows = db.execute(
                select(CollectedImage, Note, TaskKeyword)
                .join(Note, CollectedImage.note_id == Note.id)
                .join(TaskKeyword, Note.keyword_id == TaskKeyword.id)
                .where(
                    Note.task_id == task_id,
                    CollectedImage.selected.is_(True),
                    CollectedImage.deleted_at.is_(None),
                )
                .order_by(TaskKeyword.position, Note.collected_at, CollectedImage.ordinal)
            ).all()

            if not rows:
                raise ValueError("没有可导出的已选图片")

            headers = [
                "关键词",
                "笔记ID",
                "笔记标题",
                "作者",
                "来源链接",
                "发布时间",
                "采集时间",
                "图片文件",
                "AI是否报告单",
                "AI置信度",
                "AI理由",
            ]
            exported_count = 0
            with (staging / "manifest.csv").open(
                "w", encoding="utf-8-sig", newline=""
            ) as file:
                writer = csv.writer(file)
                writer.writerow(headers)
                for image, note, keyword in rows:
                    source = Path(image.local_path)
                    if not source.exists():
                        continue
                    suffix = source.suffix.lower() if source.suffix else ".jpg"
                    # Prefix with the stable keyword position so two distinct
                    # keywords that sanitize to the same Windows name never
                    # get merged into one export folder.
                    keyword_folder = (
                        f"{keyword.position + 1:02d}_{safe_name(keyword.keyword)}"
                    )
                    relative = (
                        Path(keyword_folder)
                        / f"{safe_name(note.platform_note_id)}_{image.ordinal:03d}{suffix}"
                    )
                    target = staging / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    values = [
                        keyword.keyword,
                        note.platform_note_id,
                        note.title,
                        note.author,
                        note.source_url,
                        note.published_at.isoformat() if note.published_at else "",
                        note.collected_at.isoformat(),
                        relative.as_posix(),
                        (
                            "是"
                            if image.ai_is_report is True
                            else "否"
                            if image.ai_is_report is False
                            else "未判断"
                        ),
                        image.ai_confidence if image.ai_confidence is not None else "",
                        image.ai_reason or "",
                    ]
                    writer.writerow([safe_csv_cell(value) for value in values])
                    exported_count += 1

            if not exported_count:
                raise ValueError("已选图片文件已过期或不存在")
            (staging / "使用与隐私说明.txt").write_text(
                "本导出由用户人工确认生成。系统未提供OCR、自动打码或医学正确性判断。\n"
                "导出方应自行确认采集、保存和使用图片的权限，并负责后续个人信息保护。\n",
                encoding="utf-8",
            )

            with zipfile.ZipFile(
                archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as zf:
                for path in staging.rglob("*"):
                    if path.is_file():
                        zf.write(path, path.relative_to(staging))

            record = ExportRecord(
                id=export_id,
                task_id=task_id,
                user_id=user_id,
                file_path=str(archive),
                image_count=exported_count,
                expires_at=utcnow() + timedelta(hours=settings.export_retention_hours),
            )
            db.add(record)
            # 暂停中的任务可以先导出当前批次，再于次日继续补采。
            if task.status == "review":
                task.status = "exported"
            db.commit()
            db.refresh(record)
            recorded = True
            return record
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if not recorded:
            archive.unlink(missing_ok=True)
