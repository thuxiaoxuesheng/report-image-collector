from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from jwt import PyJWKClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .enums import OrganizationStatus, Role
from .models import Organization, User, UserSession, as_utc, utcnow
from .site_auth import hash_session_token

settings = get_settings()


@lru_cache(maxsize=2)
def _jwk_client(certs_url: str) -> PyJWKClient:
    return PyJWKClient(certs_url)


@dataclass
class CurrentUser:
    user: User
    organization: Organization | None


def _verify_cloudflare_token(token: str) -> str:
    if not settings.cloudflare_team_domain or not settings.cloudflare_aud:
        raise HTTPException(status_code=503, detail="Cloudflare Access 尚未配置")
    domain = settings.cloudflare_team_domain.removesuffix("/")
    certs_url = f"{domain}/cdn-cgi/access/certs"
    try:
        signing_key = _jwk_client(certs_url).get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.cloudflare_aud,
            issuer=domain,
        )
        return str(payload["email"]).lower()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Access令牌无效"
        ) from exc


def _request_email(
    request: Request,
    cf_token: str | None,
    cf_email: str | None,
    dev_email: str | None,
) -> str:
    client_host = request.client.host if request.client else ""
    via_cloudflare = bool(request.headers.get("Cf-Connecting-IP"))
    # cloudflared also connects from loopback. Never let a tunneled request inherit
    # the local-development bypass merely because its origin socket is local.
    is_local = client_host in {"127.0.0.1", "::1", "testclient"} and not via_cloudflare
    if settings.dev_auth_bypass and is_local:
        return (dev_email or settings.admin_email).lower()
    if cf_token:
        return _verify_cloudflare_token(cf_token)
    if cf_email and via_cloudflare and not settings.cloudflare_team_domain:
        # 只有源站严格绑定 localhost 时才允许使用 Cloudflare 注入邮箱头。
        return cf_email.lower()
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请通过授权入口访问")


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    cf_token: str | None = Header(default=None, alias="Cf-Access-Jwt-Assertion"),
    cf_email: str | None = Header(default=None, alias="Cf-Access-Authenticated-User-Email"),
    dev_email: str | None = Header(default=None, alias="X-Dev-User"),
) -> CurrentUser:
    user = None
    session_token = request.cookies.get(settings.site_auth_cookie_name, "")
    if session_token:
        session = db.scalar(
            select(UserSession).where(UserSession.token_hash == hash_session_token(session_token))
        )
        if session and as_utc(session.expires_at) > utcnow():
            user = db.get(User, session.user_id)
        elif session:
            db.delete(session)
            db.commit()
    if not user and not settings.site_auth_enabled:
        email = _request_email(request, cf_token, cf_email, dev_email)
        user = db.scalar(select(User).where(User.email == email))
    if not user or not user.active:
        raise HTTPException(status_code=403, detail="账号不存在或已停用")
    if user.must_change_password and request.url.path != "/api/auth/change-password":
        raise HTTPException(status_code=403, detail="请先修改初始密码")
    organization = None
    if user.organization_id:
        organization = db.get(Organization, user.organization_id)
        if not organization or organization.status != OrganizationStatus.ACTIVE:
            raise HTTPException(status_code=403, detail="账号已停用或授权到期")
        if (
            organization.authorization_expires_at
            and as_utc(organization.authorization_expires_at) <= utcnow()
        ):
            organization.status = OrganizationStatus.EXPIRED
            db.commit()
            raise HTTPException(status_code=403, detail="账号授权已到期")
    return CurrentUser(user=user, organization=organization)


def require_admin(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if current.user.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return current


def scoped_organization_id(current: CurrentUser, requested: str | None = None) -> str:
    if not current.organization:
        raise HTTPException(status_code=403, detail="用户工作空间不存在")
    if requested and requested != current.organization.id:
        raise HTTPException(status_code=403, detail="不能访问其他用户的数据")
    return current.organization.id
