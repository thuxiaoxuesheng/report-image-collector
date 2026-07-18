from __future__ import annotations

import shutil
import uuid

from sqlalchemy import exists, inspect, select, text

from .config import get_settings
from .database import Base, SessionLocal, engine
from .enums import OrganizationStatus, Role
from .models import (
    BrowserLease,
    CollectionTask,
    ConsentRecord,
    ModelConfiguration,
    Organization,
    SeenNote,
    User,
)
from .secrets import encrypt_secret, get_minimax_key, secret_hint


def initialize_database() -> None:
    if engine.dialect.name != "postgresql":
        _initialize_database_unlocked()
        return
    # Web and worker processes may start together. A session-scoped advisory
    # lock makes schema/bootstrap work a single-writer operation.
    lock_key = 7_146_835_127_421_909_311
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
        try:
            _initialize_database_unlocked()
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})


def _initialize_database_unlocked() -> None:
    settings = get_settings()
    settings.ensure_directories()
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    lease_columns = {
        column["name"] for column in inspector.get_columns("browser_leases")
    }
    if "user_id" not in lease_columns:
        # Browser leases are transient coordination state, so replacing the old
        # workspace-keyed table is safe and avoids carrying stale locks forward.
        BrowserLease.__table__.drop(engine)
        BrowserLease.__table__.create(engine)
        inspector = inspect(engine)
    if inspector.dialect.name != "sqlite":
        _bootstrap_default_workspace()
        _ensure_unique_user_workspaces()
        return
    columns = {column["name"] for column in inspector.get_columns("collected_images")}
    if "ai_is_report" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE collected_images ADD COLUMN ai_is_report BOOLEAN"))
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_collected_images_ai_is_report "
                    "ON collected_images (ai_is_report)"
                )
            )
    if "ai_previous_selected" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE collected_images ADD COLUMN ai_previous_selected BOOLEAN")
            )
    user_columns = {column["name"] for column in inspect(engine).get_columns("users")}
    user_migrations = {
        "username": "ALTER TABLE users ADD COLUMN username VARCHAR(64)",
        "password_hash": "ALTER TABLE users ADD COLUMN password_hash TEXT",
        "must_change_password": (
            "ALTER TABLE users ADD COLUMN must_change_password BOOLEAN DEFAULT 0"
        ),
        "last_login_at": "ALTER TABLE users ADD COLUMN last_login_at DATETIME",
    }
    with engine.begin() as connection:
        for name, statement in user_migrations.items():
            if name not in user_columns:
                connection.execute(text(statement))
        connection.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username)")
        )
    task_columns = {
        column["name"] for column in inspect(engine).get_columns("collection_tasks")
    }
    task_migrations = {
        "lease_owner": "ALTER TABLE collection_tasks ADD COLUMN lease_owner VARCHAR(160)",
        "lease_token": "ALTER TABLE collection_tasks ADD COLUMN lease_token VARCHAR(64)",
        "lease_expires_at": "ALTER TABLE collection_tasks ADD COLUMN lease_expires_at DATETIME",
        "heartbeat_at": "ALTER TABLE collection_tasks ADD COLUMN heartbeat_at DATETIME",
        "attempt_count": (
            "ALTER TABLE collection_tasks ADD COLUMN attempt_count INTEGER DEFAULT 0"
        ),
        "state_version": (
            "ALTER TABLE collection_tasks ADD COLUMN state_version INTEGER DEFAULT 0"
        ),
        "classification_keyword_id": (
            "ALTER TABLE collection_tasks ADD COLUMN classification_keyword_id VARCHAR(36)"
        ),
        "classification_final_status": (
            "ALTER TABLE collection_tasks ADD COLUMN classification_final_status VARCHAR(32)"
        ),
    }
    with engine.begin() as connection:
        for name, statement in task_migrations.items():
            if name not in task_columns:
                connection.execute(text(statement))
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_collection_tasks_lease_token "
                "ON collection_tasks (lease_token)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_collection_tasks_lease_expires_at "
                "ON collection_tasks (lease_expires_at)"
            )
        )
    _bootstrap_default_workspace()
    _ensure_unique_user_workspaces()


def _ensure_unique_user_workspaces() -> None:
    # SQLite and PostgreSQL both support IF NOT EXISTS here. The bootstrap
    # migration below first splits any legacy shared rows, so adding the
    # constraint is safe on upgrades as well as fresh installations.
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_private_workspace "
                "ON users (organization_id)"
            )
        )


