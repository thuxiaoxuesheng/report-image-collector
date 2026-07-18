from __future__ import annotations

import asyncio
import contextvars
import io
import logging
import os
import random
import re
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from PIL import Image
from playwright.async_api import Page
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .ai import classify_image
from .browser import browser_manager, is_allowed_url
from .config import get_settings
from .database import SessionLocal, engine
from .date_utils import SHANGHAI, parse_xhs_datetime
from .enums import OrganizationStatus, TaskStatus
from .models import (
    CollectedImage,
    CollectionTask,
    Note,
    Organization,
    SeenNote,
    TaskKeyword,
    as_utc,
    utcnow,
)
from .social_copilot import (
    SocialCopilotRestriction,
    SocialCopilotUnavailable,
    social_copilot,
)
from .task_queue import (
    TaskLease,
    claim_classification,
    claim_collection,
    clear_lease,
    owns_lease,
    recover_expired_leases,
    renew_lease,
    worker_identity,
)

logger = logging.getLogger(__name__)
settings = get_settings()
current_lease_token: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_task_lease_token", default=None
)
NOTE_ID_PATTERN = re.compile(r"/(?:explore|discovery/item)/([a-zA-Z0-9]+)")
ATTENTION_TOKENS = (
    "验证码",
    "请完成验证",
    "身份验证",
    "滑块验证",
    "拖动滑块",
    "访问频繁",
    "操作频繁",
    "网络环境存在风险",
    "IP存在风险",
    "安全限制",
    "请稍后再试",
)
LOGIN_TOKENS = ("登录后查看更多", "扫码登录", "手机号登录")


class AttentionRequired(RuntimeError):
    pass


class TaskStopped(RuntimeError):
    pass


class DailyLimitReached(RuntimeError):
    pass


def configured_limit_reached(current: int, configured_limit: int) -> bool:
    """Treat zero or a negative operator limit as unlimited."""
    return configured_limit > 0 and current >= configured_limit


async def human_pause(short: bool = True) -> None:
    # 保守串行节奏只用于控制负载与避免突发请求，不尝试伪装设备或绕过限制。
    bounds = (
        (settings.min_action_delay_seconds, settings.max_action_delay_seconds)
        if short
        else (settings.min_long_pause_seconds, settings.max_long_pause_seconds)
    )
    await asyncio.sleep(random.uniform(*bounds))


async def visible_body_text(page: Page) -> str:
    try:
        return (await page.locator("body").inner_text(timeout=8_000))[:60_000]
    except Exception:
        return ""


async def detect_page_block(page: Page) -> None:
    body = await visible_body_text(page)
    if any(token in body for token in LOGIN_TOKENS):
        raise AttentionRequired("小红书登录状态失效，请重新登录")
    for token in ATTENTION_TOKENS:
        if token in body:
            raise AttentionRequired(f"页面提示“{token}”，请打开登录窗口人工处理")


def extract_note_id(url: str) -> str | None:
    match = NOTE_ID_PATTERN.search(url)
    return match.group(1) if match else None


def extract_xsec_token(url: str) -> str | None:
    values = parse_qs(urlparse(url).query).get("xsec_token", [])
    return values[0] if values and values[0] else None


async def click_visible_text(page: Page, text: str) -> bool:
    locator = page.get_by_text(text, exact=True)
    for index in range(await locator.count()):
        candidate = locator.nth(index)
        try:
            if await candidate.is_visible(timeout=700):
                await candidate.click()
                return True
        except Exception:
            continue
    return False


async def hover_visible_text(page: Page, text: str) -> bool:
    locator = page.get_by_text(text, exact=True)
    for index in range(await locator.count()):
        candidate = locator.nth(index)
        try:
            if await candidate.is_visible(timeout=700):
                await candidate.hover()
                return True
        except Exception:
            continue
    return False


