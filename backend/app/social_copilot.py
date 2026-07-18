from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

try:
    import psutil
except ImportError:  # The bridge still works; only stale-process cleanup is skipped.
    psutil = None

from .config import get_settings
from .date_utils import SHANGHAI

XHS_FEED_URL = "https://edith.xiaohongshu.com/api/sns/web/v1/feed"


class SocialCopilotUnavailable(RuntimeError):
    """The optional local adapter is not ready; the caller may use the page fallback."""


class SocialCopilotRestriction(RuntimeError):
    """XHS explicitly rejected the request; collection must pause."""


def _first(mapping: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = mapping.get(name)
        if value not in (None, ""):
            return value
    return None


def normalize_note_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize the GPL adapter response without retaining its raw response."""
    body = payload.get("body", payload)
    if not isinstance(body, dict):
        return None
    success = body.get("success")
    code = body.get("code")
    if success is False or (code not in (None, 0, "0")):
        message = str(_first(body, "msg", "message") or "小红书未返回笔记详情")
        if any(token in message for token in ("风控", "频繁", "验证", "风险", "限制")):
            raise SocialCopilotRestriction(message)
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return None
    item = items[0] if isinstance(items[0], dict) else {}
    note = _first(item, "note_card", "noteCard")
    if not isinstance(note, dict):
        return None

    user = note.get("user") if isinstance(note.get("user"), dict) else {}
    image_list = _first(note, "image_list", "imageList") or []
    images: list[str] = []
    for image in image_list if isinstance(image_list, list) else []:
        if not isinstance(image, dict):
            continue
        url = _first(image, "url_default", "urlDefault", "url_pre", "urlPre", "url")
        if not url:
            info_list = _first(image, "info_list", "infoList") or []
            if isinstance(info_list, list):
                for info in info_list:
                    if isinstance(info, dict) and info.get("url"):
                        url = info["url"]
                        break
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            images.append(url.replace("http://", "https://", 1))

    timestamp = _first(note, "time", "publish_time", "publishTime")
    published_at = None
    if timestamp not in (None, ""):
        try:
            numeric = float(timestamp)
            if numeric > 10_000_000_000:
                numeric /= 1000
            published_at = datetime.fromtimestamp(numeric, tz=SHANGHAI)
        except (TypeError, ValueError, OSError):
            published_at = None

    return {
        "type": str(note.get("type") or ""),
        "title": str(_first(note, "title", "display_title", "displayTitle") or "")[:500],
        "author": str(_first(user, "nickname", "nick_name", "nickName") or "")[:200],
        "published_at": published_at,
        "images": list(dict.fromkeys(images))[:30],
    }


class SocialCopilotBridge:
    """Own the loopback-only Node bridge used by the vendored browser extension."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._process: asyncio.subprocess.Process | None = None

    @property
    def bridge_secret(self) -> str:
        configured = self.settings.social_copilot_bridge_secret
        if configured:
            return configured
        source = self.settings.secrets_master_key or self.settings.site_auth_session_secret
        if not source:
            source = "xhs-collector-development-bridge"
        return hashlib.sha256(f"social-copilot:{source}".encode()).hexdigest()

    @property
    def extension_dir(self) -> Path:
        return self.settings.social_copilot_dir.resolve() / "output" / "chrome-mv3"

    @property
    def enabled(self) -> bool:
        manifest_exists = (self.extension_dir / "manifest.json").is_file()
        return self.settings.social_copilot_enabled and manifest_exists

    async def _healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
                response = await client.get(
                    f"{self.settings.social_copilot_url.rstrip('/')}/health",
                    headers={"Authorization": f"Bearer {self.bridge_secret}"},
                )
            return response.status_code == 200 and response.json() == {"ok": True}
        except (httpx.HTTPError, ValueError):
            return False

    async def start(self) -> None:
        if not self.enabled or await self._healthy():
            return
        server_dir = self.settings.social_copilot_dir.resolve() / "server"
        entrypoint = server_dir / "index.js"
        if not entrypoint.is_file():
            return
        await self._stop_stale_local_bridge(entrypoint)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        environment = os.environ.copy()
        environment["BRIDGE_SECRET"] = self.bridge_secret
        self._process = await asyncio.create_subprocess_exec(
            "node",
            str(entrypoint),
            cwd=str(server_dir),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=creationflags,
            env=environment,
        )
        for _ in range(30):
            if await self._healthy():
                return
            if self._process.returncode is not None:
                break
            await asyncio.sleep(0.2)
        await self.stop()

    async def _stop_stale_local_bridge(self, entrypoint: Path) -> None:
        """Terminate only an orphaned bridge launched from this exact checkout."""
        if psutil is None:
            return
        parsed = urlparse(self.settings.social_copilot_url)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return
        port = parsed.port or 80

        def stop_matching_process() -> None:
            try:
                connections = psutil.net_connections(kind="tcp")
            except psutil.Error:
                return
            for connection in connections:
                if (
                    connection.status != psutil.CONN_LISTEN
                    or not connection.laddr
                    or connection.laddr.port != port
                    or not connection.pid
                    or connection.pid == os.getpid()
                ):
                    continue
                try:
                    process = psutil.Process(connection.pid)
                    arguments = process.cmdline()
                    matches = any(
                        Path(argument).resolve() == entrypoint
                        for argument in arguments
                        if argument.lower().endswith("index.js")
                    )
                    if not matches:
                        return
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except psutil.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
                    return
                except (OSError, psutil.Error):
                    return

        await asyncio.to_thread(stop_matching_process)

    async def stop(self) -> None:
        if not self._process:
            return
        if self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=3)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
        self._process = None

    async def fetch_note(
        self, page, user_id: str, note_id: str, xsec_token: str
    ) -> dict[str, Any]:
        if not self.enabled or not await self._healthy():
            raise SocialCopilotUnavailable("本地详情适配器未启动")
        await page.bring_to_front()
        request_data = {
            "url": XHS_FEED_URL,
            "method": "POST",
            "data": {
                "source_note_id": note_id,
                "image_formats": ["jpg", "webp", "avif"],
                "extra": {"need_body_topic": "1"},
                "xsec_source": "pc_search",
                "xsec_token": xsec_token,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                response = await client.post(
                    f"{self.settings.social_copilot_url.rstrip('/')}/request",
                    json={"workspace_id": user_id, "request": request_data},
                    headers={"Authorization": f"Bearer {self.bridge_secret}"},
                )
        except httpx.HTTPError as exc:
            raise SocialCopilotUnavailable("本地详情适配器连接失败") from exc
        if response.status_code in {403, 429}:
            raise SocialCopilotRestriction(f"小红书详情请求返回 {response.status_code}")
        if response.status_code in {412, 500, 502, 503, 504}:
            raise SocialCopilotUnavailable("本地详情适配器尚未连接浏览器")
        if response.status_code != 200:
            raise SocialCopilotUnavailable(f"本地详情适配器返回 {response.status_code}")
        try:
            normalized = normalize_note_payload(response.json())
        except (ValueError, TypeError) as exc:
            raise SocialCopilotUnavailable("本地详情适配器响应无效") from exc
        if not normalized:
            raise SocialCopilotUnavailable("笔记详情不可用")
        return normalized


social_copilot = SocialCopilotBridge()
