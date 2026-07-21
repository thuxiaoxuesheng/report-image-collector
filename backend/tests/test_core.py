import zipfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import backend.app.ai as ai_module
import backend.app.bootstrap as bootstrap
import backend.app.collector as collector
import backend.app.exporter as exporter
from backend.app.ai import _openai_endpoint, _parse_json
from backend.app.api import (
    create_user,
    move_queued_task,
    read_settings,
    save_model_configuration,
    undo_ai_screen,
)
from backend.app.auth import CurrentUser, get_current_user
from backend.app.browser import is_allowed_url
from backend.app.collector import (
    AttentionRequired,
    configured_limit_reached,
    detect_page_block,
    extract_xsec_token,
)
from backend.app.database import Base, get_db
from backend.app.date_utils import parse_xhs_datetime
from backend.app.main import app
from backend.app.models import (
    CollectedImage,
    CollectionTask,
    ModelConfiguration,
    Note,
    Organization,
    TaskKeyword,
    User,
)
from backend.app.schemas import (
    AdminUserCreate,
    BulkImageUpdate,
    KeywordCreate,
    ModelConfigurationUpdate,
    TaskCreate,
)
from backend.app.secrets import decrypt_secret, encrypt_secret
from backend.app.site_auth import (
    create_opaque_session,
    create_site_session,
    hash_site_password,
    verify_site_password,
    verify_site_session,
)
from backend.app.social_copilot import normalize_note_payload

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_site_password_is_hashed_and_verified() -> None:
    encoded = hash_site_password("a-secure-password", salt=b"0123456789abcdef")
    assert "a-secure-password" not in encoded
    assert verify_site_password("a-secure-password", encoded)
    assert not verify_site_password("wrong-password", encoded)


def test_site_session_is_signed_and_expires() -> None:
    token = create_site_session("testadmin", "session-secret", issued_at=1_000)
    assert verify_site_session(token, "testadmin", "session-secret", max_age_seconds=100, now=1_050)
    assert not verify_site_session(
        token, "testadmin", "wrong-secret", max_age_seconds=100, now=1_050
    )
    assert not verify_site_session(
        token, "testadmin", "session-secret", max_age_seconds=100, now=1_101
    )


def test_database_session_cookie_is_opaque_and_only_hash_is_stored() -> None:
    token, token_hash = create_opaque_session()
    assert token not in token_hash
    assert len(token_hash) == 64


def test_user_secret_encryption_round_trip() -> None:
    encrypted = encrypt_secret("example-private-key-value")
    assert "example-private-key-value" not in encrypted
    assert decrypt_secret(encrypted) == "example-private-key-value"


def test_openai_compatible_endpoint_rejects_local_network() -> None:
    assert (
        _openai_endpoint("https://api.minimaxi.com/v1")
        == "https://api.minimaxi.com/v1/chat/completions"
    )
    with pytest.raises(ValueError):
        _openai_endpoint("https://127.0.0.1/v1")


def test_switching_model_provider_requires_a_new_key(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'model.db').as_posix()}")
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, expire_on_commit=False)
    with test_session() as db:
        organization = Organization(name="模型切换")
        db.add(organization)
        db.flush()
        user = User(
            email="model@example.com",
            display_name="模型用户",
            role="admin",
            organization_id=organization.id,
        )
        db.add(user)
        db.add(
            ModelConfiguration(
                organization_id=organization.id,
                provider="minimax_token_plan",
                encrypted_api_key=encrypt_secret("example-existing-model-key"),
            )
        )
        db.flush()
        current = CurrentUser(user=user, organization=organization)
        payload = ModelConfigurationUpdate(
            provider="openai_compatible",
            base_url="https://api.example.com/v1",
            model_name="vision-model",
        )
        with pytest.raises(HTTPException, match="新API Key") as error:
            save_model_configuration(payload, db, current)
        assert error.value.status_code == 422