async def apply_search_filters(page: Page) -> None:
    """Use visible controls and require confirmation that latest sorting was selectable."""
    try:
        filter_opened = await hover_visible_text(page, "筛选")
        if not filter_opened:
            filter_opened = await click_visible_text(page, "筛选")
        if not filter_opened:
            raise AttentionRequired("无法打开搜索筛选，页面结构可能已经变化")
        await human_pause()
        latest_selected = await click_visible_text(page, "最新")
        if not latest_selected:
            latest_selected = await click_visible_text(page, "最新发布")
        if not latest_selected:
            raise AttentionRequired("无法确认“最新”排序，搜索页面结构可能已经变化")
        await human_pause()
        # 图文控件不是正确性的唯一保障，详情页还会再次排除视频。
        if await click_visible_text(page, "图文"):
            await human_pause()
    except AttentionRequired:
        raise
    except Exception as exc:
        raise AttentionRequired("无法设置搜索排序，搜索页面结构可能已经变化") from exc


async def collect_search_links(page: Page) -> list[str]:
    links = await page.locator("a[href*='/explore/'], a[href*='/discovery/item/']").evaluate_all(
        """els => els.map(a => a.href).filter(Boolean)"""
    )
    # 正常搜索页面自身保存了已经加载的卡片状态；与DOM链接合并可避免虚拟列表
    # 回收旧节点后漏掉已正常展示过的笔记。
    try:
        state_links = await page.evaluate(
            """() => {
              const raw = window.__INITIAL_STATE__?.search?.feeds;
              const feeds = raw?._rawValue ?? raw?.value ?? raw ?? [];
              if (!Array.isArray(feeds)) return [];
              return feeds.map(item => {
                const id = item?.id || item?.noteId;
                const token = item?.xsecToken || item?.xsec_token;
                if (!id) return null;
                const query = token
                  ? `?xsec_token=${encodeURIComponent(token)}&xsec_source=pc_search`
                  : "";
                return `https://www.xiaohongshu.com/explore/${id}${query}`;
              }).filter(Boolean);
            }"""
        )
        # 状态数据包含详情请求必需的 xsec_token，因此同一笔记优先使用状态链接。
        links = [*state_links, *links]
    except Exception:
        logger.debug("搜索页面状态不可用，使用可见链接")
    unique: list[str] = []
    seen: set[str] = set()
    for link in links:
        note_id = extract_note_id(link)
        if note_id and note_id not in seen:
            seen.add(note_id)
            unique.append(link)
    return unique


async def first_text(page: Page, selectors: list[str]) -> str:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() and await locator.is_visible(timeout=700):
                value = (await locator.inner_text()).strip()
                if value:
                    return value
        except Exception:
            continue
    return ""


async def extract_published_at(page: Page) -> datetime | None:
    selectors = [
        ".date",
        ".publish-time",
        ".bottom-container .date",
        "[class*='publish-time']",
        "[class*='date']",
    ]
    for selector in selectors:
        try:
            texts = await page.locator(selector).all_inner_texts()
            for text_value in texts[:10]:
                parsed = parse_xhs_datetime(text_value)
                if parsed:
                    return parsed
        except Exception:
            continue
    body = await visible_body_text(page)
    for line in body.splitlines():
        if any(marker in line for marker in ("发布于", "编辑于", "昨天", "天前", "小时前")):
            parsed = parse_xhs_datetime(line)
            if parsed:
                return parsed
    return None


async def extract_image_urls(page: Page) -> list[str]:
    await page.wait_for_timeout(1_500)
    values = await page.locator("img").evaluate_all(
        """els => els.map(img => ({
          src: img.currentSrc || img.src,
          w: img.naturalWidth || img.width,
          h: img.naturalHeight || img.height,
          cls: img.className || ''
        }))"""
    )
    urls: list[str] = []
    seen: set[str] = set()
    for item in values:
        src = str(item.get("src", ""))
        width = int(item.get("w") or 0)
        height = int(item.get("h") or 0)
        class_name = str(item.get("cls", "")).lower()
        if not src.startswith("http") or src in seen:
            continue
        if width < 300 or height < 300 or "avatar" in class_name:
            continue
        if not any(host in src for host in ("xhscdn", "xhsimg", "xiaohongshu")):
            continue
        seen.add(src)
        urls.append(src)
    return urls[:30]


