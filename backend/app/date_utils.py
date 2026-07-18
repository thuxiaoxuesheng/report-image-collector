from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def parse_xhs_datetime(text: str, now: datetime | None = None) -> datetime | None:
    """Parse common visible XHS publication date formats without using private APIs."""
    now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    normalized = re.sub(r"\s+", " ", text.strip())

    full = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", normalized)
    if full:
        return datetime(int(full[1]), int(full[2]), int(full[3]), tzinfo=SHANGHAI)

    month_day = re.search(r"(?<!\d)(\d{1,2})[-/.月](\d{1,2})(?:日)?", normalized)
    if month_day:
        candidate = datetime(now.year, int(month_day[1]), int(month_day[2]), tzinfo=SHANGHAI)
        if candidate > now + timedelta(days=1):
            candidate = candidate.replace(year=now.year - 1)
        return candidate

    if "昨天" in normalized:
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if "前天" in normalized:
        return (now - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    days = re.search(r"(\d+)天前", normalized)
    if days:
        return now - timedelta(days=int(days[1]))
    hours = re.search(r"(\d+)小时前", normalized)
    if hours:
        return now - timedelta(hours=int(hours[1]))
    minutes = re.search(r"(\d+)分钟前", normalized)
    if minutes:
        return now - timedelta(minutes=int(minutes[1]))
    if any(token in normalized for token in ("刚刚", "分钟前", "小时前")):
        return now
    return None
