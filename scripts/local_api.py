from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import get_settings  # noqa: E402
from backend.app.database import SessionLocal  # noqa: E402
from backend.app.models import User, UserSession, utcnow  # noqa: E402
from backend.app.site_auth import create_opaque_session  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Call a protected collector API from the server loopback interface."
    )
    parser.add_argument("method", choices=("GET", "POST", "PUT", "PATCH", "DELETE"))
    parser.add_argument("path", help="Absolute /api/... path")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--json", dest="json_body", default="")
    parser.add_argument(
        "--json-base64",
        default="",
        help="UTF-8 JSON encoded as Base64 (avoids Windows shell quoting issues)",
    )
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument(
        "--output",
        default="",
        help="Write the response body below the project data directory",
    )
    return parser.parse_args()


def console_output(
    status: int,
    response_body: bytes,
    *,
    transport_error: str = "",
    output_path: str = "",
) -> bytes:
    output = bytearray(f"HTTP_STATUS={status}\n".encode())
    if transport_error:
        output.extend(f"TRANSPORT_ERROR={transport_error}\n".encode())
    if output_path:
        output.extend(f"OUTPUT_PATH={output_path}\n".encode())
        output.extend(f"OUTPUT_BYTES={len(response_body)}\n".encode())
    elif response_body:
        output.extend(response_body)
        output.extend(b"\n")
    return bytes(output)


def main() -> int:
    args = parse_args()
    if not args.path.startswith("/api/") or "://" in args.path:
        raise SystemExit("path must be a local /api/... path")

    body = None
    headers: dict[str, str] = {}
    if args.json_body and args.json_base64:
        raise SystemExit("use only one of --json and --json-base64")
    json_body = args.json_body
    if args.json_base64:
        try:
            json_body = base64.b64decode(args.json_base64, validate=True).decode()
        except (ValueError, UnicodeDecodeError) as exc:
            raise SystemExit("invalid --json-base64 value") from exc
    if json_body:
        body = json.dumps(json.loads(json_body), ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"

    token = ""
    token_hash = ""
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == args.username.lower()))
        if not user or not user.active:
            raise SystemExit("active user not found")
        token, token_hash = create_opaque_session()
        db.add(
            UserSession(
                user_id=user.id,
                token_hash=token_hash,
                expires_at=utcnow() + timedelta(minutes=10),
            )
        )
        db.commit()

    headers["Cookie"] = f"{get_settings().site_auth_cookie_name}={token}"
    request = Request(
        f"http://127.0.0.1:8765{args.path}",
        data=body,
        headers=headers,
        method=args.method,
    )
    status = 0
    response_body = b""
    transport_error = ""
    try:
        try:
            with urlopen(request, timeout=args.timeout) as response:  # noqa: S310
                status = response.status
                response_body = response.read()
        except HTTPError as exc:
            status = exc.code
            response_body = exc.read()
        except (TimeoutError, URLError) as exc:
            transport_error = str(exc.reason if isinstance(exc, URLError) else exc)
    finally:
        with SessionLocal() as db:
            db.query(UserSession).filter(
                UserSession.token_hash == token_hash
            ).delete(synchronize_session=False)
            db.commit()

    output_path = ""
    if args.output and response_body:
        output_root = (PROJECT_ROOT / "data" / "local-api-output").resolve()
        candidate = Path(args.output)
        if candidate.is_absolute():
            raise SystemExit("--output must be relative")
        candidate = (output_root / candidate).resolve()
        if not candidate.is_relative_to(output_root):
            raise SystemExit("--output must stay below data/local-api-output")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        try:
            with candidate.open("xb") as output_file:
                output_file.write(response_body)
        except FileExistsError as exc:
            raise SystemExit("--output refuses to overwrite an existing file") from exc
        output_path = str(candidate)

    output = console_output(
        status,
        response_body,
        transport_error=transport_error,
        output_path=output_path,
    )
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()
    return 0 if 200 <= status < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
