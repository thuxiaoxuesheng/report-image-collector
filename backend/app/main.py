from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from hashlib import sha256
from hmac import compare_digest
from hmac import new as hmac_new
from pathlib import Path
from time import time
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from .api import router
from .bootstrap import initialize_database
from .cleanup import cleanup_worker
from .collector import task_worker
from .config import get_settings
from .database import SessionLocal
from .login_security import clear_login_failures, login_allowed, record_login_failure, request_ip
from .models import User, UserSession, as_utc, utcnow
from .site_auth import (
    create_opaque_session,
    hash_session_token,
    hash_site_password,
    session_expiry,
    verify_site_password,
)
from .social_copilot import social_copilot
from .task_queue import recover_expired_leases

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Equalize password verification work for unknown and known usernames so the
# login endpoint does not become a practical username-enumeration oracle.
DUMMY_PASSWORD_HASH = hash_site_password(
    "not-a-real-account-password", salt=b"xhs-login-dummy!"
)


def login_csrf_secret() -> bytes:
    settings = get_settings()
    configured = settings.site_auth_session_secret or settings.secrets_master_key
    if configured:
        # All web workers/instances using the same deployment secret validate
        # the same short-lived form tokens.
        return sha256(f"xhs-login-csrf:{configured}".encode()).digest()
    # Development fallback; a restart merely invalidates an open login form.
    return secrets.token_bytes(32)


LOGIN_CSRF_SECRET = login_csrf_secret()
LOGIN_CSRF_MAX_AGE_SECONDS = 600
LOGIN_CSRF_COOKIE = "xhs_login_csrf"


def recover_interrupted_tasks() -> None:
    recover_expired_leases()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    initialize_database()
    recover_interrupted_tasks()
    await social_copilot.start()
    await task_worker.start()
    await cleanup_worker.start()
    yield
    await cleanup_worker.stop()
    await task_worker.stop()
    await social_copilot.stop()


app = FastAPI(title="小红书检查检验图片采集系统", version="0.1.0", lifespan=lifespan)


def is_cross_site_write(request: Request) -> bool:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return False
    origin = request.headers.get("origin", "").strip()
    fetch_site = request.headers.get("sec-fetch-site", "")
    if origin and origin.casefold() != "null":
        origin_authority = urlsplit(origin).netloc.lower()
        allowed_authorities = {request.headers.get("host", "").lower()}

        # The application only listens on loopback in production and Caddy is
        # the public entry point. Honour its original Host header without
        # allowing an Internet client to spoof X-Forwarded-Host directly.
        client_host = request.client.host if request.client else ""
        if client_host in {"127.0.0.1", "::1"}:
            forwarded_host = request.headers.get("x-forwarded-host", "").lower()
            if forwarded_host:
                allowed_authorities.add(forwarded_host)

        # Origin is authoritative for browser write requests. Some privacy
        # tools and upstream services have been observed to report an
        # inconsistent Sec-Fetch-Site value even for a same-origin form post.
        return not origin_authority or origin_authority not in allowed_authorities

    # Older clients may omit Origin, while sandboxed/privacy-oriented clients
    # can serialize an opaque origin as the literal value "null". In either
    # case use browser-controlled Fetch Metadata: the observed macOS Chrome
    # login flow reports `Origin: null` together with `same-origin`.
    return fetch_site == "cross-site"


def cross_site_rejection() -> JSONResponse:
    return JSONResponse(
        {"detail": "拒绝跨站请求"},
        status_code=403,
        media_type="application/json; charset=utf-8",
    )


def valid_login_csrf(request: Request) -> bool:
    """Validate a signed token bound to the browser that loaded the login page."""
    if request.method != "POST" or request.url.path != "/site-login":
        return False
    token = request.query_params.get("csrf", "")
    cookie_token = request.cookies.get(LOGIN_CSRF_COOKIE, "")
    if not token or not cookie_token or not compare_digest(token, cookie_token):
        return False
    try:
        timestamp_text, nonce, supplied_signature = token.split(".", 2)
        timestamp = int(timestamp_text)
    except (TypeError, ValueError):
        return False
    age = int(time()) - timestamp
    if age < -30 or age > LOGIN_CSRF_MAX_AGE_SECONDS:
        return False
    payload = f"{timestamp_text}.{nonce}"
    expected_signature = hmac_new(
        LOGIN_CSRF_SECRET, payload.encode(), sha256
    ).hexdigest()
    return compare_digest(supplied_signature, expected_signature)


def create_login_csrf() -> str:
    payload = f"{int(time())}.{secrets.token_urlsafe(24)}"
    signature = hmac_new(LOGIN_CSRF_SECRET, payload.encode(), sha256).hexdigest()
    return f"{payload}.{signature}"


async def browser_security(request: Request, call_next):
    if is_cross_site_write(request) and not valid_login_csrf(request):
        response = cross_site_rejection()
    else:
        response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'",
    )
    if get_settings().environment == "production":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