async def extract_note_state(page: Page, note_id: str) -> dict | None:
    """Read the complete note data already delivered to the normal detail page."""
    try:
        return await page.evaluate(
            """noteId => {
              const state = window.__INITIAL_STATE__;
              let note = state?.noteData?.data?.noteData;
              if (!note) {
                const map = state?.note?.noteDetailMap;
                const rawMap = map?._rawValue ?? map?.value ?? map;
                note = rawMap?.[noteId]?.note;
              }
              note = note?._rawValue ?? note?.value ?? note;
              if (!note) return null;
              const images = Array.isArray(note.imageList) ? note.imageList : [];
              return {
                type: note.type || "",
                title: note.title || note.displayTitle || "",
                author: note.user?.nickname || note.user?.nickName || "",
                publishedTimestamp: note.time || note.publishTime || null,
                images: images.map(item =>
                  item?.urlDefault || item?.urlPre || item?.url || item?.infoList?.[0]?.url
                ).filter(Boolean),
              };
            }""",
            note_id,
        )
    except Exception:
        return None


async def parse_note(search_page: Page, url: str, organization_id: str) -> dict | None:
    note_id = extract_note_id(url)
    xsec_token = extract_xsec_token(url)
    if note_id and xsec_token:
        try:
            adapted = await social_copilot.fetch_note(
                search_page, organization_id, note_id, xsec_token
            )
            if adapted.get("type") == "video":
                return None
            if adapted.get("images"):
                return adapted
        except SocialCopilotRestriction as exc:
            raise AttentionRequired(f"小红书限制了笔记访问：{exc}") from exc
        except SocialCopilotUnavailable:
            # 扩展未连接或单篇详情不可用时，回退到正常详情页解析。
            pass

    context = search_page.context
    detail = await context.new_page()
    try:
        response = await detail.goto(url, wait_until="domcontentloaded", timeout=30_000)
        if response and (response.status in {403, 412, 429} or response.status >= 500):
            raise AttentionRequired(f"笔记页面返回 {response.status}，请稍后人工恢复")
        await human_pause()
        await detect_page_block(detail)
        state_note = await extract_note_state(detail, note_id or "")
        if state_note:
            if state_note.get("type") == "video":
                return None
            image_urls = [
                value.replace("http://", "https://", 1)
                for value in state_note.get("images", [])
                if isinstance(value, str) and value.startswith(("http://", "https://"))
            ]
            timestamp = state_note.get("publishedTimestamp")
            published_at = None
            if timestamp:
                numeric = float(timestamp)
                if numeric > 10_000_000_000:
                    numeric /= 1000
                published_at = datetime.fromtimestamp(numeric, tz=SHANGHAI)
            if image_urls:
                return {
                    "title": str(state_note.get("title", ""))[:500],
                    "author": str(state_note.get("author", ""))[:200],
                    "published_at": published_at,
                    "images": list(dict.fromkeys(image_urls))[:30],
                }
        if await detail.locator("video").count() > 0:
            return None
        published_at = await extract_published_at(detail)
        title = await first_text(detail, ["#detail-title", ".title", "[class*='note-title']"])
        author = await first_text(
            detail,
            [".author .name", ".username", "[class*='author'] [class*='name']"],
        )
        images = await extract_image_urls(detail)
        if not images:
            return None
        return {
            "title": title[:500],
            "author": author[:200],
            "published_at": published_at,
            "images": images,
        }
    finally:
        await detail.close()


def create_thumbnail(source: Path, target: Path) -> None:
    with Image.open(source) as image:
        thumb = image.convert("RGB")
        thumb.thumbnail((480, 480))
        target.parent.mkdir(parents=True, exist_ok=True)
        thumb.save(target, "JPEG", quality=78, optimize=True)


