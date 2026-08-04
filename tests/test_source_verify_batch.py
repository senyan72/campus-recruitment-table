"""源验证批量标官方 / 拒绝。"""

from __future__ import annotations

from pathlib import Path

from app.db.local import LocalDB


def test_batch_reject_and_official(tmp_path: Path) -> None:
    db = LocalDB(tmp_path / "t.db")
    c1 = db.upsert_company(
        {
            "name": "甲公司",
            "verify_status": "unverified",
            "career_urls": [],
        }
    )
    c2 = db.upsert_company(
        {
            "name": "乙公司",
            "verify_status": "unverified",
            "career_urls": [],
        }
    )
    s1 = db.enqueue_source_verify(c1, "url", "https://a.example/career", "待验")
    s2 = db.enqueue_source_verify(c2, "url", "https://b.example/career", "待验")
    s3 = db.enqueue_source_verify(c1, "url", "https://a.example/other", "待验")
    assert len(db.list_source_verify("pending", limit=50)) >= 3

    # 模拟 Admin._apply_source_resolve：拒绝两条
    for sid, cid in ((s1, c1), (s2, c2)):
        db.update_company_verify(cid, "rejected")
        db.resolve_source_verify(sid, "rejected")

    pending = {x["id"] for x in db.list_source_verify("pending", limit=50)}
    assert s1 not in pending
    assert s2 not in pending
    assert s3 in pending
    assert db.get_company(c1)["verify_status"] == "rejected"
    assert db.get_company(c2)["verify_status"] == "rejected"

    # 第三条标官方 + 同步种子
    sync = db.sync_company_seed_urls(c1, add_urls=["https://a.example/other"], set_official=True)
    assert sync.get("synced")
    db.resolve_source_verify(s3, "official")
    assert not db.list_source_verify("pending", limit=50)
    co = db.get_company(c1)
    assert co["verify_status"] == "official"
