from __future__ import annotations

import mimetypes
import tempfile
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from PIL import Image
from sqlalchemy import case, func, select, text
from sqlalchemy.orm import Session, selectinload

from .ai import (
    classify_image,
    global_model_configuration,
    global_model_organization_id,
)
from .audit import write_audit
from .auth import CurrentUser, get_current_user, require_admin
from .browser import browser_manager
from .cleanup import cleanup_expired_files
from .config import get_settings
from .database import get_db
from .enums import ConsentType, TaskStatus
from .exporter import build_export
from .models import (
    CollectedImage,
    CollectionTask,
    ConsentRecord,
    ExportRecord,
    ModelConfiguration,
    Note,
    Organization,
    TaskKeyword,
    User,
    UserSession,
    as_utc,
    utcnow,
)
from .schemas import (
    AdminUserCreate,
    AdminUserUpdate,
    AiClassifyCreate,
    BrowserClick,
    BrowserKey,
    BrowserScroll,
    BrowserType,
    BulkImageUpdate,
    ExportCreate,
    ImageUpdate,
    ModelConfigurationUpdate,
    PasswordChange,
    TaskAction,
    TaskCreate,
    TaskView,
    UserView,
)
from .secrets import encrypt_secret, secret_hint
from .site_auth import (
    generate_temporary_password,
    hash_session_token,
    hash_site_password,
    verify_site_password,
)
from .task_queue import clear_lease

router = APIRouter(prefix="/api")
settings = get_settings()


def release_read_transaction(db: Session) -> None:
    """Return a pooled connection before awaiting browser or model network I/O."""
    if db.in_transaction():
        db.rollback()


def default_org(db: Session, current: CurrentUser) -> Organization:
    if current.organization:
        return current.organization
    raise HTTPException(status_code=403, detail="账号未关联独立工作空间")


def get_scoped_task(db: Session, task_id: str, current: CurrentUser) -> CollectionTask:
    task = db.scalar(
        select(CollectionTask)
        .where(CollectionTask.id == task_id)
        .options(selectinload(CollectionTask.keywords))
    )
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.created_by_id != current.user.id:
        raise HTTPException(status_code=403, detail="不能访问其他数据空间")
    return task


def move_queued_task(db: Session, task: CollectionTask, direction: int) -> None:
    """Normalize and swap queue positions so ordering stays deterministic."""
    queued = list(
        db.scalars(
            select(CollectionTask)
            .where(CollectionTask.status == TaskStatus.QUEUED)
            .order_by(
                CollectionTask.queue_position,
                CollectionTask.created_at,
                CollectionTask.id,
            )
        )
    )
    for position, queued_task in enumerate(queued, start=1):
        queued_task.queue_position = position
    current_index = next(
        (i for i, queued_task in enumerate(queued) if queued_task.id == task.id), -1
    )
    target_index = current_index + direction
    if current_index < 0 or target_index < 0 or target_index >= len(queued):
        raise HTTPException(status_code=409, detail="任务已经位于队列边界")
    neighbor = queued[target_index]
    task.queue_position, neighbor.queue_position = neighbor.queue_position, task.queue_position


def apply_task_action(db: Session, task: CollectionTask, action: str) -> None:
    if action == "pause" and task.status in {
        TaskStatus.QUEUED,
        TaskStatus.RUNNING,
        TaskStatus.CLASSIFYING,
        TaskStatus.NEEDS_ATTENTION,
    }:
        task.status = TaskStatus.PAUSED
        clear_lease(task)
    elif action == "resume" and task.status in {
        TaskStatus.PAUSED,
        TaskStatus.NEEDS_ATTENTION,
        TaskStatus.FAILED,
    }:
        resuming_ai = bool(task.classification_final_status)
        task.status = TaskStatus.CLASSIFYING if resuming_ai else TaskStatus.QUEUED
        task.attention_reason = None
        task.progress_message = (
            "已恢复，等待AI筛选执行器" if resuming_ai else "已恢复，等待采集执行器"
        )
        if not resuming_ai:
            task.queue_position = (
                db.scalar(
                    select(func.coalesce(func.max(CollectionTask.queue_position), 0)).where(
                        CollectionTask.status == TaskStatus.QUEUED
                    )
                )
                or 0
            ) + 1
        clear_lease(task)
    elif action == "cancel" and task.status not in {
        TaskStatus.EXPORTED,
        TaskStatus.CANCELLED,
    }:
        task.status = TaskStatus.CANCELLED
        task.finished_at = utcnow()
        task.review_expires_at = utcnow() + timedelta(hours=settings.image_retention_hours)
        clear_lease(task)
    elif action in {"move_up", "move_down"} and task.status == TaskStatus.QUEUED:
        move_queued_task(db, task, -1 if action == "move_up" else 1)
    else:
        raise HTTPException(status_code=409, detail="当前任务状态不允许此操作")
    task.state_version += 1


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    db.execute(text("SELECT 1"))
    return {"ok": True, "database": "ok", "time": utcnow()}


