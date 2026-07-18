from __future__ import annotations

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

import backend.app.browser as browser_module
import backend.app.browser_leases as browser_leases
import backend.app.main as main_module
import backend.app.task_queue as task_queue
from backend.app.api import apply_task_action
from backend.app.browser import BrowserRuntime
from backend.app.collector import resolved_worker_concurrency
from backend.app.database import Base
from backend.app.exporter import safe_csv_cell, safe_name
from backend.app.login_security import (
    clear_login_failures,
    login_allowed,
    record_login_failure,
)
from backend.app.main import (
    app,
    browser_security,
    create_login_csrf,
    is_cross_site_write,
    valid_login_csrf,
)
from backend.app.models import CollectionTask, LoginThrottle, Organization, User, utcnow
from scripts.local_api import console_output


def _write_request(*, origin: str = "", fetch_site: str = "", forwarded_host: str = ""):
    headers = [(b"host", b"collector.example.com")]
    if origin:
        headers.append((b"origin", origin.encode()))
    if fetch_site:
        headers.append((b"sec-fetch-site", fetch_site.encode()))
    if forwarded_host:
        headers.append((b"x-forwarded-host", forwarded_host.encode()))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/site-login",
            "raw_path": b"/site-login",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8765),
        }
    )


def test_same_origin_write_wins_over_inconsistent_fetch_metadata() -> None:
    assert not is_cross_site_write(
        _write_request(
            origin="https://collector.example.com", fetch_site="cross-site"
        )
    )
    assert is_cross_site_write(
        _write_request(origin="https://attacker.example", fetch_site="same-origin")
    )
    assert is_cross_site_write(_write_request(fetch_site="cross-site"))


def test_loopback_reverse_proxy_forwarded_host_is_accepted() -> None:
    request = _write_request(
        origin="https://public.example",
        fetch_site="same-origin",
        forwarded_host="public.example",
    )
    request.scope["headers"] = [
        (b"host", b"127.0.0.1:8765"),
        (b"origin", b"https://public.example"),
        (b"sec-fetch-site", b"same-origin"),
        (b"x-forwarded-host", b"public.example"),
    ]
    assert not is_cross_site_write(request)


def test_cancelled_browser_start_releases_capacity_and_lease(
    tmp_path, monkeypatch
) -> None:
    class Capacity:
        def __init__(self):
            self.acquired = 0
            self.released = 0

        async def acquire(self):
            self.acquired += 1

        def release(self):
            self.released += 1

    class Chromium:
        async def launch_persistent_context(self, **_options):
            raise asyncio.CancelledError

    class Manager:
        def __init__(self):
            self.settings = SimpleNamespace(
                data_dir=tmp_path,
                browser_headless=False,
                browser_viewport_width=1280,
                browser_viewport_height=800,
                browser_channel="chrome",
                browser_start_timeout_seconds=1,
                browser_close_timeout_seconds=1,
                browser_lease_heartbeat_seconds=30,
            )
            self.capacity = Capacity()
            self.owner = "test-owner"
            self._playwright = SimpleNamespace(chromium=Chromium())

        async def playwright(self):
            return self._playwright

    released = []
    monkeypatch.setattr(browser_module, "acquire_browser_lease", lambda *_args: "token")
    monkeypatch.setattr(
        browser_module,
        "release_browser_lease",
        lambda *args: released.append(args) or True,
    )
    manager = Manager()
    runtime = BrowserRuntime(manager, "test-user")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runtime.open_remote())

    assert manager.capacity.acquired == 1
    assert manager.capacity.released == 1
    assert not runtime.remote_active
    assert not runtime._capacity_acquired
    assert len(released) == 1


