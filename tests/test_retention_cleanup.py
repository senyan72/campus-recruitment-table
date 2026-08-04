"""岗位 1 年保留期：cutoff 与软删。"""

from datetime import date
from pathlib import Path

from app.collector.cleanup import cleanup_expired_jobs
from app.collector.filters import (
    DEFAULT_RETENTION_DAYS,
    is_past_retention,
    job_retention_age_date,
    resolve_retention_days,
    retention_cutoff,
)
from app.db.local import LocalDB


def test_retention_cutoff_default_365():
    today = date(2026, 8, 2)
    assert resolve_retention_days(None) == DEFAULT_RETENTION_DAYS == 365
    assert retention_cutoff(today=today) == date(2025, 8, 2)
    assert retention_cutoff(retention_days=365, today=today) == date(2025, 8, 2)
    assert retention_cutoff(retention_days=30, today=today) == date(2026, 7, 3)


def test_job_retention_age_date_takes_latest():
    job = {
        "open_at": "2024-01-15",
        "updated_at": "2025-06-01T12:00:00+00:00",
        "created_at": "2024-02-01",
    }
    assert job_retention_age_date(job) == date(2025, 6, 1)

    assert job_retention_age_date({"open_at": "2024-09-01"}) == date(2024, 9, 1)
    assert job_retention_age_date({}) is None
    assert job_retention_age_date(None) is None


def test_is_past_retention_boundary():
    today = date(2026, 8, 2)
    # 参照日 == cutoff → 未超期（严格小于）
    assert not is_past_retention(
        {"open_at": "2025-08-02"},
        retention_days=365,
        today=today,
    )
    # 早一天 → 超期
    assert is_past_retention(
        {"open_at": "2025-08-01"},
        retention_days=365,
        today=today,
    )
    # open_at 很旧但 updated_at 仍在窗内 → 保留
    assert not is_past_retention(
        {"open_at": "2023-01-01", "updated_at": "2026-01-01"},
        retention_days=365,
        today=today,
    )
    # 无日期 → 不误删
    assert not is_past_retention({}, retention_days=365, today=today)


def test_cleanup_expired_jobs_soft_deletes(tmp_path: Path):
    db = LocalDB(tmp_path / "ret.db")
    today = date(2026, 8, 2)

    old_id = db.upsert_job(
        {
            "company": "旧司",
            "title": "旧岗",
            "source_url": "https://example.com/old",
            "open_at": "2024-01-01",
            "status": "active",
        }
    )
    # 强制把 updated_at/created_at 也写成旧日期（upsert 会写 now）
    with db.conn() as c:
        c.execute(
            "UPDATE jobs SET open_at=?, updated_at=?, created_at=? WHERE id=?",
            ("2024-01-01", "2024-01-02T00:00:00+00:00", "2024-01-01T00:00:00+00:00", old_id),
        )

    fresh_id = db.upsert_job(
        {
            "company": "新司",
            "title": "新岗",
            "source_url": "https://example.com/new",
            "open_at": "2026-07-01",
            "status": "active",
        }
    )
    company_id = db.upsert_company({"name": "种子公司", "verify_status": "unverified"})

    db.enqueue_review(
        "stale_over_3m",
        {
            "title": "异常旧岗",
            "source_url": "https://example.com/rev",
            "open_at": "2023-05-01",
            "updated_at": "2023-05-02",
            "created_at": "2023-05-01",
        },
        "old",
    )
    db.enqueue_review(
        "stale_over_3m",
        {
            "title": "异常新岗",
            "source_url": "https://example.com/rev2",
            "open_at": "2026-06-01",
        },
        "fresh",
    )

    result = cleanup_expired_jobs(db, retention_days=365, today=today)
    assert result["deleted"] == 1
    assert old_id in result["deleted_ids"]
    assert result["review_ignored"] == 1
    assert result["cutoff"] == "2025-08-02"

    assert db.get_job(old_id)["status"] == "deleted"
    assert db.get_job(fresh_id)["status"] == "active"
    assert db.get_company(company_id) is not None

    pending = db.list_review_queue("pending", limit=50)
    assert len(pending) == 1
    assert pending[0]["payload"]["title"] == "异常新岗"
