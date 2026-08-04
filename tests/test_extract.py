from pathlib import Path

from app.collector.adapters.router import parse_html
from app.collector.extract import (
    compute_confidence,
    is_source_trust_reject,
    route_auto_publish_failure,
    should_auto_publish,
)
from app.db.local import LocalDB


def test_should_auto_publish_rules():
    jd = "岗位职责：负责后端开发与维护。任职要求：熟悉 Python。" + "详" * 20
    ok, _ = should_auto_publish(
        verify_status="official",
        title="后端开发工程师",
        source_url="https://example.com/job/1",
        recruit_project="秋招",
        confidence=0.8,
        aggregator=False,
        jd_text=jd,
        company="示例科技",
    )
    assert ok
    ok2, reason = should_auto_publish(
        verify_status="unverified",
        title="后端开发工程师",
        source_url="https://example.com/job/1",
        recruit_project="秋招",
        confidence=0.9,
        aggregator=False,
        jd_text=jd,
        company="示例科技",
    )
    assert not ok2
    assert "官方" in reason
    assert is_source_trust_reject(reason)


def test_route_unverified_source_goes_to_source_verify(tmp_path: Path):
    db = LocalDB(tmp_path / "r.db")
    cid = db.upsert_company({"name": "路由测试公司", "verify_status": "unverified"})
    company = db.get_company(cid) or {"id": cid, "name": "路由测试公司"}
    job = {
        "company_id": cid,
        "company": "路由测试公司",
        "title": "后端开发工程师",
        "source_url": "https://example.com/career/1",
        "apply_url": "https://example.com/apply/1",
        "recruit_project": "校园招聘",
    }
    stats = {"queued": 0, "skipped": 0}
    route_auto_publish_failure(db, company, job, "源未验证为官方", stats)
    assert stats["skipped"] == 1
    assert stats["queued"] == 0
    assert len(db.list_review_queue("pending", limit=50)) == 0
    srcs = db.list_source_verify("pending", limit=50)
    assert len(srcs) == 1
    assert srcs[0]["source_value"] == "https://example.com/career/1"

    # 内容问题进异常队列，不进源验证（同 URL 去重后源验证仍为 1）
    stats2 = {"queued": 0, "skipped": 0}
    route_auto_publish_failure(db, company, job, "缺少硬字段（单位侧标题/原文/招聘项目）", stats2)
    assert stats2["queued"] == 1
    reviews = db.list_review_queue("pending", limit=50)
    assert len(reviews) == 1
    assert reviews[0]["kind"] == "missing_fields"


def test_confidence_and_generic_parse():
    html = """
    <html><head><meta property="og:title" content="某某公司2026届校园招聘"/>
    <title>x</title></head>
    <body><article>""" + ("岗位职责与任职要求。" * 20) + """</article></body></html>
    """
    result = parse_html("https://careers.example.com/campus/1", html)
    assert result.title and "校园招聘" in result.title
    assert result.recruit_bucket == "校招"
    conf = compute_confidence(
        title=result.title,
        source_url="https://careers.example.com/campus/1",
        jd_text=result.jd_text,
        recruit_bucket=result.recruit_bucket,
        official_source=True,
        aggregator=False,
    )
    assert conf >= 0.7