def login_page(
    error: str = "", *, status_code: int = 200, headers: dict[str, str] | None = None
) -> HTMLResponse:
    template = (Path(__file__).parent / "static" / "login.html").read_text(encoding="utf-8")
    message = f'<p class="error">{error}</p>' if error else ""
    response_headers = {"Cache-Control": "no-store"}
    response_headers.update(headers or {})
    csrf_token = create_login_csrf()
    response = HTMLResponse(
        template.replace("<!--ERROR-->", message).replace("<!--CSRF-->", csrf_token),
        status_code=status_code,
        headers=response_headers,
    )
    production = get_settings().environment == "production"
    response.set_cookie(
        LOGIN_CSRF_COOKIE,
        csrf_token,
        max_age=LOGIN_CSRF_MAX_AGE_SECONDS,
        secure=production,
        httponly=True,
        # None is intentional in production: some privacy/proxy combinations
        # classify the initial form submission as cross-site. The random token
        # still has to match the browser cookie, so another site cannot forge it.
        samesite="none" if production else "lax",
        path="/site-login",
    )
    return response


def password_page(error: str = "") -> HTMLResponse:
    template = (Path(__file__).parent / "static" / "change-password.html").read_text(
        encoding="utf-8"
    )
    message = f'<p class="error">{error}</p>' if error else ""
    return HTMLResponse(
        template.replace("<!--ERROR-->", message), headers={"Cache-Control": "no-store"}
    )


def session_user(db, request: Request) -> tuple[UserSession | None, User | None]:
    token = request.cookies.get(get_settings().site_auth_cookie_name, "")
    if not token:
        return None, None
    session = db.scalar(
        select(UserSession).where(UserSession.token_hash == hash_session_token(token))
    )
    if not session or as_utc(session.expires_at) <= utcnow():
        if session:
            db.delete(session)
            db.commit()
        return None, None
    user = db.get(User, session.user_id)
    if not user or not user.active:
        return session, None
    return session, user


@app.middleware("http")
async def site_login_gate(request: Request, call_next):
    if is_cross_site_write(request) and not valid_login_csrf(request):
        return cross_site_rejection()
    settings = get_settings()
    if not settings.site_auth_enabled:
        return await call_next(request)
    if request.url.path == "/api/health":
        return await call_next(request)
    if request.url.path == "/site-login":
        if request.method == "GET":
            return login_page()
        if request.method == "POST":
            form = await request.form()
            username = str(form.get("username", "")).strip().lower()
            password = str(form.get("password", ""))
            ip_address = request_ip(request)
            with SessionLocal() as db:
                decision = login_allowed(db, username, ip_address)
                if not decision.allowed:
                    return login_page(
                        error="登录尝试过多，请稍后再试",
                        status_code=429,
                        headers={"Retry-After": str(decision.retry_after_seconds)},
                    )
                user = db.scalar(select(User).where(User.username == username))
                candidate_hash = (
                    user.password_hash
                    if user and user.active and user.password_hash
                    else DUMMY_PASSWORD_HASH
                )
                password_valid = verify_site_password(password, candidate_hash)
                valid = bool(user and user.active and user.password_hash and password_valid)
                if not valid:
                    record_login_failure(db, username, ip_address)
                    db.commit()
                    return login_page(error="账号或密码错误", status_code=401)
                clear_login_failures(db, username, ip_address)
                token, token_hash = create_opaque_session()
                db.add(
                    UserSession(
                        user_id=user.id,
                        token_hash=token_hash,
                        expires_at=session_expiry(settings.site_auth_session_days),
                    )
                )
                user.last_login_at = utcnow()
                destination = "/change-password" if user.must_change_password else "/"
                db.commit()
            response = RedirectResponse(destination, status_code=303)
            response.set_cookie(
                settings.site_auth_cookie_name,
                token,
                max_age=settings.site_auth_session_days * 86400,
                secure=settings.environment == "production",
                httponly=True,
                samesite="strict",
                path="/",
            )
            response.delete_cookie(LOGIN_CSRF_COOKIE, path="/site-login")
            return response
        return JSONResponse({"detail": "Method Not Allowed"}, status_code=405)
    if request.url.path == "/site-logout":
        with SessionLocal() as db:
            session, _ = session_user(db, request)
            if session:
                db.delete(session)
                db.commit()
        response = RedirectResponse("/site-login", status_code=303)
        response.delete_cookie(settings.site_auth_cookie_name, path="/")
        return response
    with SessionLocal() as db:
        _session, user = session_user(db, request)
        if request.url.path == "/change-password":
            if not user:
                return RedirectResponse("/site-login", status_code=303)
            if request.method == "GET":
                return password_page()
            if request.method == "POST":
                form = await request.form()
                password = str(form.get("password", ""))
                confirmation = str(form.get("confirmation", ""))
                if password != confirmation:
                    return password_page("两次输入的密码不一致")
                try:
                    user.password_hash = hash_site_password(password)
                except ValueError as exc:
                    return password_page(str(exc))
                user.must_change_password = False
                db.commit()
                return RedirectResponse("/", status_code=303)
            return JSONResponse({"detail": "Method Not Allowed"}, status_code=405)
        if user:
            if user.must_change_password:
                if request.url.path.startswith("/api/"):
                    return JSONResponse({"detail": "请先修改初始密码"}, status_code=403)
                return RedirectResponse("/change-password", status_code=303)
            return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "请先登录网站"}, status_code=401)
    return RedirectResponse("/site-login", status_code=303)


# Register this last so it is the outermost HTTP middleware and security
# headers also cover login-gate redirects, errors and unauthenticated responses.
app.middleware("http")(browser_security)

app.include_router(router)

static_dir = Path(__file__).parent / "static"
app.mount("/assets", StaticFiles(directory=static_dir), name="assets")


@app.get("/{full_path:path}", include_in_schema=False)
def spa(full_path: str) -> FileResponse:
    return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-store"})