@router.get("/me", response_model=UserView)
def me(current: CurrentUser = Depends(get_current_user)) -> User:
    return current.user


@router.post("/auth/change-password")
def change_password(
    payload: PasswordChange,
    request: Request,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    if not current.user.password_hash or not verify_site_password(
        payload.current_password, current.user.password_hash
    ):
        raise HTTPException(status_code=400, detail="当前密码错误")
    current.user.password_hash = hash_site_password(payload.new_password)
    current.user.must_change_password = False
    current_token_hash = hash_session_token(
        request.cookies.get(settings.site_auth_cookie_name, "")
    )
    db.query(UserSession).filter(
        UserSession.user_id == current.user.id,
        UserSession.token_hash != current_token_hash,
    ).delete()
    db.commit()
    return {"ok": True}


@router.get("/admin/users")
def admin_users(
    db: Session = Depends(get_db), current: CurrentUser = Depends(require_admin)
) -> list[dict]:
    users = list(db.scalars(select(User).order_by(User.created_at, User.username)))
    shared_model_configured = bool(global_model_configuration(db))
    return [
        {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
            "active": user.active,
            "must_change_password": user.must_change_password,
            "created_at": user.created_at,
            "last_login_at": user.last_login_at,
            "model_configured": shared_model_configured,
        }
        for user in users
    ]


@router.post("/admin/users")
def create_user(
    payload: AdminUserCreate,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(require_admin),
) -> dict:
    if db.scalar(select(User.id).where(User.username == payload.username)):
        raise HTTPException(status_code=409, detail="用户名已存在")
    organization = Organization(
        name=f"{payload.display_name}-{payload.username}", status="active"
    )
    db.add(organization)
    db.flush()
    password = generate_temporary_password()
    user = User(
        email=f"{payload.username}@local.invalid",
        username=payload.username,
        display_name=payload.display_name.strip(),
        role="user",
        organization_id=organization.id,
        password_hash=hash_site_password(password),
        must_change_password=True,
        active=True,
    )
    db.add(user)
    db.flush()
    write_audit(
        db,
        action="user.create",
        user_id=current.user.id,
        organization_id=organization.id,
        target_type="user",
        target_id=user.id,
    )
    db.commit()
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "temporary_password": password,
    }


@router.patch("/admin/users/{user_id}")
def update_user(
    user_id: str,
    payload: AdminUserUpdate,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(require_admin),
) -> dict:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if payload.active is False and user.id == current.user.id:
        raise HTTPException(status_code=409, detail="不能停用当前管理员账号")
    if payload.display_name is not None:
        user.display_name = payload.display_name.strip()
    if payload.active is not None:
        user.active = payload.active
        if not user.active:
            db.query(UserSession).filter(UserSession.user_id == user.id).delete()
            active_statuses = [
                TaskStatus.QUEUED,
                TaskStatus.RUNNING,
                TaskStatus.CLASSIFYING,
                TaskStatus.NEEDS_ATTENTION,
            ]
            tasks = db.scalars(
                select(CollectionTask).where(
                    CollectionTask.created_by_id == user.id,
                    CollectionTask.status.in_(active_statuses),
                )
            ).all()
            for task in tasks:
                task.status = TaskStatus.PAUSED
                task.progress_message = "账号已停用，任务停止"
                task.state_version += 1
                clear_lease(task)
    write_audit(
        db,
        action="user.update",
        user_id=current.user.id,
        organization_id=user.organization_id,
        target_type="user",
        target_id=user.id,
        detail={"active": user.active, "display_name": user.display_name},
    )
    db.commit()
    return {"ok": True}