def test_all_users_share_the_administrator_model_configuration(
    tmp_path: Path, monkeypatch
) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'shared-model.db').as_posix()}")
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(ai_module, "SessionLocal", test_session)

    with test_session() as db:
        admin_workspace = Organization(name="管理员模型空间")
        user_workspace = Organization(name="普通用户模型空间")
        db.add_all([admin_workspace, user_workspace])
        db.flush()
        admin = User(
            email=ai_module.get_settings().admin_email.lower(),
            display_name="管理员",
            role="admin",
            organization_id=admin_workspace.id,
        )
        ordinary = User(
            email="shared-model-user@example.com",
            display_name="普通用户",
            role="user",
            organization_id=user_workspace.id,
        )
        db.add_all([admin, ordinary])
        db.flush()
        db.add_all(
            [
                ModelConfiguration(
                    organization_id=admin_workspace.id,
                    provider="minimax_token_plan",
                    encrypted_api_key=encrypt_secret("administrator-shared-key"),
                    key_hint="adm••••-key",
                ),
                ModelConfiguration(
                    organization_id=user_workspace.id,
                    provider="minimax_token_plan",
                    encrypted_api_key=encrypt_secret("ignored-user-key"),
                    key_hint="old••••-key",
                ),
            ]
        )
        db.commit()

        ordinary_settings = read_settings(
            db, CurrentUser(user=ordinary, organization=user_workspace)
        )
        admin_settings = read_settings(
            db, CurrentUser(user=admin, organization=admin_workspace)
        )

    configuration, decrypted_key = ai_module._configuration(user_workspace.id)

    assert configuration.organization_id == admin_workspace.id
    assert decrypted_key == "administrator-shared-key"
    assert ordinary_settings["model_configured"] is True
    assert ordinary_settings["model_can_manage"] is False
    assert ordinary_settings["key_hint"] is None
    assert ordinary_settings["last_test_message"] == "管理员已配置全局视觉模型"
    assert admin_settings["model_can_manage"] is True
    assert admin_settings["key_hint"] == "adm••••-key"


def test_ordinary_user_cannot_modify_the_shared_model_configuration(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'model-permission.db').as_posix()}")
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, expire_on_commit=False)
    with test_session() as db:
        workspace = Organization(name="普通用户权限空间")
        db.add(workspace)
        db.flush()
        ordinary = User(
            email="model-permission@example.com",
            display_name="普通用户",
            role="user",
            organization_id=workspace.id,
        )
        db.add(ordinary)
        db.commit()

    def override_db():
        with test_session() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user=ordinary, organization=workspace
    )
    try:
        response = TestClient(app).put(
            "/api/settings/model",
            json={
                "provider": "minimax_token_plan",
                "api_key": "ordinary-user-must-not-save-this-key",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()["detail"] == "需要管理员权限"


def test_allowed_browser_navigation_is_restricted_to_xhs() -> None:
    assert is_allowed_url("https://www.xiaohongshu.com/explore")
    assert is_allowed_url("https://sns-webpic-qc.xhscdn.com/example.jpg")
    assert is_allowed_url("chrome-extension://extension-id/sidepanel.html")
    assert not is_allowed_url("http://www.xiaohongshu.com/explore")
    assert not is_allowed_url("data:text/html,not-allowed")
    assert not is_allowed_url("https://example.com/")
    assert not is_allowed_url("file:///C:/Users/test/secret.txt")


def test_social_copilot_note_response_is_normalized() -> None:
    payload = {
        "body": {
            "success": True,
            "data": {
                "items": [
                    {
                        "note_card": {
                            "type": "normal",
                            "title": "血常规报告",
                            "time": 1783785600000,
                            "user": {"nickname": "测试用户"},
                            "image_list": [
                                {"url_default": "http://sns-webpic-qc.xhscdn.com/a.jpg"},
                                {"info_list": [{"url": "https://sns-webpic-qc.xhscdn.com/b.jpg"}]},
                            ],
                        }
                    }
                ]
            },
        }
    }
    note = normalize_note_payload(payload)
    assert note is not None
    assert note["type"] == "normal"
    assert note["title"] == "血常规报告"
    assert note["author"] == "测试用户"
    assert note["published_at"] is not None
    assert note["images"] == [
        "https://sns-webpic-qc.xhscdn.com/a.jpg",
        "https://sns-webpic-qc.xhscdn.com/b.jpg",
    ]


def test_xsec_token_is_read_from_search_state_link() -> None:
    url = "https://www.xiaohongshu.com/explore/note123?xsec_token=a%2Bb%3D&xsec_source=pc_search"
    assert extract_xsec_token(url) == "a+b="


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("发布于 2026-07-01", date(2026, 7, 1)),
        ("编辑于 07-02", date(2026, 7, 2)),
        ("昨天 12:30", date(2026, 7, 12)),
        ("3天前", date(2026, 7, 10)),
    ],
)
def test_parse_visible_publication_dates(value: str, expected: date) -> None:
    now = datetime(2026, 7, 13, 10, 0, tzinfo=SHANGHAI)
    parsed = parse_xhs_datetime(value, now)
    assert parsed is not None
    assert parsed.date() == expected


