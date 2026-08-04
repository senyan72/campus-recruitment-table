from pathlib import Path

import pytest

from app.db.local import LocalDB
from app.sync.supabase import SupabaseSync


def test_job_and_my_status(tmp_path: Path):
    db = LocalDB(tmp_path / "jobs.db")
    jid = db.upsert_job(
        {
            "company": "测试公司",
            "title": "后端开发",
            "source_url": "https://example.com/job/1",
            "recruit_project": "秋招",
            "recruit_bucket": "校招",
            "status": "active",
        }
    )
    db.set_my_status(jid, "已投递", "已网申")
    jobs = db.list_jobs(bucket="校招")
    assert len(jobs) == 1
    assert jobs[0]["my_apply_status"] == "已投递"
    detail = db.get_job(jid)
    assert detail is not None
    assert detail["my_note"] == "已网申"


def test_same_company_same_title_distinct_urls_are_kept(tmp_path: Path):
    db = LocalDB(tmp_path / "same-title-urls.db")
    first = db.upsert_job(
        {
            "company": "测试公司",
            "title": "算法工程师",
            "source_url": "https://example.com/campus",
            "apply_url": "https://example.com/jobs/hefei",
            "work_location": "合肥",
            "status": "active",
        }
    )
    second = db.upsert_job(
        {
            "company": "测试公司",
            "title": "算法工程师",
            "source_url": "https://example.com/campus",
            "apply_url": "https://example.com/jobs/shanghai",
            "work_location": "上海",
            "status": "active",
        }
    )
    assert first != second
    assert db.count_jobs(status="active") == 2


def test_revert_jobs_to_pending_review_and_revoke_queue(tmp_path: Path):
    db = LocalDB(tmp_path / "revert.db")
    jid = db.upsert_job(
        {
            "company": "测试公司",
            "title": "前端",
            "source_url": "https://example.com/job/2",
            "status": "active",
            "cloud_updated_at": "2026-08-01T10:00:00",
        }
    )
    result = db.revert_jobs_to_pending_review([jid])
    assert result["reverted"] == 1
    assert result["revoke_queued"] == 1
    job = db.get_job(jid)
    assert job is not None
    assert job["status"] == "pending_review"
    assert not (job.get("cloud_updated_at") or "").strip()
    assert jid not in {j["id"] for j in db.list_jobs(status="active")}
    assert jid in {j["id"] for j in db.list_jobs(status="pending_review")}
    revoke = db.list_cloud_revoke_jobs()
    assert len(revoke) == 1
    assert revoke[0]["id"] == jid
    assert revoke[0]["status"] == "deleted"

    # 再次审核通过应取消撤云
    job["status"] = "active"
    db.upsert_job(job)
    db.remove_cloud_revoke_ids([jid])
    assert db.list_cloud_revoke_jobs() == []


def test_publish_includes_revoke_as_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = LocalDB(tmp_path / "revoke_push.db")
    jid = db.upsert_job(
        {
            "company": "A",
            "title": "T",
            "source_url": "https://example.com/r",
            "status": "active",
            "cloud_updated_at": "2026-08-01T10:00:00",
        }
    )
    db.revert_jobs_to_pending_review([jid])
    captured: list[list[dict]] = []

    def fake_push(self, jobs, *, chunk_size=200):  # noqa: ANN001
        captured.append(list(jobs))
        return len(jobs)

    monkeypatch.setattr(SupabaseSync, "push_jobs", fake_push)
    sync = SupabaseSync("https://example.supabase.co", "anon", "service")
    result = sync.publish_local_jobs_for_sync(db)
    assert result["revoked"] == 1
    assert captured and captured[0][0]["status"] == "deleted"
    assert db.list_cloud_revoke_jobs() == []
    # 本地仍为待审核且未标已推送
    local = db.get_job(jid)
    assert local is not None
    assert local["status"] == "pending_review"
    assert not (local.get("cloud_updated_at") or "").strip()