def test_profile_maintenance_lease_allows_inactive_user(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'maintenance-lease.db').as_posix()}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(browser_leases, "SessionLocal", sessions)
    with sessions() as db:
        user = User(
            email="inactive@example.invalid",
            username="inactive-user",
            display_name="Inactive User",
            role="user",
            active=False,
        )
        db.add(user)
        db.commit()
        user_id = user.id

    assert browser_leases.acquire_browser_lease(user_id, "owner", "remote") is None
    token = browser_leases.acquire_browser_lease(
        user_id,
        "owner",
        "clear",
        allow_inactive=True,
    )
    assert token
    assert (
        browser_leases.acquire_browser_lease(
            user_id,
            "other-owner",
            "clear",
            allow_inactive=True,
        )
        is None
    )
    browser_leases.release_browser_lease(user_id, "owner", token)


def test_closed_page_reuses_existing_context_without_second_capacity_slot(
    tmp_path,
) -> None:
    class Capacity:
        def __init__(self):
            self.acquired = 0

        async def acquire(self):
            self.acquired += 1

        def release(self):
            pass

    class Page:
        url = "about:blank"

        def __init__(self, closed=False):
            self.closed = closed

        def is_closed(self):
            return self.closed

    class Context:
        def __init__(self):
            self.pages = [Page(closed=True)]
            self.created = 0

        async def new_page(self):
            self.created += 1
            return Page()

    class Manager:
        def __init__(self):
            self.settings = SimpleNamespace(data_dir=tmp_path)
            self.capacity = Capacity()

        async def playwright(self):
            raise AssertionError("existing context must not launch Playwright")

    manager = Manager()
    runtime = BrowserRuntime(manager, "test-user")
    context = Context()
    runtime._context = context
    runtime._page = context.pages[0]
    runtime._capacity_acquired = True

    page = asyncio.run(runtime.ensure_started())

    assert page is runtime._page
    assert not page.is_closed()
    assert context.created == 1
    assert manager.capacity.acquired == 0
    assert runtime._capacity_acquired


def test_cancel_during_context_close_still_releases_capacity_and_lease(
    tmp_path, monkeypatch
) -> None:
    class Capacity:
        def __init__(self):
            self.released = 0

        def release(self):
            self.released += 1

    class Context:
        def __init__(self):
            self.started = asyncio.Event()
            self.finish = asyncio.Event()
            self.closed = False

        async def close(self):
            self.started.set()
            await self.finish.wait()
            self.closed = True

    manager = SimpleNamespace(
        settings=SimpleNamespace(
            data_dir=tmp_path,
            browser_close_timeout_seconds=1,
        ),
        capacity=Capacity(),
    )
    runtime = BrowserRuntime(manager, "test-user")
    context = Context()
    runtime._context = context
    runtime._capacity_acquired = True
    runtime._lease_owner = "owner"
    runtime._lease_token = "token"
    released = []
    monkeypatch.setattr(
        browser_module,
        "release_browser_lease",
        lambda *args: released.append(args) or True,
    )

    async def cancel_close():
        task = asyncio.create_task(runtime.close())
        await context.started.wait()
        task.cancel()
        context.finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_close())

    assert context.closed
    assert runtime._context is None
    assert not runtime._capacity_acquired
    assert manager.capacity.released == 1
    assert len(released) == 1