def test_multi_keyword_task_validation() -> None:
    task = TaskCreate(
        name="检查检验报告",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 7, 1),
        keywords=[
            KeywordCreate(keyword="血常规报告单", target_count=50),
            KeywordCreate(keyword="CT检查报告", target_count=30),
        ],
        privacy_confirmed=True,
        ai_confirmed=False,
    )
    assert len(task.keywords) == 2


def test_keyword_targets_and_task_total_have_no_product_cap() -> None:
    task = TaskCreate(
        name="大批量检查检验报告",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 7, 1),
        keywords=[
            KeywordCreate(keyword=f"报告单-{index}", target_count=1_000_000)
            for index in range(10)
        ],
        privacy_confirmed=True,
    )
    assert sum(keyword.target_count for keyword in task.keywords) == 10_000_000


def test_zero_operator_quantity_limits_mean_unlimited() -> None:
    assert not configured_limit_reached(10_000_000, 0)
    assert not configured_limit_reached(10_000_000, -1)
    assert not configured_limit_reached(149, 150)
    assert configured_limit_reached(150, 150)


def test_duplicate_keywords_are_rejected() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            name="重复关键词",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 7, 1),
            keywords=[KeywordCreate(keyword="检查报告"), KeywordCreate(keyword="检查报告")],
            privacy_confirmed=True,
        )


def test_bulk_review_supports_more_than_two_thousand_images() -> None:
    payload = BulkImageUpdate(image_ids=[f"image-{index}" for index in range(3_000)])
    assert len(payload.image_ids) == 3_000


def test_ai_response_is_strictly_limited_to_binary_report_verdict() -> None:
    parsed = _parse_json('{"is_report":true,"confidence":0.82,"reason":"符合报告版式"}')
    assert parsed["is_report"] is True
    assert parsed["confidence"] == pytest.approx(0.82)
    assert parsed["reason"] == "符合报告版式"
    with pytest.raises(ValueError):
        _parse_json('{"is_report":"true","confidence":1,"reason":""}')


@pytest.mark.asyncio
async def test_ai_screen_sets_current_selection_and_can_be_undone(
    tmp_path: Path, monkeypatch
) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'ai.db').as_posix()}")
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(collector, "SessionLocal", test_session)

    async def fake_classify(path: Path, _organization_id: str) -> dict:
        is_report = path.stem == "report"
        return {
            "is_report": is_report,
            "confidence": 0.9,
            "reason": "测试",
            "model": "test",
        }

    monkeypatch.setattr(collector, "classify_image", fake_classify)
    with test_session() as db:
        organization = Organization(name="AI测试")
        db.add(organization)
        db.flush()
        user = User(email="ai@example.com", display_name="AI", role="admin")
        db.add(user)
        db.flush()
        task = CollectionTask(
            organization_id=organization.id,
            created_by_id=user.id,
            name="AI筛选",
            status="classifying",
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 2),
            privacy_confirmed=True,
            ai_confirmed=True,
        )
        db.add(task)
        db.flush()
        keyword = TaskKeyword(task_id=task.id, keyword="血常规报告单", target_count=1)
        db.add(keyword)
        db.flush()
        note = Note(
            task_id=task.id,
            keyword_id=keyword.id,
            platform_note_id="ai-note",
            title="AI测试",
            author="",
            source_url="https://www.xiaohongshu.com/explore/ai-note",
        )
        db.add(note)
        db.flush()
        report_path = tmp_path / "report.jpg"
        non_report_path = tmp_path / "non-report.jpg"
        db.add_all(
            [
                CollectedImage(note_id=note.id, local_path=str(report_path), ordinal=1),
                CollectedImage(note_id=note.id, local_path=str(non_report_path), ordinal=2),
            ]
        )
        db.commit()
        task_id, user_id = task.id, user.id

    stats = await collector.classify_task_images(task_id)
    assert stats == {"attempted": 2, "succeeded": 2, "failed": 0}
    with test_session() as db:
        images = list(db.scalars(select(CollectedImage).order_by(CollectedImage.ordinal)))
        assert [image.selected for image in images] == [True, False]
        assert [image.ai_previous_selected for image in images] == [True, True]
        assert [image.ai_reason for image in images] == ["测试", "测试"]
        task = db.get(CollectionTask, task_id)
        task.status = "review"
        images[0].selected = False  # AI之后的人工修改；撤销作为后一次操作仍应覆盖它。
        db.commit()
        current = CurrentUser(
            user=db.get(User, user_id), organization=db.get(Organization, task.organization_id)
        )
        result = undo_ai_screen(task_id, db, current)
        assert result == {"restored": 2}
        assert [image.selected for image in images] == [True, True]


