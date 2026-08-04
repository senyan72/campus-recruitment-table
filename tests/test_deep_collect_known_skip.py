"""深度采集：已入库岗应快速跳过并换下一企。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.collector.website import collect_company_careers, _list_pass_fully_known
from app.db.local import LocalDB


def test_list_pass_fully_known():
    stats = {"batch_new": 0, "skipped_known": 5}
    assert _list_pass_fully_known(stats, 5)
    assert not _list_pass_fully_known(stats, 6)
    assert not _list_pass_fully_known({"batch_new": 1, "skipped_known": 5}, 5)


def test_collect_company_stops_when_all_known(tmp_path: Path):
    db = LocalDB(tmp_path / "known_skip.db")
    cid = db.upsert_company(
        {
            "name": "全已知公司",
            "verify_status": "official",
            "career_urls": ["https://career.example/campus"],
        }
    )
    for i in range(3):
        db.upsert_job(
            {
                "company": "全已知公司",
                "title": f"岗{i}",
                "apply_url": f"https://career.example/j/{i}",
                "source_url": "https://career.example/campus",
                "status": "active",
            }
        )

    calls = {"n": 0}

    def fake_collect_from_career_page(db, company, career_url, **kwargs):
        calls["n"] += 1
        return {
            "parsed": 3,
            "published": 0,
            "queued": 0,
            "errors": 0,
            "skipped": 0,
            "stale": 0,
            "batch_new": 0,
            "skipped_known": 3,
        }

    company = db.get_company(cid)
    with patch(
        "app.collector.website.collect_from_career_page",
        side_effect=fake_collect_from_career_page,
    ):
        stats = collect_company_careers(
            db,
            company,
            max_career_urls=3,
            stop_when_published=True,
        )
    assert calls["n"] == 1, "应只扫第一个入口即换企"
    assert stats["skipped_known"] == 3
    assert stats["batch_new"] == 0


def test_reclaim_stuck_running_queue(tmp_path: Path):
    db = LocalDB(tmp_path / "reclaim.db")
    companies = [{"id": db.upsert_company({"name": "A", "career_urls": ["https://x/a"]}), "name": "A"}]
    run_id = db.create_collect_run(companies, reset=True, resume=False)
    items = db.list_collect_queue(run_id, status="pending", limit=1)
    db.update_collect_queue_item(items[0]["id"], status="running", bump_attempts=True)
    n = db.reclaim_stuck_collect_queue(run_id)
    assert n == 1
    pending = db.list_collect_queue(run_id, status="pending", limit=10)
    assert len(pending) == 1
