"""时间窗、is_job_posting、噪声/公司名+校园招聘标题。"""

from datetime import date, timedelta
from pathlib import Path

from app.collector.adapters import feishu, moka
from app.collector.cleanup import cleanup_noise_and_duplicate_jobs
from app.collector.extract import should_auto_publish
from app.collector.filters import (
    classify_freshness,
    has_internship_signal,
    is_company_plus_recruit_title,
    is_job_posting,
    is_noise_title,
    keep_job_by_freshness,
    resolve_lookback_days,
)
from app.collector.website import extract_job_links
from app.db.local import LocalDB

FIXTURES = Path(__file__).parent / "fixtures"


def test_resolve_lookback_days():
    assert resolve_lookback_days() == 90
    assert resolve_lookback_days(collect_months=3) == 90
    assert resolve_lookback_days(lookback_days=60) == 60
    assert resolve_lookback_days(lookback_days=60, collect_months=6) == 60


def test_keep_job_by_freshness_deadline_and_stale():
    today = date(2026, 8, 1)
    ok, reason = keep_job_by_freshness(deadline="2026-07-01", today=today)
    assert not ok and "截止" in reason

    old = (today - timedelta(days=120)).isoformat()
    ok2, reason2 = keep_job_by_freshness(
        published_at=old, title="后端开发", jd_text="岗位职责：" + "x" * 50, today=today
    )
    assert not ok2
    assert "超过" in reason2 or "早于" in reason2 or "异常" in reason2

    status, _ = classify_freshness(
        published_at=old, title="后端开发", jd_text="岗位职责：" + "x" * 50, today=today
    )
    assert status == "stale_anomaly"

    ok3, _ = keep_job_by_freshness(
        published_at=old,
        title="后端开发（在招）",
        jd_text="正在招聘，欢迎投递。" + "x" * 40,
        today=today,
    )
    assert ok3

    recent = (today - timedelta(days=10)).isoformat()
    ok4, _ = keep_job_by_freshness(published_at=recent, today=today)
    assert ok4
    assert classify_freshness(published_at=recent, today=today)[0] == "ok"

    # 实习岗即使发布时间超 3 个月也纳入正常
    old = (today - timedelta(days=120)).isoformat()
    st, reason = classify_freshness(
        published_at=old,
        title="职能实习生（可转正）",
        jd_text="日常实习，欢迎投递",
        recruit_bucket="日常实习",
        today=today,
    )
    assert st == "ok"
    assert "实习" in reason
    assert has_internship_signal("暑期实习生招聘")


def test_is_job_posting_rejects_noise_and_company_shell():
    ok, conf, reason = is_job_posting(title="个人中心", source_url="https://x.com/login")
    assert not ok and conf < 0.2

    ok2, _, reason2 = is_job_posting(
        title="晨光生物校园招聘",
        source_url="https://x.com/campus",
        company="晨光生物",
        jd_text="欢迎投递",
    )
    assert not ok2
    assert "公司名" in reason2 or "校园招聘" in reason2

    assert is_company_plus_recruit_title("字节跳动", "字节跳动 校园招聘入口")
    assert is_company_plus_recruit_title("测试公司", "测试公司校招")
    assert not is_company_plus_recruit_title("字节跳动", "后端开发工程师")

    jd = "岗位职责：负责后端服务开发与维护。任职要求：熟悉 Python。" + "详" * 20
    ok3, conf3, _ = is_job_posting(
        title="后端开发工程师",
        source_url="https://demo.jobs.feishu.cn/campus/position/1/detail",
        jd_text=jd,
        company="某科技",
    )
    assert ok3 and conf3 >= 0.55


def test_should_auto_publish_allows_portal_not_only_specific_jobs():
    """异常不以是否列出具体岗位为准：公司校招入口在新鲜时可自动发布。"""
    jd = "岗位职责：参与产品设计。" + "内容" * 30
    ok, reason = should_auto_publish(
        verify_status="official",
        title="晨光生物校园招聘",
        source_url="https://example.com/a",
        recruit_project="秋招",
        confidence=0.9,
        aggregator=False,
        company="晨光生物",
        jd_text=jd,
        require_job_posting=False,
    )
    assert ok, reason

    ok2, _ = should_auto_publish(
        verify_status="official",
        title="算法实习生",
        source_url="https://demo.jobs.feishu.cn/campus/position/9/detail",
        recruit_project="实习",
        confidence=0.85,
        aggregator=False,
        company="某公司",
        jd_text=jd,
    )
    assert ok2


def test_cleanup_keeps_portal_title_but_removes_closed(tmp_path: Path):
    db = LocalDB(tmp_path / "shell.db")
    cid = "c1"
    db.upsert_company({"id": cid, "name": "晨光生物", "verify_status": "official"})
    db.upsert_job(
        {
            "company_id": cid,
            "company": "晨光生物",
            "title": "晨光生物校园招聘",
            "source_url": "https://a.example.com/portal",
            "recruit_project": "校园招聘",
            "recruit_bucket": "校招",
            "confidence": 0.6,
            "status": "active",
        }
    )
    db.upsert_job(
        {
            "company_id": cid,
            "company": "晨光生物",
            "title": "当前网页已关停",
            "source_url": "https://a.example.com/closed",
            "recruit_project": "秋招",
            "recruit_bucket": "校招",
            "confidence": 0.2,
            "status": "active",
        }
    )
    result = cleanup_noise_and_duplicate_jobs(db)
    assert result["company_recruit_shell"] == 0
    titles = {j["title"] for j in db.list_jobs(status="active")}
    assert "晨光生物校园招聘" in titles
    assert "当前网页已关停" not in titles


def test_feishu_fixture_is_job_posting():
    html = (FIXTURES / "feishu_detail.html").read_text(encoding="utf-8")
    url = "https://demo.jobs.feishu.cn/campus/position/10001/detail"
    result = feishu.parse_feishu_detail(url, html)
    ok, conf, _ = is_job_posting(
        title=result.title,
        source_url=url,
        jd_text=result.jd_text,
        company="演示公司",
    )
    assert ok and conf >= 0.55
    assert result.work_location == "北京"


def test_moka_list_extract_and_noise():
    html = """
    <html><body>
      <a href="/login">登录</a>
      <a href="/job/1001">后端开发工程师</a>
      <a href="/jobs/2002/detail">产品经理实习</a>
      <a href="/account">个人中心</a>
    </body></html>
    """
    links = moka.extract_detail_links("https://app.mokahr.com/campus", html)
    assert any("1001" in u or "2002" in u for u in links)
    assert not any("login" in u for u in links)
    assert is_noise_title("提示")


def test_generic_job_links_filter():
    html = """
    <a href="/career">校园招聘</a>
    <a href="/position/1/detail">Java开发</a>
    <a href="/user/center">个人中心</a>
    """
    links = extract_job_links("https://corp.example.com", html)
    assert len(links) == 1
    assert "position/1/detail" in links[0]
