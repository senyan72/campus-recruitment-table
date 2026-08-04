"""清除当前所有数据：DB 清空范围与口令常量。"""

from pathlib import Path

from app.db.local import LocalDB
from app.ui.admin import CLEAR_ALL_DATA_PASSWORD


def test_clear_all_password_constant():
    assert CLEAR_ALL_DATA_PASSWORD == "+A1838406637zjh"


def test_clear_all_job_related_data_keeps_companies(tmp_path: Path):
    db = LocalDB(tmp_path / "clear.db")
    cid = db.upsert_company(
        {
            "name": "保留种子公司",
            "hint_apply_urls": ["https://example.com/apply"],
            "verify_status": "official",
        }
    )
    jid = db.upsert_job(
        {
            "company_id": cid,
            "company": "保留种子公司",
            "title": "测试岗",
            "source_url": "https://example.com/job/1",
            "status": "active",
        }
    )
    db.set_my_status(jid, "已投递", "note")
    db.enqueue_review("parse", {"title": "坏岗"}, reason="测试")
    db.enqueue_source_verify(cid, "career", "https://example.com", reason="测")
    db.add_digest("旧日报")
    db.set_meta("last_scan_finished_at", "2026-01-01T00:00:00+00:00")
    db.set_meta("deep_collect_run_id", "run-1")
    db.create_collect_run([{"id": cid, "name": "保留种子公司"}], reset=True)

    result = db.clear_all_job_related_data()

    assert result["jobs"] >= 1
    assert result["review_queue"] >= 1
    assert result["source_verify_queue"] >= 1
    assert result["digest_log"] >= 1
    assert result["collect_queue"] >= 1
    assert db.count_jobs() == 0
    assert db.count_companies() == 1
    assert db.list_review_queue("pending", limit=10) == []
    assert db.list_source_verify("pending", limit=10) == []
    assert db.latest_digests(10) == []
    assert db.get_meta("last_scan_finished_at") is None
    assert db.get_meta("deep_collect_run_id") is None
    assert db.get_company(cid) is not None