@pytest.mark.asyncio
async def test_captcha_signal_pauses_instead_of_being_bypassed(monkeypatch) -> None:
    async def fake_body(_page) -> str:
        return "请完成验证，拖动滑块"

    monkeypatch.setattr(collector, "visible_body_text", fake_body)
    with pytest.raises(AttentionRequired, match="请完成验证"):
        await detect_page_block(object())


def test_queue_reordering_swaps_deterministic_positions(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'queue.db').as_posix()}")
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, expire_on_commit=False)
    with test_session() as db:
        organization = Organization(name="队列测试")
        db.add(organization)
        db.flush()
        user = User(email="queue@example.com", display_name="队列", role="admin")
        db.add(user)
        db.flush()
        tasks = []
        for position in range(1, 4):
            task = CollectionTask(
                organization_id=organization.id,
                created_by_id=user.id,
                name=f"任务{position}",
                status="queued",
                queue_position=position,
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 2),
                privacy_confirmed=True,
            )
            db.add(task)
            tasks.append(task)
        db.flush()
        move_queued_task(db, tasks[2], -1)
        assert [task.queue_position for task in tasks] == [1, 3, 2]


def test_admin_created_user_gets_independent_workspace_and_temporary_password(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'users.db').as_posix()}")
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, expire_on_commit=False)
    with test_session() as db:
        admin_org = Organization(name="管理员工作空间")
        db.add(admin_org)
        db.flush()
        admin = User(
            email="admin@example.com",
            username="admin",
            display_name="管理员",
            role="admin",
            organization_id=admin_org.id,
            active=True,
        )
        db.add(admin)
        db.flush()
        result = create_user(
            AdminUserCreate(username="user01", display_name="第一用户"),
            db=db,
            current=CurrentUser(user=admin, organization=admin_org),
        )
        created = db.scalar(select(User).where(User.username == "user01"))
        assert created is not None
        assert created.role == "user"
        assert created.organization_id != admin.organization_id
        assert created.must_change_password is True
        assert verify_site_password(result["temporary_password"], created.password_hash)