def test_context_close_has_hard_deadline_when_cancellation_is_ignored(
    tmp_path, monkeypatch
) -> None:
    class Capacity:
        def __init__(self):
            self.released = 0

        def release(self):
            self.released += 1

    class Context:
        async def close(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.Event().wait()

    manager = SimpleNamespace(
        settings=SimpleNamespace(
            data_dir=tmp_path,
            browser_close_timeout_seconds=0.01,
        ),
        capacity=Capacity(),
    )
    runtime = BrowserRuntime(manager, "test-user")
    runtime._context = Context()
    runtime._capacity_acquired = True
    runtime._lease_owner = "owner"
    runtime._lease_token = "token"
    released = []
    monkeypatch.setattr(
        browser_module,
        "release_browser_lease",
        lambda *args: released.append(args) or True,
    )

    async def close_with_outer_deadline():
        await asyncio.wait_for(runtime.close(), timeout=1.5)
        assert runtime._poisoned_close_task is not None
        assert runtime._capacity_acquired
        assert manager.capacity.released == 0
        assert released == []
        with pytest.raises(RuntimeError, match="未能完全关闭"):
            await runtime.ensure_started()
        runtime._poisoned_close_task.cancel()
        await asyncio.gather(runtime._poisoned_close_task, return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(close_with_outer_deadline())

    assert runtime._context is None
    assert not runtime._capacity_acquired
    assert manager.capacity.released == 1
    assert len(released) == 1


def test_outer_cancel_during_close_grace_keeps_profile_lease(
    tmp_path, monkeypatch
) -> None:
    class Capacity:
        def __init__(self):
            self.released = 0

        def release(self):
            self.released += 1

    class Context:
        def __init__(self):
            self.cancel_seen = asyncio.Event()
            self.finish = asyncio.Event()

        async def close(self):
            try:
                await self.finish.wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()
                await self.finish.wait()

    manager = SimpleNamespace(
        settings=SimpleNamespace(
            data_dir=tmp_path,
            browser_close_timeout_seconds=0.01,
        ),
        capacity=Capacity(),
    )
    runtime = BrowserRuntime(manager, "test-user")
    context = Context()
    runtime._context = context
    runtime._capacity_acquired = True
    runtime._lease_owner = "owner"
    runtime._lease_token = "token"
    released = []
    monkeypatch.setattr(
        browser_module,
        "release_browser_lease",
        lambda *args: released.append(args) or True,
    )

    async def cancel_during_grace():
        outer_close = asyncio.create_task(runtime.close())
        await context.cancel_seen.wait()
        outer_close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer_close
        assert runtime._poisoned_close_task is not None
        assert runtime._capacity_acquired
        assert manager.capacity.released == 0
        assert released == []
        context.finish.set()
        await asyncio.gather(runtime._poisoned_close_task, return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(cancel_during_grace())

    assert runtime._poisoned_close_task is None
    assert not runtime._capacity_acquired
    assert manager.capacity.released == 1
    assert len(released) == 1


def test_login_csrf_token_survives_proxy_header_rewrite() -> None:
    token = create_login_csrf()
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/site-login",
            "raw_path": b"/site-login",
            "query_string": f"csrf={token}".encode(),
            "headers": [
                (b"host", b"internal-proxy"),
                (b"origin", b"https://collector.example.com"),
                (b"sec-fetch-site", b"cross-site"),
                (b"cookie", f"xhs_login_csrf={token}".encode()),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8765),
        }
    )
    assert is_cross_site_write(request)
    assert valid_login_csrf(request)
    request_without_cookie = _write_request(
        origin="https://collector.example.com", fetch_site="cross-site"
    )
    request_without_cookie.scope["query_string"] = f"csrf={token}".encode()
    assert not valid_login_csrf(request_without_cookie)
    request_with_wrong_cookie = _write_request(
        origin="https://collector.example.com", fetch_site="cross-site"
    )
    request_with_wrong_cookie.scope["query_string"] = f"csrf={token}".encode()
    request_with_wrong_cookie.scope["headers"].append(
        (b"cookie", b"xhs_login_csrf=another-token")
    )
    assert not valid_login_csrf(request_with_wrong_cookie)
    bad_request = _write_request(
        origin="https://collector.example.com", fetch_site="cross-site"
    )
    bad_request.scope["query_string"] = b"csrf=wrong-token"
    assert not valid_login_csrf(bad_request)


def test_security_headers_middleware_is_outermost() -> None:
    assert app.user_middleware[0].kwargs["dispatch"] is browser_security


def test_login_page_response_has_security_headers(monkeypatch) -> None:
    settings = SimpleNamespace(site_auth_enabled=True, environment="production")
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    response = TestClient(app).get("/site-login")

    assert response.status_code == 200
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["strict-transport-security"].startswith("max-age=")
    csrf_cookie = response.cookies.get("xhs_login_csrf")
    assert csrf_cookie
    assert f'action="/site-login?csrf={csrf_cookie}"' in response.text


def test_local_api_status_is_emitted_before_response_body() -> None:
    assert console_output(200, b'{"ok":true}') == (
        b'HTTP_STATUS=200\n{"ok":true}\n'
    )


def _queued_task(organization_id: str, user_id: str, name: str, position: int):
    return CollectionTask(
        organization_id=organization_id,
        created_by_id=user_id,
        name=name,
        status="queued",
        queue_position=position,
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 2),
        privacy_confirmed=True,
    )


