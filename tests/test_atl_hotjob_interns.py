"""hotjob/wecruit interns.html：表格枚举、频道、公司名频道后缀、噪声过滤。"""

from __future__ import annotations

from pathlib import Path

from app.collector.adapters.generic import enumerate_job_table_rows
from app.collector.fill_from_url import parse_result_to_form_fields, resolve_jobs_from_url
from app.collector.filters import (
    is_noise_title,
    strip_recruit_channel_company_suffix,
)
from app.collector.portal_nav import channel_from_url, detect_recruit_channel

FIXTURES = Path(__file__).parent / "fixtures"
ATL_URL = (
    "https://wecruit.hotjob.cn/SU66c8012c1eb80543256bf330/pb/interns.html"
    "?projectCode=ATL2026"
)


def test_noise_rejects_schedule_and_template_titles():
    for t in (
        "校招行程",
        "招聘行程",
        "宣讲行程",
        "应聘指南",
        "了解我们",
        "职位搜索",
        "登录/注册",
        "{{item.postName}}",
        "{{item.postName}} · {{item.workPlace}}",
        "item.postName",
        "宁德新能源 (ATL) - 实习",
    ):
        assert is_noise_title(t), t
    assert not is_noise_title("电芯工艺实习生")
    assert not is_noise_title("质量管理实习生")


def test_strip_channel_suffix_from_company():
    assert (
        strip_recruit_channel_company_suffix("宁德新能源 (ATL) - 实习")
        == "宁德新能源 (ATL)"
    )
    assert (
        strip_recruit_channel_company_suffix("宁德新能源 (ATL)（实习）")
        == "宁德新能源 (ATL)"
    )
    assert strip_recruit_channel_company_suffix("某某公司实习生专项") == "某某公司"
    assert strip_recruit_channel_company_suffix("宁德新能源 (ATL)") == "宁德新能源 (ATL)"


def test_interns_html_channel_detection():
    html = (FIXTURES / "atl_hotjob_interns_list.html").read_text(encoding="utf-8")
    assert channel_from_url(ATL_URL) == "intern"
    ch, project, bucket = detect_recruit_channel(ATL_URL, html)
    assert ch == "intern"
    assert project == "实习生招聘"
    assert bucket == "日常实习"


def test_atl_table_enumerates_18_with_headcount_and_dates():
    html = (FIXTURES / "atl_hotjob_interns_list.html").read_text(encoding="utf-8")
    posts = enumerate_job_table_rows(
        ATL_URL,
        html,
        limit=40,
        channel_project="实习生招聘",
        channel_bucket="日常实习",
    )
    assert len(posts) == 18
    titles = {p.title for p in posts}
    assert "电芯工艺实习生" in titles
    assert "实验技术实习生" in titles
    assert "{{item.postName}}" not in titles
    assert "校招行程" not in titles
    by = {p.title: p for p in posts}
    assert by["电芯工艺实习生"].work_location == "宁德"
    assert by["电芯工艺实习生"].headcount == "5"
    assert "2026-07-18" in (by["电芯工艺实习生"].extras or {}).get("list_updated_at", "")
    assert by["设备工程实习生"].headcount == "3"
    assert all(p.recruit_project == "实习生招聘" for p in posts)
    assert all(p.recruit_bucket == "日常实习" for p in posts)
    assert all(p.apply_url and "postId=" in (p.apply_url or "") for p in posts)


def test_resolve_interns_portal_returns_18_not_empty():
    html = (FIXTURES / "atl_hotjob_interns_list.html").read_text(encoding="utf-8")
    result = resolve_jobs_from_url(
        ATL_URL,
        html=html,
        fetch=False,
        ocr_enabled=False,
        discover_portal=True,
        list_collect_months=6,
        list_limit=40,
        keep_company="宁德新能源 (ATL) - 实习",
    )
    assert result.ok, result.error
    assert result.total == 18
    titles = [c.fields.get("title") for c in result.candidates]
    assert "电芯工艺实习生" in titles
    assert "校招行程" not in titles
    assert not any("{{" in (t or "") for t in titles)
    for c in result.candidates:
        assert c.fields.get("company") == "宁德新能源 (ATL)"
    sample = next(c for c in result.candidates if c.fields.get("title") == "电芯工艺实习生")
    assert sample.fields.get("headcount") == "5"
    assert sample.fields.get("work_location") == "宁德"
    assert "2026-07-18" in (sample.fields.get("open_at") or "")
    assert sample.fields.get("apply_url")
    assert sample.fields.get("recruit_bucket") == "日常实习"


def test_form_fields_strip_keep_company_suffix():
    from app.collector.adapters.base import ParseResult

    pr = ParseResult(
        title="电芯工艺实习生",
        recruit_project="实习生招聘",
        recruit_bucket="日常实习",
        work_location="宁德",
        headcount="5",
        apply_url="https://wecruit.hotjob.cn/x/pb/position.html?postId=101",
        extras={"list_updated_at": "2026-07-18", "published_at": "2026-07-18"},
    )
    fields = parse_result_to_form_fields(
        pr,
        page_url=ATL_URL,
        keep_company="宁德新能源 (ATL) - 实习",
    )
    assert fields["company"] == "宁德新能源 (ATL)"
    assert fields["headcount"] == "5"
    assert fields["open_at"] == "2026-07-18"
