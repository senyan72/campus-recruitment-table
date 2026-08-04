"""异常队列实习岗转正常。"""

from pathlib import Path

from app.collector.promote_intern import promote_internship_from_review
from app.db.local import LocalDB


def test_promote_internship_from_review(tmp_path: Path):
    db = LocalDB(tmp_path / "pi.db")
    db.enqueue_review(
        "stale_over_3m",
        {
            "company": "测试公司",
            "title": "日常实习生",
            "source_url": "https://example.com/intern/1",
            "recruit_project": "日常实习",
            "recruit_bucket": "日常实习",
            "jd_text": "岗位职责：协助业务。" + "x" * 40,
        },
        "超3个月",
    )
    db.enqueue_review(
        "stale_over_3m",
        {
            "company": "测试公司",
            "title": "2023校招公告",
            "source_url": "https://example.com/old/1",
            "recruit_project": "秋招",
            "recruit_bucket": "校招",
        },
        "超3个月",
    )
    result = promote_internship_from_review(db)
    assert result["promoted"] == 1
    titles = {j["title"] for j in db.list_jobs(status="active")}
    assert "日常实习生" in titles
    assert "2023校招公告" not in titles
    pending = db.list_review_queue("pending")
    assert len(pending) == 1


def test_clear_review_queue(tmp_path: Path):
    db = LocalDB(tmp_path / "clr.db")
    db.enqueue_review("x", {"title": "a", "source_url": "https://a.com"}, "r")
    db.enqueue_review("y", {"title": "b", "source_url": "https://b.com"}, "r")
    assert db.clear_review_queue("pending") == 2
    assert db.list_review_queue("pending") == []