@router.post("/admin/users/{user_id}/reset-password")
def reset_user_password(
    user_id: str,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(require_admin),
) -> dict:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.id == current.user.id:
        raise HTTPException(status_code=409, detail="请通过个人改密流程修改当前管理员密码")
    password = generate_temporary_password()
    user.password_hash = hash_site_password(password)
    user.must_change_password = True
    db.query(UserSession).filter(UserSession.user_id == user.id).delete()
    write_audit(
        db,
        action="user.password.reset",
        user_id=current.user.id,
        organization_id=user.organization_id,
        target_type="user",
        target_id=user.id,
    )
    db.commit()
    return {"temporary_password": password}


@router.delete("/admin/users/{user_id}/browser-profile")
async def admin_clear_browser_profile(
    user_id: str,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(require_admin),
) -> dict:
    user = db.get(User, user_id)
    if not user or not user.organization_id:
        raise HTTPException(status_code=404, detail="用户或工作空间不存在")
    target_user_id = user.id
    target_workspace_id = user.organization_id
    admin_user_id = current.user.id
    release_read_transaction(db)
    try:
        await browser_manager.clear_profile(target_user_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    write_audit(
        db,
        action="browser.profile.admin_clear",
        user_id=admin_user_id,
        organization_id=target_workspace_id,
        target_type="user",
        target_id=target_user_id,
    )
    db.commit()
    return {"ok": True}


@router.get("/admin/queue")
def admin_queue(
    db: Session = Depends(get_db), current: CurrentUser = Depends(require_admin)
) -> list[dict]:
    rows = db.execute(
        select(CollectionTask, User)
        .join(User, CollectionTask.created_by_id == User.id)
        .where(
            CollectionTask.status.in_(
                [
                    TaskStatus.QUEUED,
                    TaskStatus.RUNNING,
                    TaskStatus.CLASSIFYING,
                    TaskStatus.NEEDS_ATTENTION,
                    TaskStatus.PAUSED,
                ]
            )
        )
        .order_by(CollectionTask.queue_position, CollectionTask.created_at)
    ).all()
    return [
        {
            "id": task.id,
            "name": task.name,
            "status": task.status,
            "queue_position": task.queue_position,
            "progress_message": task.progress_message,
            "attention_reason": task.attention_reason,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "owner_username": user.username,
            "owner_display_name": user.display_name,
        }
        for task, user in rows
    ]


@router.post("/admin/queue/{task_id}/action")
def admin_queue_action(
    task_id: str,
    payload: TaskAction,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(require_admin),
) -> dict:
    task = db.get(CollectionTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    apply_task_action(db, task, payload.action)
    write_audit(
        db,
        action=f"admin.queue.{payload.action}",
        user_id=current.user.id,
        organization_id=task.organization_id,
        target_type="task",
        target_id=task.id,
    )
    db.commit()
    return {"ok": True}


@router.get("/dashboard")
def dashboard(
    db: Session = Depends(get_db), current: CurrentUser = Depends(get_current_user)
) -> dict:
    counts = dict(
        db.execute(
            select(CollectionTask.status, func.count(CollectionTask.id))
            .where(CollectionTask.created_by_id == current.user.id)
            .group_by(CollectionTask.status)
        ).all()
    )
    selected_images = (
        db.scalar(
            select(func.count(CollectedImage.id))
            .join(Note)
            .join(CollectionTask)
            .where(
                CollectionTask.created_by_id == current.user.id,
                CollectedImage.selected.is_(True),
                CollectedImage.deleted_at.is_(None),
            )
        )
        or 0
    )
    soon = utcnow() + timedelta(hours=6)
    expiring_soon = (
        db.scalar(
            select(func.count(CollectionTask.id)).where(
                CollectionTask.created_by_id == current.user.id,
                CollectionTask.review_expires_at.is_not(None),
                CollectionTask.review_expires_at <= soon,
                CollectionTask.review_expires_at > utcnow(),
            )
        )
        or 0
    )
    recent = db.scalars(
        select(CollectionTask)
        .where(CollectionTask.created_by_id == current.user.id)
        .options(selectinload(CollectionTask.keywords))
        .order_by(CollectionTask.created_at.desc())
        .limit(8)
    ).all()
    return {
        "queued": counts.get(TaskStatus.QUEUED, 0),
        "running": counts.get(TaskStatus.RUNNING, 0) + counts.get(TaskStatus.CLASSIFYING, 0),
        "needs_attention": counts.get(TaskStatus.NEEDS_ATTENTION, 0),
        "awaiting_review": counts.get(TaskStatus.REVIEW, 0),
        "selected_images": selected_images,
        "expiring_soon": expiring_soon,
        "recent_tasks": [TaskView.model_validate(task) for task in recent],
    }


@router.post("/tasks", response_model=TaskView)
def create_task(
    payload: TaskCreate,
    request: Request,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
) -> CollectionTask:
    organization = default_org(db, current)
    active_count = (
        db.scalar(
            select(func.count(CollectionTask.id)).where(
                CollectionTask.created_by_id == current.user.id,
                CollectionTask.status.in_(
                    [
                        TaskStatus.QUEUED,
                        TaskStatus.RUNNING,
                        TaskStatus.CLASSIFYING,
                        TaskStatus.NEEDS_ATTENTION,
                        TaskStatus.PAUSED,
                    ]
                ),
            )
        )
        or 0
    )
    if active_count >= 3:
        raise HTTPException(status_code=409, detail="最多保留3个等待或运行中的任务")
    max_position = (
        db.scalar(
            select(func.coalesce(func.max(CollectionTask.queue_position), 0)).where(
                CollectionTask.status == TaskStatus.QUEUED
            )
        )
        or 0
    )
    task = CollectionTask(
        organization_id=organization.id,
        created_by_id=current.user.id,
        name=payload.name.strip(),
        status=TaskStatus.QUEUED,
        start_date=payload.start_date,
        end_date=payload.end_date,
        queue_position=max_position + 1,
        privacy_confirmed=True,
        ai_confirmed=payload.ai_confirmed,
    )
    for position, keyword in enumerate(payload.keywords):
        task.keywords.append(
            TaskKeyword(
                keyword=keyword.keyword, target_count=keyword.target_count, position=position
            )
        )
    db.add(task)
    db.flush()
    client_ip = request.client.host if request.client else None
    db.add(
        ConsentRecord(
            organization_id=organization.id,
            user_id=current.user.id,
            task_id=task.id,
            consent_type=ConsentType.TASK_PRIVACY,
            ip_address=client_ip,
        )
    )
    if payload.ai_confirmed:
        db.add(
            ConsentRecord(
                organization_id=organization.id,
                user_id=current.user.id,
                task_id=task.id,
                consent_type=ConsentType.AI_PROCESSING,
                ip_address=client_ip,
            )
        )
    write_audit(
        db,
        action="task.create",
        user_id=current.user.id,
        organization_id=organization.id,
        target_type="task",
        target_id=task.id,
        detail={"keywords": [item.keyword for item in payload.keywords]},
    )
    db.commit()
    db.refresh(task)
    return task


@router.get("/tasks", response_model=list[TaskView])
def list_tasks(
    db: Session = Depends(get_db), current: CurrentUser = Depends(get_current_user)
) -> list[CollectionTask]:
    return list(
        db.scalars(
            select(CollectionTask)
            .where(CollectionTask.created_by_id == current.user.id)
            .options(selectinload(CollectionTask.keywords))
            .order_by(
                case((CollectionTask.status == TaskStatus.RUNNING, 0), else_=1),
                CollectionTask.queue_position,
                CollectionTask.created_at.desc(),
            )
        ).all()
    )


@router.get("/tasks/{task_id}", response_model=TaskView)
def task_detail(
    task_id: str,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
) -> CollectionTask:
    return get_scoped_task(db, task_id, current)


@router.post("/tasks/{task_id}/action", response_model=TaskView)
def task_action(
    task_id: str,
    payload: TaskAction,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
) -> CollectionTask:
    task = get_scoped_task(db, task_id, current)
    action = payload.action
    if action in {"move_up", "move_down"} and current.user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以调整全局队列")
    apply_task_action(db, task, action)
    write_audit(
        db,
        action=f"task.{action}",
        user_id=current.user.id,
        organization_id=task.organization_id,
        target_type="task",
        target_id=task.id,
    )
    db.commit()
    db.refresh(task)
    return task


@router.post("/browser/open")
async def browser_open(
    db: Session = Depends(get_db), current: CurrentUser = Depends(get_current_user)
) -> dict:
    default_org(db, current)
    user_id = current.user.id
    release_read_transaction(db)
    try:
        await browser_manager.open_remote(user_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await browser_manager.status(user_id)


@router.post("/browser/close")
async def browser_close(
    db: Session = Depends(get_db), current: CurrentUser = Depends(get_current_user)
) -> dict:
    try:
        default_org(db, current)
        user_id = current.user.id
        release_read_transaction(db)
        await browser_manager.close_remote(user_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@router.get("/browser/status")
async def browser_status(
    db: Session = Depends(get_db), current: CurrentUser = Depends(get_current_user)
) -> dict:
    default_org(db, current)
    user_id = current.user.id
    release_read_transaction(db)
    return await browser_manager.status(user_id)


@router.get("/browser/screenshot")
async def browser_screenshot(
    db: Session = Depends(get_db), current: CurrentUser = Depends(get_current_user)
) -> Response:
    default_org(db, current)
    user_id = current.user.id
    release_read_transaction(db)
    try:
        data = await browser_manager.screenshot(user_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/browser/click")
async def browser_click(
    payload: BrowserClick,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        default_org(db, current)
        user_id = current.user.id
        release_read_transaction(db)
        await browser_manager.click(user_id, payload.x, payload.y)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/browser/type")
async def browser_type(
    payload: BrowserType,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        default_org(db, current)
        user_id = current.user.id
        release_read_transaction(db)
        await browser_manager.type_text(user_id, payload.text)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/browser/key")
async def browser_key(
    payload: BrowserKey,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        default_org(db, current)
        user_id = current.user.id
        release_read_transaction(db)
        await browser_manager.press_key(user_id, payload.key)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/browser/scroll")
async def browser_scroll(
    payload: BrowserScroll,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        default_org(db, current)
        user_id = current.user.id
        release_read_transaction(db)
        await browser_manager.scroll(user_id, payload.delta_y)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@router.delete("/browser/profile")
async def browser_clear_profile(
    db: Session = Depends(get_db), current: CurrentUser = Depends(get_current_user)
) -> dict:
    organization = default_org(db, current)
    user_id = current.user.id
    organization_id = organization.id
    release_read_transaction(db)
    try:
        await browser_manager.clear_profile(user_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    write_audit(
        db,
        action="browser.profile.clear",
        user_id=user_id,
        organization_id=organization_id,
    )
    db.commit()
    return {"ok": True}


@router.get("/images")
def list_images(
    task_id: str | None = None,
    keyword_id: str | None = None,
    verdict: str | None = None,
    selected: bool | None = None,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
) -> list[dict]:
    query = (
        select(CollectedImage, Note, TaskKeyword)
        .select_from(CollectedImage)
        .join(Note, CollectedImage.note_id == Note.id)
        .join(TaskKeyword, Note.keyword_id == TaskKeyword.id)
        .join(CollectionTask, Note.task_id == CollectionTask.id)
        .where(
            CollectionTask.created_by_id == current.user.id,
            CollectedImage.deleted_at.is_(None),
        )
        .order_by(Note.collected_at, CollectedImage.ordinal)
    )
    if task_id:
        query = query.where(Note.task_id == task_id)
    if keyword_id:
        query = query.where(TaskKeyword.id == keyword_id)
    if verdict == "report":
        query = query.where(CollectedImage.ai_is_report.is_(True))
    elif verdict == "non_report":
        query = query.where(CollectedImage.ai_is_report.is_(False))
    elif verdict == "unreviewed":
        query = query.where(CollectedImage.ai_is_report.is_(None))
    if selected is not None:
        query = query.where(CollectedImage.selected == selected)
    rows = db.execute(query).all()
    return [
        {
            "id": image.id,
            "note_id": note.id,
            "task_id": note.task_id,
            "keyword_id": keyword.id,
            "keyword": keyword.keyword,
            "platform_note_id": note.platform_note_id,
            "note_title": note.title,
            "author": note.author,
            "source_url": note.source_url,
            "published_at": note.published_at,
            "ordinal": image.ordinal,
            "selected": image.selected,
            "ai_is_report": image.ai_is_report,
            "ai_confidence": image.ai_confidence,
            "ai_reason": image.ai_reason,
            "captured_at": image.captured_at,
            "deleted_at": image.deleted_at,
            "image_url": f"/api/images/{image.id}/file",
            "thumbnail_url": f"/api/images/{image.id}/thumbnail",
        }
        for image, note, keyword in rows
    ]


def scoped_image(db: Session, image_id: str, current: CurrentUser) -> CollectedImage:
    image = db.scalar(
        select(CollectedImage).join(Note).join(CollectionTask).where(CollectedImage.id == image_id)
    )
    if not image:
        raise HTTPException(status_code=404, detail="图片不存在")
    if image.note.task.created_by_id != current.user.id:
        raise HTTPException(status_code=403, detail="不能访问其他数据空间")
    return image


@router.get("/images/{image_id}/file")
def image_file(
    image_id: str,
    thumbnail: bool = False,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
) -> FileResponse:
    image = scoped_image(db, image_id, current)
    path = Path(image.thumbnail_path if thumbnail and image.thumbnail_path else image.local_path)
    if image.deleted_at or not path.exists():
        raise HTTPException(status_code=410, detail="图片已删除")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, no-store"})


@router.get("/images/{image_id}/thumbnail")
def image_thumbnail(
    image_id: str,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
) -> FileResponse:
    return image_file(image_id, True, db, current)


@router.patch("/images/{image_id}")
def update_image(
    image_id: str,
    payload: ImageUpdate,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    image = scoped_image(db, image_id, current)
    if payload.selected is not None:
        image.selected = payload.selected
        image.manually_edited = True
    db.commit()
    return {"ok": True}


@router.post("/images/bulk")
def bulk_images(
    payload: BulkImageUpdate,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    images = db.scalars(
        select(CollectedImage)
        .join(Note)
        .join(CollectionTask)
        .where(
            CollectionTask.created_by_id == current.user.id,
            CollectedImage.id.in_(payload.image_ids),
            CollectedImage.deleted_at.is_(None),
        )
    ).all()
    for image in images:
        if payload.invert:
            image.selected = not image.selected
            image.manually_edited = True
        elif payload.selected is not None:
            image.selected = payload.selected
            image.manually_edited = True
    db.commit()
    return {"updated": len(images)}


@router.post("/tasks/{task_id}/classify")
async def classify_existing_task(
    task_id: str,
    payload: AiClassifyCreate,
    request: Request,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    task = get_scoped_task(db, task_id, current)
    if task.status not in {TaskStatus.REVIEW, TaskStatus.EXPORTED}:
        raise HTTPException(status_code=409, detail="只有已完成采集的任务可以运行AI判断")
    if not global_model_configuration(db):
        raise HTTPException(status_code=409, detail="管理员尚未配置全局视觉模型")
    keyword = None
    if payload.keyword_id:
        keyword = db.get(TaskKeyword, payload.keyword_id)
        if not keyword or keyword.task_id != task.id:
            raise HTTPException(status_code=404, detail="检索词不存在")
    image_count_query = (
        select(func.count(CollectedImage.id))
        .join(Note)
        .where(Note.task_id == task.id, CollectedImage.deleted_at.is_(None))
    )
    if keyword:
        image_count_query = image_count_query.where(Note.keyword_id == keyword.id)
    image_count = db.scalar(image_count_query)
    if not image_count:
        raise HTTPException(status_code=409, detail="当前任务没有可判断图片")
    final_status = task.status
    task.ai_confirmed = True
    task.status = TaskStatus.CLASSIFYING
    task.classification_keyword_id = payload.keyword_id
    task.classification_final_status = final_status
    task.state_version += 1
    clear_lease(task)
    task.attention_reason = None
    scope = f"检索词“{keyword.keyword}”" if keyword else "全部检索词"
    task.progress_message = f"准备AI筛选{scope}，共 {image_count} 张"
    db.add(
        ConsentRecord(
            organization_id=task.organization_id,
            user_id=current.user.id,
            task_id=task.id,
            consent_type=ConsentType.AI_PROCESSING,
            ip_address=request.client.host if request.client else None,
        )
    )
    write_audit(
        db,
        action="task.ai_classify",
        user_id=current.user.id,
        organization_id=task.organization_id,
        target_type="task",
        target_id=task.id,
    )
    db.commit()
    return {"scheduled": True, "image_count": image_count, "scope": scope}


@router.post("/tasks/{task_id}/undo-ai-screen")
def undo_ai_screen(
    task_id: str,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    task = get_scoped_task(db, task_id, current)
    if task.status not in {TaskStatus.REVIEW, TaskStatus.EXPORTED}:
        raise HTTPException(status_code=409, detail="任务运行中，暂时不能撤销AI筛选")
    images = list(
        db.scalars(
            select(CollectedImage)
            .join(Note)
            .where(
                Note.task_id == task.id,
                CollectedImage.deleted_at.is_(None),
                CollectedImage.ai_previous_selected.is_not(None),
            )
        )
    )
    if not images:
        raise HTTPException(status_code=409, detail="没有可撤销的AI筛选")
    for image in images:
        image.selected = bool(image.ai_previous_selected)
        image.ai_previous_selected = None
        image.manually_edited = True
    write_audit(
        db,
        action="task.ai_screen.undo",
        user_id=current.user.id,
        organization_id=task.organization_id,
        target_type="task",
        target_id=task.id,
        detail={"images": len(images)},
    )
    db.commit()
    return {"restored": len(images)}


@router.post("/tasks/{task_id}/export")
def create_export(
    task_id: str,
    payload: ExportCreate,
    request: Request,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    task = get_scoped_task(db, task_id, current)
    db.add(
        ConsentRecord(
            organization_id=task.organization_id,
            user_id=current.user.id,
            task_id=task.id,
            consent_type=ConsentType.EXPORT_PRIVACY,
            ip_address=request.client.host if request.client else None,
        )
    )
    write_audit(
        db,
        action="task.export.confirm",
        user_id=current.user.id,
        organization_id=task.organization_id,
        target_type="task",
        target_id=task.id,
    )
    db.commit()
    try:
        record = build_export(task_id, current.user.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "id": record.id,
        "image_count": record.image_count,
        "expires_at": record.expires_at,
        "download_url": f"/api/exports/{record.id}/download",
    }


@router.get("/exports/{export_id}/download")
def download_export(
    export_id: str,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
) -> FileResponse:
    record = db.get(ExportRecord, export_id)
    if not record or record.deleted_at:
        raise HTTPException(status_code=404, detail="导出文件不存在或已删除")
    task = get_scoped_task(db, record.task_id, current)
    path = Path(record.file_path)
    if as_utc(record.expires_at) <= utcnow() or not path.exists():
        raise HTTPException(status_code=410, detail="导出文件已过期")
    record.downloaded_at = utcnow()
    db.commit()
    return FileResponse(path, filename=f"{task.name}.zip", media_type="application/zip")


@router.get("/settings")
def read_settings(
    db: Session = Depends(get_db), current: CurrentUser = Depends(get_current_user)
) -> dict:
    default_org(db, current)
    config = global_model_configuration(db)
    can_manage = current.user.role == "admin"
    return {
        "model_configured": bool(config),
        "model_scope": "global",
        "model_can_manage": can_manage,
        "provider": config.provider if config else None,
        "base_url": config.base_url if config else None,
        "model_name": config.model_name if config else None,
        "key_hint": config.key_hint if config and can_manage else None,
        "last_test_ok": config.last_test_ok if config else None,
        "last_test_message": (
            config.last_test_message
            if config and can_manage
            else "管理员已配置全局视觉模型"
            if config
            else "管理员尚未配置全局视觉模型"
        ),
        "last_test_at": config.last_test_at if config else None,
        "browser_headless": settings.browser_headless,
        "image_retention_hours": settings.image_retention_hours,
    }


@router.put("/settings/model")
def save_model_configuration(
    payload: ModelConfigurationUpdate,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(require_admin),
) -> dict:
    organization_id = global_model_organization_id(db)
    config = global_model_configuration(db)
    if not config and not payload.api_key:
        raise HTTPException(status_code=422, detail="首次配置必须填写API Key")
    if config and config.provider != payload.provider and not payload.api_key:
        raise HTTPException(
            status_code=422,
            detail="切换模型接入方式时必须填写对应的新API Key",
        )
    if not config:
        config = ModelConfiguration(
            organization_id=organization_id,
            provider=payload.provider,
            encrypted_api_key="",
        )
        db.add(config)
        db.flush()
    config.provider = payload.provider
    config.base_url = (
        payload.base_url.strip().rstrip("/")
        if payload.provider == "openai_compatible" and payload.base_url
        else None
    )
    config.model_name = (
        payload.model_name.strip()
        if payload.provider == "openai_compatible" and payload.model_name
        else None
    )
    if payload.api_key:
        value = payload.api_key.strip()
        config.encrypted_api_key = encrypt_secret(value)
        config.key_hint = secret_hint(value)
    config.last_test_ok = None
    config.last_test_message = "配置已保存，尚未测试"
    config.last_test_at = None
    write_audit(
        db,
        action="model.configuration.save",
        user_id=current.user.id,
        organization_id=organization_id,
        target_type="model_configuration",
        target_id=config.id,
        detail={"provider": payload.provider, "model_name": config.model_name},
    )
    db.commit()
    return {"ok": True, "configured": True, "key_hint": config.key_hint}


@router.delete("/settings/model")
def clear_model_configuration(
    db: Session = Depends(get_db), current: CurrentUser = Depends(require_admin)
) -> dict:
    organization_id = global_model_organization_id(db)
    config = global_model_configuration(db)
    if config:
        db.delete(config)
        write_audit(
            db,
            action="model.configuration.delete",
            user_id=current.user.id,
            organization_id=organization_id,
        )
        db.commit()
    return {"ok": True, "configured": False}


@router.post("/settings/model/test")
async def test_model_configuration(
    db: Session = Depends(get_db), current: CurrentUser = Depends(require_admin)
) -> dict:
    organization_id = global_model_organization_id(db)
    config = global_model_configuration(db)
    if not config:
        raise HTTPException(status_code=409, detail="请先保存视觉模型配置")
    config_id = config.id
    release_read_transaction(db)
    with tempfile.TemporaryDirectory(prefix="xhs-model-test-") as directory:
        path = Path(directory) / "non-report.png"
        Image.new("RGB", (96, 64), "white").save(path)
        try:
            result = await classify_image(path, organization_id)
            ok = isinstance(result.get("is_report"), bool)
            message = "连接成功，模型可以接收图片并返回判断结果"
        except Exception as exc:
            ok = False
            message = str(exc)[:500]
    config = db.get(ModelConfiguration, config_id)
    if config:
        config.last_test_ok = ok
        config.last_test_message = message
        config.last_test_at = utcnow()
        db.commit()
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return {"ok": True, "message": message}


@router.post("/maintenance/cleanup")
def manual_cleanup(current: CurrentUser = Depends(get_current_user)) -> dict:
    return cleanup_expired_files()