async def download_note_images(
    page: Page,
    user_id: str,
    task_id: str,
    note_id: str,
    urls: list[str],
) -> list[tuple[Path, Path]]:
    base = settings.data_dir / "users" / user_id / "tasks" / task_id / note_id
    base.mkdir(parents=True, exist_ok=True)
    downloaded: list[tuple[Path, Path]] = []
    format_suffixes = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "AVIF": ".avif"}
    for index, url in enumerate(urls, start=1):
        try:
            if not is_allowed_url(url) or urlparse(url).scheme != "https":
                logger.warning("跳过非小红书图片地址")
                continue
            response = await page.request.get(
                url, headers={"Referer": "https://www.xiaohongshu.com/"}
            )
            if not response.ok:
                continue
            data = await response.body()
            if len(data) < 5_000 or len(data) > 25 * 1024 * 1024:
                continue
            declared = response.headers.get("content-length")
            if declared and int(declared) != len(data):
                logger.warning("图片长度不完整，跳过：%s", url)
                continue
            # 解码校验真实格式，不仅依赖响应头或文件名。
            with Image.open(io.BytesIO(data)) as probe:
                probe.verify()
                suffix = format_suffixes.get((probe.format or "").upper())
            if not suffix:
                logger.warning("不支持的图片格式，跳过：%s", url)
                continue
            # The currently pinned MiniMax vision SDK accepts JPEG/PNG/WebP but
            # not AVIF. Normalize only AVIF, at high quality, before persistence.
            if suffix == ".avif":
                suffix = ".jpg"
                with Image.open(io.BytesIO(data)) as source:
                    converted = io.BytesIO()
                    source.convert("RGB").save(converted, "JPEG", quality=95, optimize=True)
                    data = converted.getvalue()
            target = base / f"{index:03d}{suffix}"
            temporary = base / f".{index:03d}{suffix}.part"
            temporary.write_bytes(data)
            temporary.replace(target)
            # 缩略图重新编码，不保留 EXIF 等元数据。
            thumb = base / "thumbs" / f"{index:03d}.jpg"
            create_thumbnail(target, thumb)
            downloaded.append((target, thumb))
        except Exception as exc:
            logger.warning("图片下载失败 %s: %s", url, exc)
            for temporary in base.glob(f".{index:03d}.*.part"):
                temporary.unlink(missing_ok=True)
        await human_pause()
    return downloaded


def task_control_state(task_id: str) -> str:
    with SessionLocal() as db:
        task = db.get(CollectionTask, task_id)
        return task.status if task else TaskStatus.CANCELLED


def ensure_task_running(task_id: str) -> None:
    with SessionLocal() as db:
        task = db.get(CollectionTask, task_id)
        if not task:
            raise TaskStopped("任务不存在")
        if not owns_lease(task, current_lease_token.get()):
            raise TaskStopped("任务执行租约已失效")
        if task.status not in {TaskStatus.RUNNING, TaskStatus.CLASSIFYING}:
            raise TaskStopped(f"任务已停止：{task.status}")
        organization = db.get(Organization, task.organization_id)
        expired = bool(
            organization
            and organization.authorization_expires_at
            and as_utc(organization.authorization_expires_at) <= utcnow()
        )
        if not organization or organization.status != OrganizationStatus.ACTIVE or expired:
            if organization and expired:
                organization.status = OrganizationStatus.EXPIRED
            task.status = TaskStatus.PAUSED
            task.attention_reason = "账号已停用或授权到期，任务已停止使用登录状态"
            task.progress_message = task.attention_reason
            db.commit()
            raise TaskStopped(task.attention_reason)


def already_seen(organization_id: str, platform_note_id: str) -> bool:
    with SessionLocal() as db:
        return (
            db.scalar(
                select(SeenNote.id).where(
                    SeenNote.organization_id == organization_id,
                    SeenNote.platform_note_id == platform_note_id,
                )
            )
            is not None
        )


def daily_new_note_count(organization_id: str) -> int:
    local_now = datetime.now(SHANGHAI)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_boundary = local_midnight.astimezone(UTC)
    with SessionLocal() as db:
        from sqlalchemy import func

        return (
            db.scalar(
                select(func.count(SeenNote.id)).where(
                    SeenNote.organization_id == organization_id,
                    SeenNote.first_seen_at >= utc_boundary,
                )
            )
            or 0
        )


def save_note(
    *,
    task_id: str,
    keyword_id: str,
    organization_id: str,
    keyword: str,
    platform_note_id: str,
    source_url: str,
    parsed: dict,
    paths: list[tuple[Path, Path]],
) -> bool:
    if not paths:
        return False
    with SessionLocal() as db:
        if db.scalar(
            select(SeenNote.id).where(
                SeenNote.organization_id == organization_id,
                SeenNote.platform_note_id == platform_note_id,
            )
        ):
            return False
        note = Note(
            task_id=task_id,
            keyword_id=keyword_id,
            platform_note_id=platform_note_id,
            title=parsed["title"],
            author=parsed["author"],
            source_url=source_url,
            published_at=parsed["published_at"],
        )
        db.add(note)
        db.flush()
        for ordinal, (local_path, thumbnail_path) in enumerate(paths, start=1):
            db.add(
                CollectedImage(
                    note_id=note.id,
                    local_path=str(local_path),
                    thumbnail_path=str(thumbnail_path),
                    ordinal=ordinal,
                )
            )
        db.add(
            SeenNote(
                organization_id=organization_id,
                platform_note_id=platform_note_id,
                first_keyword=keyword,
                published_at=parsed["published_at"],
            )
        )
        keyword_row = db.get(TaskKeyword, keyword_id)
        if keyword_row:
            keyword_row.collected_count += 1
        try:
            db.commit()
            return True
        except IntegrityError:
            db.rollback()
            return False


