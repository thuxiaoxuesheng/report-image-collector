from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import SessionLocal
from .models import ModelConfiguration, User
from .secrets import decrypt_secret

REPORT_PROMPT = """判断这张图片的主体是否为医学检查或医学检验报告单。
报告单包括化验、影像、病理、内镜、心电图等正式结果页面；聊天截图、科普、自拍、药品、缴费单、处方和纯文字分享不算。
理由只说明版式或内容类型，不要复述姓名、医院、编号或检验数值。
只输出一个JSON对象，不要Markdown：
{"is_report":true或false,"confidence":0到1,"reason":"不超过80字的判断理由"}"""


def _parse_json(content: str) -> dict:
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        raise ValueError("模型未返回JSON")
    data = json.loads(match.group(0))
    is_report = data.get("is_report")
    if not isinstance(is_report, bool):
        raise ValueError("模型未返回有效的报告单判断")
    confidence = max(0.0, min(1.0, float(data.get("confidence", 0))))
    reason = str(data.get("reason", ""))[:300]
    return {"is_report": is_report, "confidence": confidence, "reason": reason}


def global_model_organization_id(db: Session) -> str:
    """Return the canonical administrator workspace that owns the shared model."""
    settings = get_settings()
    organization_id = db.scalar(
        select(User.organization_id).where(
            User.email == settings.admin_email.lower(),
            User.role == "admin",
            User.active.is_(True),
            User.organization_id.is_not(None),
        )
    )
    if not organization_id:
        organization_id = db.scalar(
            select(User.organization_id)
            .where(
                User.role == "admin",
                User.active.is_(True),
                User.organization_id.is_not(None),
            )
            .order_by(User.created_at, User.id)
            .limit(1)
        )
    if not organization_id:
        raise RuntimeError("系统管理员工作空间不存在")
    return organization_id


def global_model_configuration(db: Session) -> ModelConfiguration | None:
    organization_id = global_model_organization_id(db)
    return db.scalar(
        select(ModelConfiguration).where(
            ModelConfiguration.organization_id == organization_id
        )
    )


def _configuration(_organization_id: str) -> tuple[ModelConfiguration, str]:
    with SessionLocal() as db:
        config = global_model_configuration(db)
        if not config:
            raise RuntimeError("管理员尚未配置全局视觉模型")
        # Copy all values before the ORM object leaves its session.
        detached = ModelConfiguration(
            organization_id=config.organization_id,
            provider=config.provider,
            base_url=config.base_url,
            model_name=config.model_name,
            encrypted_api_key=config.encrypted_api_key,
            key_hint=config.key_hint,
        )
        return detached, decrypt_secret(config.encrypted_api_key)


def _openai_endpoint(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("模型Base URL必须是有效的HTTPS地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("模型Base URL不能包含账号、查询参数或片段")
    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("模型Base URL不能指向本机")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError("模型Base URL不能指向内网地址")
    return value if value.endswith("/chat/completions") else f"{value}/chat/completions"


async def _validate_public_resolution(url: str) -> None:
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    port = parsed.port or 443
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo, hostname, port, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        raise ValueError("模型Base URL域名无法解析") from exc
    if not addresses:
        raise ValueError("模型Base URL域名没有可用地址")
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global:
            raise ValueError("模型Base URL解析到了本机或内网地址")


async def _read_limited(response: httpx.Response, limit: int) -> bytes:
    content = bytearray()
    async for chunk in response.aiter_bytes():
        content.extend(chunk)
        if len(content) > limit:
            raise ValueError("模型返回内容过长")
    return bytes(content)


def _image_data_url(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(path)
    size = path.stat().st_size
    if size > 15 * 1024 * 1024:
        raise ValueError("图片超过15MB，无法发送给视觉模型")
    suffix = path.suffix.lower()
    media_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".avif": "image/avif",
        ".gif": "image/gif",
    }.get(suffix, "image/jpeg")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


async def _classify_openai(
    path: Path, *, api_key: str, base_url: str, model_name: str
) -> dict:
    settings = get_settings()
    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": REPORT_PROMPT},
                    {"type": "image_url", "image_url": {"url": _image_data_url(path)}},
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": 300,
        "stream": False,
    }
    timeout = httpx.Timeout(settings.minimax_vision_timeout_seconds)
    endpoint = _openai_endpoint(base_url)
    await _validate_public_resolution(endpoint)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, trust_env=False
    ) as client:
        async with client.stream(
            "POST",
            endpoint,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        ) as response:
            raw = await _read_limited(response, 200_000)
            if response.status_code >= 400:
                raise RuntimeError(f"模型接口返回HTTP {response.status_code}")
    data = json.loads(raw)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("模型返回格式不兼容OpenAI Chat Completions") from exc
    if isinstance(content, list):
        content = "\n".join(
            str(item.get("text", "")) for item in content if isinstance(item, dict)
        )
    result = _parse_json(str(content))
    result["model"] = model_name
    return result


async def _classify_minimax_token_plan(path: Path, api_key: str) -> dict:
    settings = get_settings()
    helper = settings.mmx_helper_path.resolve()
    if not helper.is_file():
        raise RuntimeError("MiniMax图片判断组件未安装，请先运行 setup.ps1")
    if not path.is_file():
        raise FileNotFoundError(path)

    environment = os.environ.copy()
    environment["MINIMAX_API_KEY"] = api_key
    environment["MINIMAX_REGION"] = settings.minimax_region
    creationflags = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0)
    process = await asyncio.create_subprocess_exec(
        "node",
        str(helper),
        str(path.resolve()),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        cwd=str(helper.parent.parent),
        creationflags=creationflags,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=settings.minimax_vision_timeout_seconds
        )
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise RuntimeError("MiniMax图片判断超时") from None
    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").replace(api_key, "[REDACTED]")[:500]
        raise RuntimeError(f"MiniMax图片判断失败：{message.strip() or '未知错误'}")
    if len(stdout) > 20_000:
        raise ValueError("MiniMax返回内容过长")
    result = _parse_json(stdout.decode("utf-8", errors="strict"))
    result["model"] = "MiniMax Token Plan Vision"
    return result


async def classify_image(path: Path, organization_id: str) -> dict:
    config, api_key = _configuration(organization_id)
    if config.provider == "openai_compatible":
        return await _classify_openai(
            path,
            api_key=api_key,
            base_url=config.base_url or "",
            model_name=config.model_name or "",
        )
    if config.provider == "minimax_token_plan":
        return await _classify_minimax_token_plan(path, api_key)
    raise RuntimeError("未知的视觉模型配置")
