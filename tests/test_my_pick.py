"""Viewer 本机个人选取「个人校招投递」：持久化、筛选、不同步云端。"""

from pathlib import Path

import pytest

from app.db.local import MY_PICK_CAMPUS, LocalDB
from app.sync.supabase import SupabaseSync, _local_to_cloud


def _seed_job(db: LocalDB, *, title: str, bucket: str = "校招") -> str:
    return db.upsert_job(
        {
            "company": "测试公司",
            "title": title,
            "source_url": f"https://example.com/job/{title}",
            "recruit_bucket": bucket,
            "status": "active",
        }
    )


def test_add_and_filter_my_pick(tmp_path: Path):
    db = LocalDB(tmp_path / "pick.db")
    j1 = _seed_job(db, title="后端")
    j2 = _seed_job(db, title="前端")
    _seed_job(db, title="其他")

    assert db.add_my_pick([j1, j2]) == 2
    assert db.add_my_pick([j1]) == 0
    assert db.count_my_pick() == 2
    assert set(db.list_my_pick_job_ids()) == {j1, j2}

    picked = db.list_jobs(my_pick_only=True)
    assert len(picked) == 2
    assert {j["title"] for j in picked} == {"后端", "前端"}


def test_remove_my_pick(tmp_path: Path):
    db = LocalDB(tmp_path / "pick_rm.db")
    j1 = _seed_job(db, title="A")
    j2 = _seed_job(db, title="B")
    db.add_my_pick([j1, j2])
    assert db.remove_my_pick([j1]) == 1
    assert db.count_my_pick() == 1
    assert db.list_my_pick_job_ids() == [j2]


def test_cloud_pull_preserves_my_pick(tmp_path: Path):
    db = LocalDB(tmp_path / "pick_sync.db")
    jid = _seed_job(db, title="保留选取")
    db.add_my_pick([jid])

    cloud_row = {
        "id": jid,
        "company": "测试公司",
        "title": "保留选取",
        "source_url": "https://example.com/job/保留选取",
        "recruit_bucket": "校招",
        "status": "active",
        "updated_at": "2026-08-02T12:00:00+00:00",
        "job_tags": [],
    }
    db.replace_jobs_from_cloud([cloud_row])

    assert db.count_my_pick() == 1
    assert jid in db.list_my_pick_job_ids()
    job = db.get_job(jid)
    assert job is not None
    assert job["company"] == "测试公司"


def test_sync_payload_excludes_my_pick(tmp_path: Path):
    db = LocalDB(tmp_path / "pick_push.db")
    jid = _seed_job(db, title="不推送选取")
    db.add_my_pick([jid])
    job = db.get_job(jid)
    assert job is not None

    payload = _local_to_cloud(job)
    assert "my_pick" not in payload
    assert MY_PICK_CAMPUS not in str(payload.values())

    captured: list[list[dict]] = []

    def fake_push(self, jobs, *, chunk_size=200):  # noqa: ANN001
        captured.append(list(jobs))
        return len(jobs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(SupabaseSync, "push_jobs", fake_push)
    sync = SupabaseSync("https://example.supabase.co", "anon", "service")
    sync.publish_local_jobs_for_sync(db)
    monkeypatch.undo()

    assert captured
    for row in captured[0]:
        assert "my_pick" not in row
        assert MY_PICK_CAMPUS not in str(row.values())