def update_progress(task_id: str, message: str) -> None:
    with SessionLocal() as db:
        task = db.get(CollectionTask, task_id)
        if task and owns_lease(task, current_lease_token.get()):
            task.progress_message = message[:500]
            db.commit()


async def collect_keyword(page: Page, task: CollectionTask, keyword: TaskKeyword) -> None:
    # 2026 web UI routes note searches through search_result_ai. The legacy
    # search_result route now redirects to the explore feed and has no sort filter.
    search_url = (
        "https://www.xiaohongshu.com/search_result_ai"
        f"?keyword={quote(quote(keyword.keyword, safe=''), safe='')}&source=web_explore_feed"
    )
    response = await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
    if response and (response.status in {403, 412, 429} or response.status >= 500):
        raise AttentionRequired(f"搜索页面返回 {response.status}，请稍后人工恢复")
    await human_pause(short=False)
    await detect_page_block(page)
    await apply_search_filters(page)

    processed: set[str] = set()
    stale_rounds = 0
    consecutive_old = 0
    parse_failures = 0
    start_boundary = datetime.combine(task.start_date, time.min, tzinfo=SHANGHAI)
    end_boundary = datetime.combine(task.end_date, time.max, tzinfo=SHANGHAI)

    scroll_rounds = 0
    while (
        keyword.collected_count < keyword.target_count
        and stale_rounds < 5
        and not configured_limit_reached(
            scroll_rounds, settings.max_scroll_rounds_per_keyword
        )
        and not configured_limit_reached(
            keyword.scanned_count, settings.max_scan_per_keyword
        )
    ):
        scroll_rounds += 1
        ensure_task_running(task.id)
        links = await collect_search_links(page)
        if not links and scroll_rounds >= 3:
            body = await visible_body_text(page)
            normal_empty = any(
                marker in body for marker in ("暂无相关结果", "没有搜索结果", "换个词试试")
            )
            if not normal_empty:
                raise AttentionRequired("搜索结果持续为空，页面结构或访问状态可能异常")
            return
        new_links = [link for link in links if (extract_note_id(link) or "") not in processed]
        if not new_links:
            stale_rounds += 1
        else:
            stale_rounds = 0

        for link in new_links:
            ensure_task_running(task.id)
            note_id = extract_note_id(link)
            if not note_id:
                continue
            processed.add(note_id)
            if already_seen(task.organization_id, note_id):
                continue
            with SessionLocal() as db:
                row = db.get(TaskKeyword, keyword.id)
                if row:
                    row.scanned_count += 1
                    keyword.scanned_count = row.scanned_count
                    keyword.collected_count = row.collected_count
                    db.commit()
            if settings.daily_new_note_limit > 0 and configured_limit_reached(
                daily_new_note_count(task.organization_id), settings.daily_new_note_limit
            ):
                raise DailyLimitReached(
                    f"已达到每日新增笔记上限 {settings.daily_new_note_limit} 篇，"
                    "已保存当前结果，请次日手动恢复"
                )
            try:
                parsed = await parse_note(page, link, task.created_by_id)
            except AttentionRequired:
                raise
            except Exception as exc:
                if "target page, context or browser has been closed" in str(exc).lower():
                    raise AttentionRequired(
                        "采集浏览器被关闭，请确认不要关闭Chrome窗口后恢复任务"
                    ) from exc
                logger.warning("解析笔记 %s 失败: %s", note_id, exc)
                parse_failures += 1
                if parse_failures >= 6:
                    raise AttentionRequired("连续多篇笔记无法解析，页面结构可能已经变化") from exc
                continue
            if not parsed:
                continue
            published_at = parsed["published_at"]
            if not published_at:
                continue
            published_at = published_at.astimezone(SHANGHAI)
            if published_at > end_boundary:
                continue
            if published_at < start_boundary:
                consecutive_old += 1
                if consecutive_old >= 5:
                    return
                continue
            consecutive_old = 0
            paths = await download_note_images(
                page,
                task.created_by_id,
                task.id,
                note_id,
                parsed["images"],
            )
            saved = False
            try:
                saved = save_note(
                    task_id=task.id,
                    keyword_id=keyword.id,
                    organization_id=task.organization_id,
                    keyword=keyword.keyword,
                    platform_note_id=note_id,
                    source_url=link,
                    parsed=parsed,
                    paths=paths,
                )
            finally:
                if not saved:
                    for original, thumbnail in paths:
                        original.unlink(missing_ok=True)
                        thumbnail.unlink(missing_ok=True)
            if saved:
                keyword.collected_count += 1
                update_progress(
                    task.id,
                    f"关键词“{keyword.keyword}”：已采集 "
                    f"{keyword.collected_count}/{keyword.target_count}",
                )
            if keyword.collected_count >= keyword.target_count:
                break
            await human_pause()

        if keyword.collected_count >= keyword.target_count:
            return
        await page.mouse.wheel(0, settings.browser_viewport_height * 2)
        await human_pause(short=False)
        await detect_page_block(page)


