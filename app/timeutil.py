"""统一「当前时间」：默认中国本地（Asia/Shanghai / UTC+8），可用 CAMPUS_JOBS_NOW 覆盖。"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

ENV_NOW = "CAMPUS_JOBS_NOW"


def _china_tz() -> Any:
    """优先 IANA Asia/Shanghai；无 tzdata 时回退固定 UTC+8。"""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Shanghai")
    except Exception:
        return timezone(timedelta(hours=8), name="UTC+08:00")


CN_TZ = _china_tz()


def _parse_override(raw: str) -> datetime | None:
    s = (raw or "").strip()
    if not s:
        return None
    s = s.replace("/", "-").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:19] if ":" in fmt else s[:10], fmt)
            return dt.replace(tzinfo=CN_TZ)
        except ValueError:
            continue
    m = re.match(
        r"(20\d{2})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?",
        s,
    )
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh = int(m.group(4) or 0)
    mm = int(m.group(5) or 0)
    ss = int(m.group(6) or 0)
    try:
        return datetime(y, mo, d, hh, mm, ss, tzinfo=CN_TZ)
    except ValueError:
        return None


def now() -> datetime:
    """带时区的当前时间（中国本地）。"""
    override = os.environ.get(ENV_NOW, "").strip()
    if override:
        dt = _parse_override(override)
        if dt is not None:
            return dt
    return datetime.now(CN_TZ)


def today() -> date:
    """中国本地日历日。"""
    return now().date()


def utc_now_iso() -> str:
    """UTC ISO 时间戳（库表/同步用）；尊重 CAMPUS_JOBS_NOW。"""
    return now().astimezone(timezone.utc).replace(microsecond=0).isoformat()