def _bootstrap_default_workspace() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        # Old releases called ordinary users "institution". Keep the database
        # compatible while exposing a single user concept from now on.
        db.query(User).filter(User.role == "institution").update({User.role: Role.USER})
        admin = db.scalar(select(User).where(User.email == settings.admin_email.lower()))
        organization = (
            db.get(Organization, admin.organization_id)
            if admin and admin.organization_id
            else None
        )
        if not organization and admin:
            organization = db.scalar(
                select(Organization)
                .where(
                    ~exists().where(User.organization_id == Organization.id)
                )
                .limit(1)
            )
        if not organization:
            organization = Organization(
                name=f"用户工作空间-{uuid.uuid4().hex[:10]}",
                status=OrganizationStatus.ACTIVE,
            )
            db.add(organization)
            db.flush()

        if not admin:
            admin = User(
                email=settings.admin_email.lower(),
                display_name="系统管理员",
                role=Role.ADMIN,
                organization_id=organization.id,
            )
            db.add(admin)
        admin.username = (
            admin.username or settings.site_auth_username or "admin"
        ).strip().lower()
        admin.password_hash = admin.password_hash or settings.site_auth_password_hash or None
        admin.organization_id = admin.organization_id or organization.id
        admin.active = True
        admin.must_change_password = False

        _split_legacy_shared_workspaces(db, admin.id)

        # Move the only long-lived browser state from the legacy compatibility
        # directory to a directory directly owned by the user.
        for user in db.scalars(select(User).where(User.organization_id.is_not(None))):
            legacy_profile = (
                settings.data_dir
                / "organizations"
                / str(user.organization_id)
                / "browser-profile"
            )
            user_profile = settings.data_dir / "users" / user.id / "browser-profile"
            if legacy_profile.exists() and not user_profile.exists():
                user_profile.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(legacy_profile), str(user_profile))

        # Migrate the previous single global key into the administrator's workspace once.
        admin_organization_id = admin.organization_id or organization.id
        existing_config = db.scalar(
            select(ModelConfiguration).where(
                ModelConfiguration.organization_id == admin_organization_id
            )
        )
        legacy_key = settings.minimax_api_key
        if not legacy_key:
            try:
                legacy_key = get_minimax_key()
            except Exception:
                legacy_key = ""
        if legacy_key and not existing_config:
            db.add(
                ModelConfiguration(
                    organization_id=admin_organization_id,
                    provider="minimax_token_plan",
                    encrypted_api_key=encrypt_secret(legacy_key),
                    key_hint=secret_hint(legacy_key),
                )
            )

        db.commit()


def _split_legacy_shared_workspaces(db, admin_id: str) -> None:
    """Turn every legacy shared organization into one private row per user.

    The old schema allowed multiple accounts to point at one organization.
    Product isolation now follows the user, so keeping that shape would share
    model settings and seen-note history. One account retains the historical
    workspace (and therefore its browser profile); every other account receives
    a new workspace plus independent copies of configuration and dedup history.
    """
    users = list(
        db.scalars(
            select(User)
            .where(User.organization_id.is_not(None))
            .order_by(User.created_at, User.id)
        )
    )
    grouped: dict[str, list[User]] = {}
    for user in users:
        if user.organization_id:
            grouped.setdefault(user.organization_id, []).append(user)

    for workspace_id, members in grouped.items():
        if len(members) < 2:
            continue
        keeper = next((item for item in members if item.id == admin_id), members[0])
        source_config = db.scalar(
            select(ModelConfiguration).where(
                ModelConfiguration.organization_id == workspace_id
            )
        )
        seen_notes = list(
            db.scalars(
                select(SeenNote).where(SeenNote.organization_id == workspace_id)
            )
        )
        for user in members:
            if user.id == keeper.id:
                continue
            workspace = Organization(
                name=f"用户工作空间-{uuid.uuid4().hex[:10]}",
                status=OrganizationStatus.ACTIVE,
            )
            db.add(workspace)
            db.flush()
            user.organization_id = workspace.id
            db.query(CollectionTask).filter(
                CollectionTask.created_by_id == user.id
            ).update({CollectionTask.organization_id: workspace.id})
            db.query(ConsentRecord).filter(ConsentRecord.user_id == user.id).update(
                {ConsentRecord.organization_id: workspace.id}
            )
            if source_config:
                db.add(
                    ModelConfiguration(
                        organization_id=workspace.id,
                        provider=source_config.provider,
                        base_url=source_config.base_url,
                        model_name=source_config.model_name,
                        encrypted_api_key=source_config.encrypted_api_key,
                        key_hint=source_config.key_hint,
                        last_test_ok=source_config.last_test_ok,
                        last_test_message=source_config.last_test_message,
                        last_test_at=source_config.last_test_at,
                    )
                )
            db.add_all(
                [
                    SeenNote(
                        organization_id=workspace.id,
                        platform_note_id=item.platform_note_id,
                        first_keyword=item.first_keyword,
                        first_seen_at=item.first_seen_at,
                        published_at=item.published_at,
                    )
                    for item in seen_notes
                ]
            )
        db.flush()