async def classify_task_images(task_id: str, keyword_id: str | None = None) -> dict[str, int]:
    with SessionLocal() as db:
        task = db.get(CollectionTask, task_id)
        if (
            not task
            or not task.ai_confirmed
            or not owns_lease(task, current_lease_token.get())
        ):
            return {"attempted": 0, "succeeded": 0, "failed": 0}
        note_scope = select(Note.id).where(Note.task_id == task_id)
        if keyword_id:
            note_scope = note_scope.where(Note.keyword_id == keyword_id)
        image_scope = select(CollectedImage.id).where(
            CollectedImage.note_id.in_(note_scope),
            CollectedImage.deleted_at.is_(None),
        )
        image_ids = list(db.scalars(image_scope))
        db.execute(
            update(CollectedImage)
            .where(CollectedImage.id.in_(image_scope))
            .values(ai_previous_selected=None)
        )
        organization_id = task.organization_id
        db.commit()
    succeeded = 0
    failed = 0
    for index, image_id in enumerate(image_ids, start=1):
        ensure_task_running(task_id)
        with SessionLocal() as db:
            image = db.get(CollectedImage, image_id)
            if not image or image.deleted_at:
                continue
            path = Path(image.local_path)
        try:
            result = await classify_image(path, organization_id)
        except Exception as exc:
            failed += 1
            logger.warning("AI报告单判断失败 %s: %s", image_id, exc)
            continue
        if result:
            ensure_task_running(task_id)
            with SessionLocal() as db:
                task = db.get(CollectionTask, task_id)
                image = db.get(CollectedImage, image_id)
                if (
                    task
                    and owns_lease(task, current_lease_token.get())
                    and image
                    and not image.deleted_at
                ):
                    image.ai_previous_selected = image.selected
                    image.ai_is_report = result["is_report"]
                    image.selected = result["is_report"]
                    image.ai_confidence = result["confidence"]
                    image.ai_reason = result["reason"]
                    image.ai_model = result["model"]
                    image.classified_at = utcnow()
                    db.commit()
                    succeeded += 1
        update_progress(task_id, f"AI报告单判断 {index}/{len(image_ids)}，成功 {succeeded}")
    return {"attempted": len(image_ids), "succeeded": succeeded, "failed": failed}


