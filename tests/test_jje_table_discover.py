"""精进电动式表格列表：列解析、半年窗标注、门户发现不静默失败。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.collector.adapters.generic import enumerate_job_table_rows
from app.collector.fill_from_url import apply_reidentify_fields, resolve_jobs_from_url
from app.collector.filters import is_chrome_shell_title, sanitize_job_title
from app.collector.portal_nav import filter_posts_by_list_window
from app.ui.job_editor import selected_fill_candidates

FIXTURES = Path(__file__).parent / "fixtures"


def test_jje_table_parses_headcount_date_and_work_type():
    html = (FIXTURES / "jje_style_job_table.html").read_text(encoding="utf-8")
    posts = enumerate_job_table_rows("https://career.jje.example/campus", html)
    assert len(posts) == 3
    by = {p.title: p for p in posts}
    assert by["电机控制系统工程师"].work_location == "上海"
    assert by["电机控制系统工程师"].headcount == "2"
    assert by["电机控制系统工程师"].raw_category == "研发类"
    assert "2024-07-15" in (by["电机控制系统工程师"].extras or {}).get(
        "list_updated_at", ""
    )
    assert by["工艺工程师（应届）"].headcount == "若干"
    assert "2026-06-20" in (by["工艺工程师（应届）"].extras or {}).get(
        "list_updated_at", ""
    )


def test_keep_outside_marks_but_does_not_drop():
    html = (FIXTURES / "jje_style_job_table.html").read_text(encoding="utf-8")
    posts = enumerate_job_table_rows("https://career.jje.example/campus", html)
    kept, stop = filter_posts_by_list_window(
        posts, months=6, today=date(2026, 8, 2), keep_outside=True
    )
    assert stop is True
    assert len(kept) == 3
    outside = [p for p in kept if (p.extras or {}).get("outside_lookback")]
    inside = [p for p in kept if not (p.extras or {}).get("outside_lookback")]
    assert len(outside) == 2
    assert len(inside) == 1
    assert inside[0].title == "工艺工程师（应届）"


def test_discover_portal_lists_outside_not_empty_fail():
    page1 = (FIXTURES / "jje_style_job_table.html").read_text(encoding="utf-8")
    page2 = (FIXTURES / "jje_style_job_table_p2.html").read_text(encoding="utf-8")

    def fake_fetch(u: str, timeout: float = 45.0) -> str:  # noqa: ARG001
        if "page=2" in (u or ""):
            return page2
        return page1

    result = resolve_jobs_from_url(
        "https://career.jje.example/campus",
        html=page1,
        fetch=False,
        ocr_enabled=False,
        discover_portal=True,
        list_collect_months=6,
        list_limit=200,
        fetch_html=fake_fetch,
        keep_company="精进电动",
    )
    assert result.ok, result.error
    assert result.total >= 3
    assert result.outside_lookback_count >= 2
    assert result.notice and "超出近半年" in result.notice
    titles = [c.fields.get("title") for c in result.candidates]
    assert "工艺工程师（应届）" in titles
    assert "电机控制系统工程师" in titles
    # 窗外默认不在「仅窗内预勾」逻辑里；多选只导入勾选
    outside = [c for c in result.candidates if c.outside_lookback]
    assert outside
    assert all("超出近半年" in (c.label or "") for c in outside)
    only = selected_fill_candidates(result.candidates, [0])
    assert len(only) == 1


def test_shell_招聘官网_rejected():
    assert is_chrome_shell_title("中国五矿集团招聘官网")
    assert sanitize_job_title("中国五矿集团招聘官网") is None
    assert is_chrome_shell_title("精进电动招聘官网")


def test_reidentify_group_and_company_minmetals():
    before = {
        "company": "中国五矿",
        "group_name": "",
        "title": "中国五矿集团招聘官网",
        "source_url": "https://zhaopin.example.com/campus/detail/1",
        "apply_url": "https://zhaopin.example.com/campus/detail/1",
    }
    filled = {
        "title": "投资经理",
        "company": "五矿资产管理有限公司",
        "group_name": "中国五矿集团",
        "recruit_project": "校园招聘",
        "recruit_bucket": "校招",
        "source_url": "https://zhaopin.example.com/campus/detail/1",
        "apply_url": "https://zhaopin.example.com/campus/detail/1",
        "jd_text": "工作职责：负责投资项目。",
    }
    merged = apply_reidentify_fields(
        before, filled, seed_company_name="中国五矿集团有限公司"
    )
    assert merged.get("group_name") == "中国五矿集团"
    assert merged.get("company") == "五矿资产管理有限公司"
    assert merged.get("title") == "投资经理"