def test_existing_single_user_database_migrates_without_moving_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'migration.db').as_posix()}")
    Organization.__table__.create(engine)
    settings = bootstrap.get_settings()
    organization_id = "00000000-0000-0000-0000-000000000001"
    user_id = "00000000-0000-0000-0000-000000000002"
    with engine.begin() as connection:
        connection.execute(
            text(
                """CREATE TABLE users (
                id VARCHAR(36) PRIMARY KEY,
                email VARCHAR(320) NOT NULL UNIQUE,
                display_name VARCHAR(120) NOT NULL,
                role VARCHAR(32) NOT NULL,
                organization_id VARCHAR(36),
                active BOOLEAN NOT NULL,
                created_at DATETIME NOT NULL,
                FOREIGN KEY(organization_id) REFERENCES organizations(id)
                )"""
            )
        )
        connection.execute(
            text(
                "INSERT INTO organizations (id,name,status,created_at,updated_at) "
                "VALUES (:id,'默认机构','active',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ),
            {"id": organization_id},
        )
        connection.execute(
            text(
                "INSERT INTO users (id,email,display_name,role,organization_id,active,created_at) "
                "VALUES (:id,:email,'系统管理员','admin',NULL,1,CURRENT_TIMESTAMP)"
            ),
            {"id": user_id, "email": settings.admin_email.lower()},
        )
    test_session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(bootstrap, "engine", engine)
    monkeypatch.setattr(bootstrap, "SessionLocal", test_session)
    monkeypatch.setattr(bootstrap, "get_minimax_key", lambda: "")
    bootstrap.initialize_database()
    columns = {item["name"] for item in inspect(engine).get_columns("users")}
    assert {"username", "password_hash", "must_change_password", "last_login_at"} <= columns
    with test_session() as db:
        migrated = db.get(User, user_id)
        assert migrated.organization_id == organization_id
        assert migrated.username


def test_legacy_shared_workspace_is_split_per_user(tmp_path: Path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'shared.db').as_posix()}")
    Organization.__table__.create(engine)
    settings = bootstrap.get_settings()
    workspace_id = "10000000-0000-0000-0000-000000000001"
    admin_id = "10000000-0000-0000-0000-000000000002"
    user_id = "10000000-0000-0000-0000-000000000003"
    with engine.begin() as connection:
        connection.execute(
            text(
                """CREATE TABLE users (
                id VARCHAR(36) PRIMARY KEY,
                email VARCHAR(320) NOT NULL UNIQUE,
                display_name VARCHAR(120) NOT NULL,
                role VARCHAR(32) NOT NULL,
                organization_id VARCHAR(36),
                active BOOLEAN NOT NULL,
                created_at DATETIME NOT NULL,
                FOREIGN KEY(organization_id) REFERENCES organizations(id)
                )"""
            )
        )
        connection.execute(
            text(
                "INSERT INTO organizations (id,name,status,created_at,updated_at) "
                "VALUES (:id,'旧共享空间','active',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ),
            {"id": workspace_id},
        )
        connection.execute(
            text(
                "INSERT INTO users (id,email,display_name,role,organization_id,active,created_at) "
                "VALUES (:admin_id,:admin_email,'管理员','admin',:workspace_id,1,"
                "CURRENT_TIMESTAMP),(:user_id,'user@example.com','普通用户','institution',"
                ":workspace_id,1,CURRENT_TIMESTAMP)"
            ),
            {
                "admin_id": admin_id,
                "admin_email": settings.admin_email.lower(),
                "user_id": user_id,
                "workspace_id": workspace_id,
            },
        )
    # Create the remaining current tables around the deliberately legacy users table.
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, expire_on_commit=False)
    with test_session() as db:
        db.add(
            ModelConfiguration(
                organization_id=workspace_id,
                provider="minimax_token_plan",
                encrypted_api_key=encrypt_secret("example-shared-legacy-key"),
                key_hint="sk-••••-key",
            )
        )
        db.add(
            bootstrap.SeenNote(
                organization_id=workspace_id,
                platform_note_id="legacy-note",
                first_keyword="检查报告",
            )
        )
        db.commit()

    monkeypatch.setattr(bootstrap, "engine", engine)
    monkeypatch.setattr(bootstrap, "SessionLocal", test_session)
    monkeypatch.setattr(bootstrap, "get_minimax_key", lambda: "")
    bootstrap.initialize_database()

    with test_session() as db:
        admin = db.get(User, admin_id)
        ordinary = db.get(User, user_id)
        assert admin.organization_id == workspace_id
        assert ordinary.organization_id != workspace_id
        assert ordinary.role == "user"
        configurations = list(db.scalars(select(ModelConfiguration)))
        assert {item.organization_id for item in configurations} == {
            admin.organization_id,
            ordinary.organization_id,
        }
        seen_workspaces = set(
            db.scalars(
                select(bootstrap.SeenNote.organization_id).where(
                    bootstrap.SeenNote.platform_note_id == "legacy-note"
                )
            )
        )
        assert seen_workspaces == {admin.organization_id, ordinary.organization_id}
        ordinary.organization_id = admin.organization_id
        with pytest.raises(IntegrityError):
            db.commit()


def test_export_contains_selected_images_and_csv(tmp_path: Path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(exporter, "SessionLocal", test_session)
    monkeypatch.setattr(exporter.settings, "data_dir", tmp_path)
    (tmp_path / "exports").mkdir()

    image_path = tmp_path / "source.jpg"
    Image.new("RGB", (20, 20), "white").save(image_path)
    with test_session() as db:
        organization = Organization(name="测试")
        db.add(organization)
        db.flush()
        user = User(email="test@example.com", display_name="测试", role="admin")
        db.add(user)
        db.flush()
        task = CollectionTask(
            organization_id=organization.id,
            created_by_id=user.id,
            name="报告图片",
            status="review",
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 2),
            privacy_confirmed=True,
        )
        db.add(task)
        db.flush()
        keyword = TaskKeyword(task_id=task.id, keyword="检查报告单", target_count=1)
        db.add(keyword)
        db.flush()
        note = Note(
            task_id=task.id,
            keyword_id=keyword.id,
            platform_note_id="note123",
            title="测试报告",
            author="",
            source_url="https://www.xiaohongshu.com/explore/note123",
        )
        db.add(note)
        db.flush()
        db.add(
            CollectedImage(
                note_id=note.id,
                local_path=str(image_path),
                ordinal=1,
                selected=True,
                ai_is_report=True,
                ai_confidence=0.91,
            )
        )
        db.commit()
        task_id, user_id = task.id, user.id

    record = exporter.build_export(task_id, user_id)
    with zipfile.ZipFile(record.file_path) as archive:
        names = set(archive.namelist())
        assert "manifest.csv" in names
        assert "使用与隐私说明.txt" in names
        assert "01_检查报告单/note123_001.jpg" in names
        manifest = archive.read("manifest.csv").decode("utf-8-sig")
        assert "关键词,笔记ID" in manifest
        assert "检查报告单,note123" in manifest
        assert "AI是否报告单,AI置信度,AI理由" in manifest
        assert ",是,0.91," in manifest