async def run_existing_classification(lease: TaskLease) -> None:
    task_id = lease.task_id
    token = current_lease_token.set(lease.token)
    try:
        with SessionLocal() as db:
            task = db.get(CollectionTask, task_id)
            if not task or not owns_lease(task, lease.token):
                return
            final_status = task.classification_final_status or TaskStatus.REVIEW
            keyword_id = task.classification_keyword_id
        stats = await classify_task_images(task_id, keyword_id)
        if stats["attempted"] and not stats["succeeded"]:
            raise RuntimeError("全部图片判断失败")
        set_task_status(
            task_id,
            final_status,
            f"AI判断完成：成功 {stats['succeeded']}，失败 {stats['failed']}，请人工复核",
        )
    except TaskStopped:
        pass
    except Exception as exc:
        logger.exception("AI报告单判断任务失败")
        with SessionLocal() as db:
            task = db.get(CollectionTask, task_id)
            if task and owns_lease(task, lease.token):
                task.status = TaskStatus.REVIEW
                task.attention_reason = f"AI判断异常：{type(exc).__name__}"
                task.progress_message = "AI判断未完成，原图仍可人工复核"
                task.finished_at = utcnow()
                task.review_expires_at = utcnow() + timedelta(
                    hours=settings.image_retention_hours
                )
                task.state_version += 1
                task.classification_keyword_id = None
                task.classification_final_status = None
                clear_lease(task)
                db.commit()
    finally:
        current_lease_token.reset(token)


def set_task_status(task_id: str, status: str, message: str | None = None) -> None:
    with SessionLocal() as db:
        task = db.get(CollectionTask, task_id)
        if not task:
            return
        if not owns_lease(task, current_lease_token.get()):
            return
        task.status = status
        task.state_version += 1
        if message:
            task.progress_message = message[:500]
        if status == TaskStatus.RUNNING and not task.started_at:
            task.started_at = utcnow()
        if status in {TaskStatus.REVIEW, TaskStatus.CANCELLED, TaskStatus.FAILED}:
            task.finished_at = utcnow()
            task.review_expires_at = utcnow() + timedelta(hours=settings.image_retention_hours)
        if status != TaskStatus.RUNNING:
            clear_lease(task)
        if status != TaskStatus.CLASSIFYING:
            task.classification_keyword_id = None
            task.classification_final_status = None
        db.commit()


async def run_task(lease: TaskLease) -> None:
    task_id = lease.task_id
    token = current_lease_token.set(lease.token)
    with SessionLocal() as db:
        task = db.get(CollectionTask, task_id)
        if not task or not owns_lease(task, lease.token):
            current_lease_token.reset(token)
            return
        # 载入关系数据后脱离 session，采集过程中使用短事务更新。
        keywords = list(task.keywords)
    worker_acquired = False
    try:
        ensure_task_running(task_id)
        try:
            if not lease.browser_token:
                raise RuntimeError("任务缺少浏览器租约")
            page = await browser_manager.worker_page(
                lease.user_id, lease.owner, lease.browser_token
            )
            worker_acquired = True
        except RuntimeError as exc:
            raise AttentionRequired(str(exc)) from exc
        if not await browser_manager.login_detected(lease.user_id):
            raise AttentionRequired("尚未登录小红书，请先完成登录")
        for keyword in keywords:
            if keyword.completed:
                continue
            update_progress(task_id, f"正在采集关键词“{keyword.keyword}”")
            with SessionLocal() as db:
                task_snapshot = db.get(CollectionTask, task_id)
                keyword_snapshot = db.get(TaskKeyword, keyword.id)
                if not task_snapshot or not keyword_snapshot:
                    raise TaskStopped("任务不存在")
                task_snapshot.current_keyword_id = keyword.id
                db.commit()
                await collect_keyword(page, task_snapshot, keyword_snapshot)
            with SessionLocal() as db:
                row = db.get(TaskKeyword, keyword.id)
                if row:
                    row.completed = True
                    db.commit()
        ensure_task_running(task_id)
        with SessionLocal() as db:
            task = db.get(CollectionTask, task_id)
            ai_confirmed = bool(task and task.ai_confirmed)
        if ai_confirmed:
            with SessionLocal() as db:
                task = db.get(CollectionTask, task_id)
                if task and owns_lease(task, lease.token):
                    task.classification_keyword_id = None
                    task.classification_final_status = TaskStatus.REVIEW
                    db.commit()
            set_task_status(task_id, TaskStatus.CLASSIFYING, "采集完成，等待AI筛选执行器")
        else:
            set_task_status(task_id, TaskStatus.REVIEW, "采集完成，等待人工审核")
    except AttentionRequired as exc:
        with SessionLocal() as db:
            task = db.get(CollectionTask, task_id)
            if task and owns_lease(task, lease.token):
                task.status = TaskStatus.NEEDS_ATTENTION
                task.attention_reason = str(exc)
                task.progress_message = str(exc)
                task.state_version += 1
                clear_lease(task)
                db.commit()
    except DailyLimitReached as exc:
        with SessionLocal() as db:
            task = db.get(CollectionTask, task_id)
            if task and owns_lease(task, lease.token):
                task.status = TaskStatus.PAUSED
                task.attention_reason = str(exc)
                task.progress_message = str(exc)
                task.review_expires_at = utcnow() + timedelta(hours=settings.image_retention_hours)
                task.state_version += 1
                clear_lease(task)
                db.commit()
    except TaskStopped:
        pass
    except Exception as exc:
        logger.exception("任务执行失败")
        with SessionLocal() as db:
            task = db.get(CollectionTask, task_id)
            if task and owns_lease(task, lease.token):
                task.status = TaskStatus.FAILED
                task.attention_reason = f"任务异常：{type(exc).__name__}"
                task.progress_message = "任务执行失败，请查看日志"
                task.finished_at = utcnow()
                task.review_expires_at = utcnow() + timedelta(hours=settings.image_retention_hours)
                task.state_version += 1
                clear_lease(task)
                db.commit()
    finally:
        if worker_acquired:
            await browser_manager.worker_done(lease.user_id)
        current_lease_token.reset(token)


