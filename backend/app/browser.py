from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

try:
    import psutil
except ImportError:  # setup installs it in production; tests can still use an explicit limit.
    psutil = None

from .browser_leases import (
    acquire_browser_lease,
    release_browser_lease,
    renew_browser_lease,
)
from .config import get_settings
from .social_copilot import social_copilot

logger = logging.getLogger(__name__)

ALLOWED_HOST_SUFFIXES = (
    "xiaohongshu.com",
    "xhscdn.com",
    "xhscdn.net",
    "xhsimg.com",
)
ALLOWED_KEYS = {
    "Backspace",
    "Delete",
    "Enter",
    "Escape",
    "Tab",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "Home",
    "End",
    "PageUp",
    "PageDown",
}


def is_allowed_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme == "about":
        return url == "about:blank"
    if parsed.scheme == "chrome-extension":
        return True
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in ALLOWED_HOST_SUFFIXES)


class BrowserCapacity:
    def __init__(self) -> None:
        settings = get_settings()
        if settings.browser_max_concurrency > 0:
            slots = settings.browser_max_concurrency
        else:
            available_mb = (
                psutil.virtual_memory().available // (1024 * 1024) if psutil else 4096
            )
            memory_slots = max(1, (available_mb - 2048) // settings.browser_memory_budget_mb)
            cpu_slots = max(1, ((psutil.cpu_count(logical=True) if psutil else None) or 2) * 2)
            slots = min(128, memory_slots, cpu_slots)
        self.max_slots = max(1, int(slots))
        self._semaphore = asyncio.Semaphore(self.max_slots)

    async def acquire(self) -> None:
        await self._semaphore.acquire()

    def release(self) -> None:
        self._semaphore.release()


class BrowserRuntime:
    """One isolated browser context and state machine for one user workspace."""

    def __init__(self, manager: BrowserManager, user_id: str) -> None:
        self.manager = manager
        self.settings = manager.settings
        self.user_id = user_id
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._bridge_page: Page | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._mode_lock = asyncio.Lock()
        self._capacity_acquired = False
        self.remote_active = False
        self.worker_busy = False
        self.login_state = False
        self._lease_owner: str | None = None
        self._lease_token: str | None = None
        self._lease_heartbeat: asyncio.Task | None = None
        self._poisoned_close_task: asyncio.Task | None = None

    @property
    def profile_path(self) -> Path:
        path = (
            self.settings.data_dir
            / "users"
            / self.user_id
            / "browser-profile"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    async def ensure_started(self) -> Page:
        async with self._lifecycle_lock:
            if self._poisoned_close_task:
                if self._poisoned_close_task.done():
                    self._finish_poisoned_close(self._poisoned_close_task)
                else:
                    raise RuntimeError(
                        "浏览器未能完全关闭，暂时不能重新打开；"
                        "请稍后重试，持续出现时请重启采集服务"
                    )
            if self._context and self._page and not self._page.is_closed():
                return self._page
            if self._context:
                # A site can close or replace its page while the persistent
                # context remains healthy. Reuse that context and its existing
                # capacity slot instead of acquiring a second slot and trying
                # to launch the same profile twice.
                try:
                    self._page = next(
                        (
                            page
                            for page in self._context.pages
                            if not page.is_closed()
                            and (urlparse(page.url).hostname or "").endswith(
                                "xiaohongshu.com"
                            )
                        ),
                        None,
                    )
                    if not self._page:
                        self._page = await self._context.new_page()
                    return self._page
                except asyncio.CancelledError:
                    await asyncio.shield(self._close_unlocked())
                    raise
                except Exception:
                    # The context itself has gone away unexpectedly. Fully
                    # release its slot/lease before attempting a fresh launch.
                    await self._close_unlocked()
            await self.manager.capacity.acquire()
            self._capacity_acquired = True
            try:
                playwright = await self.manager.playwright()
                launch_options = dict(
                    user_data_dir=str(self.profile_path),
                    headless=self.settings.browser_headless,
                    viewport={
                        "width": self.settings.browser_viewport_width,
                        "height": self.settings.browser_viewport_height,
                    },
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    accept_downloads=False,
                    args=[
                        "--disable-dev-shm-usage",
                        "--disable-features=Translate,AutofillServerCommunication",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                )
                if social_copilot.enabled:
                    extension_dir = str(social_copilot.extension_dir)
                    launch_options["ignore_default_args"] = ["--disable-extensions"]
                    launch_options["args"].extend(
                        [
                            f"--disable-extensions-except={extension_dir}",
                            f"--load-extension={extension_dir}",
                        ]
                    )
                elif self.settings.browser_channel:
                    launch_options["channel"] = self.settings.browser_channel
                try:
                    self._context = await asyncio.wait_for(
                        playwright.chromium.launch_persistent_context(**launch_options),
                        timeout=self.settings.browser_start_timeout_seconds,
                    )
                except Exception as exc:
                    message = str(exc).lower()
                    channel_missing = any(
                        marker in message
                        for marker in (
                            "executable doesn't exist",
                            "not found",
                            "distribution 'chrome'",
                        )
                    )
                    if not self.settings.browser_channel or not channel_missing:
                        raise
                    launch_options.pop("channel", None)
                    self._context = await asyncio.wait_for(
                        playwright.chromium.launch_persistent_context(**launch_options),
                        timeout=self.settings.browser_start_timeout_seconds,
                    )
                self._context.set_default_timeout(15_000)
                await self._context.route("**/*", self._route_request)
                self._context.on(
                    "page", lambda page: asyncio.create_task(self._handle_new_page(page))
                )
                self._page = next(
                    (
                        page
                        for page in self._context.pages
                        if (urlparse(page.url).hostname or "").endswith("xiaohongshu.com")
                    ),
                    None,
                )
                if not self._page:
                    self._page = await self._context.new_page()
                if social_copilot.enabled:
                    await self._open_bridge_page()
                return self._page
            except asyncio.CancelledError:
                # Client disconnects cancel the request task. Always return the
                # in-memory capacity slot and persistent-profile lease before
                # propagating cancellation, otherwise every later open waits
                # forever even though no Chromium process exists.
                await asyncio.shield(self._close_unlocked())
                raise
            except Exception:
                await self._close_unlocked()
                raise

    async def _open_bridge_page(self) -> None:
        if not self._context or (self._bridge_page and not self._bridge_page.is_closed()):
            return
        worker = next(
            (
                candidate
                for candidate in self._context.service_workers
                if candidate.url.startswith("chrome-extension://")
            ),
            None,
        )
        if not worker:
            try:
                worker = await self._context.wait_for_event("serviceworker", timeout=8_000)
            except Exception:
                return
        extension_id = urlparse(worker.url).hostname
        if not extension_id:
            return
        self._bridge_page = await self._context.new_page()
        workspace = quote(self.user_id, safe="")
        token = quote(social_copilot.bridge_secret, safe="")
        await self._bridge_page.goto(
            f"chrome-extension://{extension_id}/sidepanel.html"
            f"?workspace={workspace}&token={token}",
            wait_until="domcontentloaded",
        )
        await self._bridge_page.wait_for_timeout(1_500)

    async def _route_request(self, route) -> None:
        request = route.request
        if request.is_navigation_request() and not is_allowed_url(request.url):
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _handle_new_page(self, page: Page) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8_000)
            if not is_allowed_url(page.url):
                await page.close()
        except Exception:
            if not page.is_closed() and not is_allowed_url(page.url):
                await page.close()

    async def open_remote(self) -> None:
        async with self._mode_lock:
            if self.worker_busy:
                raise RuntimeError("该账号采集任务运行中，暂时不能打开登录窗口")
            token = acquire_browser_lease(
                self.user_id, self.manager.owner, "remote"
            )
            if not token:
                raise RuntimeError("该用户的浏览器正在其他执行器中使用")
            self._lease_owner = self.manager.owner
            self._lease_token = token
            self.remote_active = True
            self._start_lease_heartbeat()
        try:
            page = await self.ensure_started()
            if page.url == "about:blank" or not is_allowed_url(page.url):
                await page.goto("https://www.xiaohongshu.com/", wait_until="domcontentloaded")
        except asyncio.CancelledError:
            async with self._mode_lock:
                self.remote_active = False
            await asyncio.shield(self.close())
            raise
        except Exception as exc:
            async with self._mode_lock:
                self.remote_active = False
            await self.close()
            logger.exception("Unable to open browser for user %s", self.user_id)
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(
                "浏览器启动失败，请确认服务器桌面保持登录状态后重试"
            ) from exc

    async def close_remote(self) -> None:
        async with self._mode_lock:
            if self.worker_busy:
                raise RuntimeError("采集任务正在使用该账号浏览器")
            self.remote_active = False
        if self._context:
            try:
                self.login_state = await self.login_detected()
            except Exception:
                pass
        await self.close()

    async def status(self) -> dict:
        page = self._page if self._context else None
        login_detected = self.login_state
        if page and not page.is_closed():
            try:
                login_detected = await self.login_detected()
                title = await page.title()
            except Exception:
                title = None
        else:
            title = None
        return {
            "running": self._context is not None,
            "url": page.url if page and not page.is_closed() else None,
            "title": title,
            "login_detected": login_detected,
            "busy": self.worker_busy,
            "remote_active": self.remote_active,
            "close_blocked": bool(
                self._poisoned_close_task and not self._poisoned_close_task.done()
            ),
        }

    async def login_detected(self) -> bool:
        page = await self.ensure_started()
        assert self._context
        cookies = await self._context.cookies("https://www.xiaohongshu.com")
        cookie_names = {cookie["name"] for cookie in cookies if cookie.get("value")}
        login_prompt_visible = False
        for selector in (
            "input[placeholder*='手机号']",
            "input[placeholder*='验证码']",
        ):
            try:
                locator = page.locator(selector)
                for index in range(await locator.count()):
                    if await locator.nth(index).is_visible(timeout=300):
                        login_prompt_visible = True
                        break
            except Exception:
                continue
            if login_prompt_visible:
                break
        self.login_state = "web_session" in cookie_names and not login_prompt_visible
        return self.login_state

    def ensure_remote_control(self) -> None:
        if not self.remote_active:
            raise RuntimeError("远程浏览器未开启")
        if self.worker_busy:
            raise RuntimeError("采集任务正在使用浏览器")

    async def screenshot(self) -> bytes:
        self.ensure_remote_control()
        page = await self.ensure_started()
        return await page.screenshot(type="jpeg", quality=72, animations="disabled")

    async def click(self, x: float, y: float) -> None:
        self.ensure_remote_control()
        page = await self.ensure_started()
        await page.mouse.click(
            x * self.settings.browser_viewport_width,
            y * self.settings.browser_viewport_height,
        )

    async def type_text(self, text: str) -> None:
        self.ensure_remote_control()
        page = await self.ensure_started()
        await page.keyboard.insert_text(text)

    async def press_key(self, key: str) -> None:
        self.ensure_remote_control()
        if key not in ALLOWED_KEYS:
            raise ValueError("不允许的按键")
        page = await self.ensure_started()
        await page.keyboard.press(key)

    async def scroll(self, delta_y: int) -> None:
        self.ensure_remote_control()
        page = await self.ensure_started()
        await page.mouse.wheel(0, delta_y)

    async def worker_page(self, owner: str, browser_token: str) -> Page:
        async with self._mode_lock:
            if self.remote_active:
                raise RuntimeError("用户正在操作该账号登录窗口")
            if self.worker_busy:
                raise RuntimeError("该账号已有采集任务正在运行")
            if not renew_browser_lease(self.user_id, owner, browser_token):
                raise RuntimeError("浏览器执行租约已失效，任务将重新排队")
            self._lease_owner = owner
            self._lease_token = browser_token
            self.worker_busy = True
            self._start_lease_heartbeat()
        try:
            return await self.ensure_started()
        except asyncio.CancelledError:
            async with self._mode_lock:
                self.worker_busy = False
            await asyncio.shield(self.close())
            raise
        except Exception:
            async with self._mode_lock:
                self.worker_busy = False
            await self.close()
            raise

    async def worker_done(self) -> None:
        async with self._mode_lock:
            if not self.worker_busy:
                return
            self.worker_busy = False
        if self._context:
            try:
                self.login_state = await self.login_detected()
            except Exception:
                pass
        await self.close()

    async def clear_profile(self) -> None:
        async with self._mode_lock:
            if self.worker_busy:
                raise RuntimeError("该账号采集任务正在使用浏览器")
            self.remote_active = False
        await self.close()
        token = acquire_browser_lease(
            self.user_id,
            self.manager.owner,
            "clear",
            allow_inactive=True,
        )
        if not token:
            raise RuntimeError("该用户的登录状态正在其他执行器中使用，暂时不能清除")
        try:
            if self.profile_path.exists():
                await asyncio.to_thread(shutil.rmtree, self.profile_path)
            self.login_state = False
        finally:
            release_browser_lease(self.user_id, self.manager.owner, token)

    async def _close_unlocked(self) -> None:
        cancellation: asyncio.CancelledError | None = None
        context = self._context
        stuck_close = False
        try:
            if context:
                close_task = asyncio.create_task(
                    context.close(), name=f"close-browser-{self.user_id}"
                )
                deadline = (
                    asyncio.get_running_loop().time()
                    + self.settings.browser_close_timeout_seconds
                )
                while not close_task.done():
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        close_task.cancel()
                        stuck_close = True
                        self._register_poisoned_close(close_task)
                        logger.error("Browser close timed out for user %s", self.user_id)
                        _done, pending = await asyncio.wait({close_task}, timeout=1)
                        if pending:
                            logger.critical(
                                "Browser close task did not accept cancellation for user %s",
                                self.user_id,
                            )
                        break
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(close_task), timeout=remaining
                        )
                    except asyncio.CancelledError as exc:
                        # Finish releasing the profile, capacity slot and DB
                        # lease before propagating the caller's cancellation.
                        cancellation = exc
                    except TimeoutError:
                        close_task.cancel()
                        stuck_close = True
                        self._register_poisoned_close(close_task)
                        logger.error("Browser close timed out for user %s", self.user_id)
                        _done, pending = await asyncio.wait({close_task}, timeout=1)
                        if pending:
                            logger.critical(
                                "Browser close task did not accept cancellation for user %s",
                                self.user_id,
                            )
                        break
                    except Exception:
                        break
                if close_task.done() and not close_task.cancelled():
                    try:
                        close_task.result()
                    except Exception:
                        # Chromium may already have crashed or closed its pipe.
                        pass
        finally:
            # These operations are deliberately synchronous and live in the
            # finally block so a second cancellation cannot leak accounting.
            self._context = None
            self._page = None
            self._bridge_page = None
            # A close coroutine that ignored cancellation may still own a live
            # Chromium process and this exact user-data-dir. Keep its capacity
            # slot and distributed lease until the task really finishes so no
            # second browser can corrupt the same profile.
            if not stuck_close and not self._poisoned_close_task:
                self._release_accounting()
        if cancellation:
            raise cancellation

    def _register_poisoned_close(self, close_task: asyncio.Task) -> None:
        self._poisoned_close_task = close_task
        tracker = getattr(self.manager, "track_orphan", None)
        if tracker:
            tracker(close_task)
        close_task.add_done_callback(self._finish_poisoned_close)

    def _finish_poisoned_close(self, close_task: asyncio.Task) -> None:
        if self._poisoned_close_task is not close_task:
            return
        if not close_task.cancelled():
            try:
                close_error = close_task.exception()
            except asyncio.CancelledError:
                close_error = None
            if close_error:
                logger.warning(
                    "Delayed browser close failed for user %s: %s",
                    self.user_id,
                    close_error,
                )
        self._poisoned_close_task = None
        self._release_accounting()

    def _release_accounting(self) -> None:
        if self._capacity_acquired:
            self.manager.capacity.release()
            self._capacity_acquired = False
        if self._lease_heartbeat:
            self._lease_heartbeat.cancel()
            self._lease_heartbeat = None
        if self._lease_owner and self._lease_token:
            release_browser_lease(self.user_id, self._lease_owner, self._lease_token)
        self._lease_owner = None
        self._lease_token = None

    def _start_lease_heartbeat(self) -> None:
        if self._lease_heartbeat and not self._lease_heartbeat.done():
            return
        self._lease_heartbeat = asyncio.create_task(
            self._lease_heartbeat_loop(),
            name=f"browser-lease-{self.user_id}",
        )

    async def _lease_heartbeat_loop(self) -> None:
        while self._lease_owner and self._lease_token:
            await asyncio.sleep(self.settings.browser_lease_heartbeat_seconds)
            if not renew_browser_lease(
                self.user_id, self._lease_owner, self._lease_token
            ):
                self.remote_active = False
                self.worker_busy = False
                # Close in a separate task: close() cancels the heartbeat task itself.
                asyncio.create_task(self.close(), name="close-expired-browser-lease")
                return

    async def close(self) -> None:
        async with self._lifecycle_lock:
            await self._close_unlocked()


class BrowserManager:
    """Registry of isolated per-user browser runtimes."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.capacity = BrowserCapacity()
        self.owner = (
            f"{socket.gethostname()}:{os.getpid()}:web:{uuid.uuid4().hex[:8]}"
        )
        self._runtimes: dict[str, BrowserRuntime] = {}
        self._orphan_tasks: set[asyncio.Task] = set()
        self._playwright: Playwright | None = None
        self._playwright_lock = asyncio.Lock()

    async def playwright(self) -> Playwright:
        async with self._playwright_lock:
            if not self._playwright:
                self._playwright = await asyncio.wait_for(
                    async_playwright().start(),
                    timeout=self.settings.browser_start_timeout_seconds,
                )
            return self._playwright

    def runtime(self, user_id: str) -> BrowserRuntime:
        runtime = self._runtimes.get(user_id)
        if not runtime:
            runtime = BrowserRuntime(self, user_id)
            self._runtimes[user_id] = runtime
        return runtime

    def track_orphan(self, task: asyncio.Task) -> None:
        self._orphan_tasks.add(task)
        task.add_done_callback(self._orphan_tasks.discard)

    def profile_path(self, user_id: str) -> Path:
        return self.runtime(user_id).profile_path

    def is_remote_active(self, user_id: str) -> bool:
        runtime = self._runtimes.get(user_id)
        return bool(runtime and runtime.remote_active)

    async def open_remote(self, user_id: str) -> None:
        await self.runtime(user_id).open_remote()

    async def close_remote(self, user_id: str) -> None:
        await self.runtime(user_id).close_remote()

    async def status(self, user_id: str) -> dict:
        return await self.runtime(user_id).status()

    async def login_detected(self, user_id: str) -> bool:
        return await self.runtime(user_id).login_detected()

    async def screenshot(self, user_id: str) -> bytes:
        return await self.runtime(user_id).screenshot()

    async def click(self, user_id: str, x: float, y: float) -> None:
        await self.runtime(user_id).click(x, y)

    async def type_text(self, user_id: str, text: str) -> None:
        await self.runtime(user_id).type_text(text)

    async def press_key(self, user_id: str, key: str) -> None:
        await self.runtime(user_id).press_key(key)

    async def scroll(self, user_id: str, delta_y: int) -> None:
        await self.runtime(user_id).scroll(delta_y)

    async def worker_page(self, user_id: str, owner: str, browser_token: str) -> Page:
        return await self.runtime(user_id).worker_page(owner, browser_token)

    async def worker_done(self, user_id: str) -> None:
        await self.runtime(user_id).worker_done()

    async def clear_profile(self, user_id: str) -> None:
        await self.runtime(user_id).clear_profile()

    async def close(self) -> None:
        await asyncio.gather(
            *(runtime.close() for runtime in list(self._runtimes.values())),
            return_exceptions=True,
        )
        async with self._playwright_lock:
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None


browser_manager = BrowserManager()