def test_queue_runs_different_users_in_parallel_but_serializes_same_browser(
    tmp_path, monkeypatch
) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'leases.db').as_posix()}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(task_queue, "engine", engine)
    monkeypatch.setattr(task_queue, "SessionLocal", sessions)

    with sessions() as db:
        first_workspace = Organization(name="用户甲工作空间")
        second_workspace = Organization(name="用户乙工作空间")
        db.add_all([first_workspace, second_workspace])
        db.flush()
        first_user = User(email="a@example.com", display_name="甲", role="user")
        second_user = User(email="b@example.com", display_name="乙", role="user")
        db.add_all([first_user, second_user])
        db.flush()
        db.add_all(
            [
                _queued_task(first_workspace.id, first_user.id, "甲-1", 1),
                _queued_task(first_workspace.id, first_user.id, "甲-2", 2),
                _queued_task(second_workspace.id, second_user.id, "乙-1", 3),
            ]
        )
        db.commit()

    first = task_queue.claim_collection("worker-1")
    second = task_queue.claim_collection("worker-2")
    third = task_queue.claim_collection("worker-3")

    assert first is not None and first.organization_id == first_workspace.id
    assert second is not None and second.organization_id == second_workspace.id
    assert third is None


def test_worker_concurrency_scales_across_users_with_sqlite_safety_cap() -> None:
    assert resolved_worker_concurrency(
        "sqlite",
        12,
        configured_collection=0,
        configured_ai=0,
        cpu_count=16,
    ) == (4, 2)
    assert resolved_worker_concurrency(
        "postgresql",
        12,
        configured_collection=0,
        configured_ai=0,
        cpu_count=16,
    ) == (12, 16)
    assert resolved_worker_concurrency(
        "sqlite",
        12,
        configured_collection=3,
        configured_ai=3,
        cpu_count=16,
    ) == (3, 3)


def test_missing_token_never_owns_an_active_task_lease() -> None:
    task = CollectionTask(
        organization_id="workspace",
        created_by_id="user",
        name="租约测试",
        status="running",
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 2),
        privacy_confirmed=True,
        lease_token="active-token",
        lease_expires_at=utcnow() + timedelta(minutes=1),
    )
    assert not task_queue.owns_lease(task, None)
    assert task_queue.owns_lease(task, "active-token")


def test_paused_ai_work_resumes_in_ai_queue_not_collection_queue(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'resume.db').as_posix()}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        workspace = Organization(name="恢复测试")
        db.add(workspace)
        db.flush()
        user = User(email="resume@example.com", display_name="恢复", role="user")
        db.add(user)
        db.flush()
        task = _queued_task(workspace.id, user.id, "恢复AI", 1)
        task.status = "paused"
        task.classification_final_status = "review"
        db.add(task)
        db.flush()
        apply_task_action(db, task, "resume")
        assert task.status == "classifying"
        assert task.classification_final_status == "review"


def test_login_throttle_locks_username_and_ip_then_can_be_cleared(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'login.db').as_posix()}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        for _ in range(task_queue.get_settings().login_max_failures):
            record_login_failure(db, "alice", "203.0.113.4")
        db.commit()
        assert not login_allowed(db, "alice", "203.0.113.4").allowed
        assert db.scalar(select(LoginThrottle)) is not None
        clear_login_failures(db, "alice", "203.0.113.4")
        db.commit()
        assert login_allowed(db, "alice", "203.0.113.4").allowed


def test_export_cells_and_windows_names_are_safe() -> None:
    assert safe_csv_cell("=HYPERLINK(\"https://example.com\")").startswith("'")
    assert safe_csv_cell(" normal") == " normal"
    assert safe_name("CON") == "_CON"