def resolved_worker_concurrency(
    dialect_name: str,
    browser_slots: int,
    *,
    configured_collection: int,
    configured_ai: int,
    cpu_count: int,
) -> tuple[int, int]:
    collection_count = configured_collection
    if collection_count <= 0:
        # SQLite is safe here because embedded workers share one event loop,
        # claims are synchronous/atomic within that process, WAL serializes
        # the brief writes, and browser leases still serialize each user.
        # Keep a conservative cap for the single-file database; PostgreSQL
        # can use every resource-derived browser slot.
        collection_count = browser_slots
        if dialect_name == "sqlite":
            collection_count = min(4, collection_count)

    ai_count = configured_ai
    if ai_count <= 0:
        ai_count = max(1, min(16, cpu_count))
        if dialect_name == "sqlite":
            ai_count = min(2, ai_count)
    elif dialect_name == "sqlite":
        ai_count = min(4, ai_count)
    return max(1, collection_count), max(1, ai_count)


class TaskWorker:
    def __init__(self) -> None:
        self._loops: list[asyncio.Task] = []
        self._stopping = False

    async def start(self, *, force: bool = False) -> None:
        self._stopping = False
        if self._loops:
            return
        if not settings.embedded_workers and not force:
            return
        recover_expired_leases()
        collection_count, ai_count = resolved_worker_concurrency(
            engine.dialect.name,
            browser_manager.capacity.max_slots,
            configured_collection=settings.collection_worker_concurrency,
            configured_ai=settings.ai_worker_concurrency,
            cpu_count=os.cpu_count() or 2,
        )
        for slot in range(collection_count):
            self._loops.append(
                asyncio.create_task(
                    self._collection_loop(slot), name=f"collection-worker-{slot}"
                )
            )
        for slot in range(ai_count):
            self._loops.append(
                asyncio.create_task(self._ai_loop(slot), name=f"ai-worker-{slot}")
            )

    async def stop(self) -> None:
        self._stopping = True
        for loop in self._loops:
            loop.cancel()
        if self._loops:
            await asyncio.gather(*self._loops, return_exceptions=True)
        self._loops.clear()
        await browser_manager.close()

    async def _heartbeat(self, lease: TaskLease) -> None:
        while not self._stopping:
            await asyncio.sleep(settings.worker_heartbeat_seconds)
            if not renew_lease(lease):
                return

    async def _run_leased(self, lease: TaskLease, operation) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(lease))
        try:
            await operation(lease)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _collection_loop(self, slot: int) -> None:
        owner = worker_identity("collection", slot)
        while not self._stopping:
            try:
                lease = claim_collection(owner)
                if lease:
                    await self._run_leased(lease, run_task)
                else:
                    recover_expired_leases()
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("采集任务队列轮询异常")
                await asyncio.sleep(5)

    async def _ai_loop(self, slot: int) -> None:
        owner = worker_identity("ai", slot)
        while not self._stopping:
            try:
                lease = claim_classification(owner)
                if lease:
                    await self._run_leased(lease, run_existing_classification)
                else:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("AI任务队列轮询异常")
                await asyncio.sleep(5)


task_worker = TaskWorker()
