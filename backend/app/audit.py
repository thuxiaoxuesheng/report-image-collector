from __future__ import annotations

import json

from sqlalchemy.orm import Session

from .models import AuditLog


def write_audit(
    db: Session,
    *,
    action: str,
    user_id: str | None = None,
    organization_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict | str | None = None,
) -> None:
    if isinstance(detail, dict):
        detail = json.dumps(detail, ensure_ascii=False, default=str)
    db.add(
        AuditLog(
            action=action,
            user_id=user_id,
            organization_id=organization_id,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
        )
    )
