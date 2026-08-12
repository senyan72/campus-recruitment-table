"""与校招表 LocalDB 的薄集成。"""

from __future__ import annotations

from typing import Any

from app.config import db_path as default_campus_db_path
from app.db.local import LocalDB


def get_campus_db(path: str | None = None) -> LocalDB:
    return LocalDB(path) if path else LocalDB()


def list_published_jobs(
    *,
    campus_db: LocalDB | None = None,
    campus_db_path: str | None = None,
    keyword: str | None = None,
    location: str | None = None,
    bucket: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    db = campus_db or get_campus_db(campus_db_path)
    rows = db.list_jobs(
        status="active",
        keyword=keyword,
        location=location,
        bucket=bucket,
        limit=min(max(limit, 1), 100),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": r.get("id"),
                "title": r.get("title"),
                "company": r.get("company"),
                "city": r.get("work_location"),
                "bucket": r.get("recruit_bucket"),
                "education": r.get("education"),
                "industry": r.get("industry"),
                "description": (r.get("jd_text") or "")[:4000],
                "apply_url": r.get("apply_url"),
                "source_url": r.get("source_url"),
                "my_apply_status": r.get("my_apply_status"),
            }
        )
    return out


def get_job_for_match(*, job_id: str, campus_db: LocalDB | None = None, campus_db_path: str | None = None) -> dict[str, Any] | None:
    db = campus_db or get_campus_db(campus_db_path)
    row = db.get_job(job_id)
    if not row:
        return None
    return {
        "id": row.get("id"),
        "title": row.get("title"),
        "company": row.get("company"),
        "city": row.get("work_location"),
        "education": row.get("education"),
        "description": row.get("jd_text") or "",
        "apply_url": row.get("apply_url"),
    }
