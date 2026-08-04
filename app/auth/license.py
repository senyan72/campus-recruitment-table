"""Viewer access-license date helpers."""

from __future__ import annotations

from datetime import date, datetime


def normalize_expiry(value: str | date | datetime | None) -> str | None:
    """Normalize an account expiry to ISO date; blank means no expiry."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = str(value).strip()
    if not raw:
        return None
    # Accept an ISO timestamp from Supabase while storing only the date locally.
    candidate = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate).date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(raw[:10]).isoformat()
        except ValueError as exc:
            raise ValueError("到期日期必须是 YYYY-MM-DD") from exc


def is_expired(value: str | date | datetime | None, *, today: date | None = None) -> bool:
    normalized = normalize_expiry(value)
    if not normalized:
        return False
    return date.fromisoformat(normalized) < (today or date.today())


def expiry_label(value: str | date | datetime | None) -> str:
    normalized = normalize_expiry(value)
    return normalized or "未设置"
