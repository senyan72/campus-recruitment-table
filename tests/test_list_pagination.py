"""列表翻页：下一页链接 + 近 6 个月更新日期停止条件。"""

from datetime import date
from pathlib import Path

from app.collector.filters import list_date_cutoff, resolve_list_lookback_months
from app.collector.portal_nav import (
    enumerate_list_with_pagination,
    filter_posts_by_list_window,
    find_next_page_url,
    should_paginate_channel,
)
from app.collector.adapters.base import ParseResult

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 8, 2)


def _pages() -> dict[str, str]:
    return {
        "https://zhaopin.example.com/campus": (FIXTURES / "minmetals_campus_list_p1.html").read_text(
            encoding="utf-8"
        ),
        "https://zhaopin.example.com/campus?page=2": (
            FIXTURES / "minmetals_campus_list_p2.html"
        ).read_text(encoding="utf-8"),
        "https://zhaopin.example.com/campus?page=3": (
            FIXTURES / "minmetals_campus_list_p3.html"
        ).read_text(encoding="utf-8"),
        "https://zhaopin.example.com/intern": (FIXTURES / "minmetals_intern_list_p1.html").read_text(
            encoding="utf-8"
        ),
        "https://zhaopin.example.com/intern?page=2": (
            FIXTURES / "minmetals_intern_list_p2_all_old.html"
        ).read_text(encoding="utf-8"),
    }


def test_resolve_list_lookback_months_default_six():
    assert resolve_list_lookback_months(None) == 6
    assert resolve_list_lookback_months(0) == 6
    assert resolve_list_lookback_months(4) == 4
    assert list_date_cutoff(months=6, today=TODAY) == date(2026, 2, 3)


def test_should_paginate_campus_and_intern_only():
    assert should_paginate_channel("campus")
    assert should_paginate_channel("intern")
    assert not should_paginate_channel("social")
    assert not should_paginate_channel(None)


def test_find_next_page_from_gt_link():
    html = (FIXTURES / "minmetals_campus_list_p1.html").read_text(encoding="utf-8")
    nxt = find_next_page_url("https://zhaopin.example.com/campus", html)
    assert nxt is not None
    assert "page=2" in nxt


def test_find_next_page_disabled_returns_none():
    html = (FIXTURES / "minmetals_campus_list_p3.html").read_text(encoding="utf-8")
    nxt = find_next_page_url("https://zhaopin.example.com/campus?page=3", html)
    assert nxt is None


def test_filter_boundary_keeps_in_window_and_stops():
    posts = [
        ParseResult(
            title="新",
            parse_status="ok",
            confidence=0.5,
            extras={"list_updated_at": "2026-04-01"},
        ),
        ParseResult(
            title="旧",
            parse_status="ok",
            confidence=0.5,
            extras={"list_updated_at": "2025-11-01"},
        ),
    ]
    kept, stop = filter_posts_by_list_window(posts, months=6, today=TODAY)
    assert [p.title for p in kept] == ["新"]
    assert stop is True


def test_filter_all_old_stops_empty():
    posts = [
        ParseResult(
            title="旧1",
            parse_status="ok",
            confidence=0.5,
            extras={"published_at": "2025-10-01"},
        ),
        ParseResult(
            title="旧2",
            parse_status="ok",
            confidence=0.5,
            extras={"published_at": "2025-09-01"},
        ),
    ]
    kept, stop = filter_posts_by_list_window(posts, months=6, today=TODAY)
    assert kept == []
    assert stop is True


def test_paginate_campus_stops_on_boundary_page():
    pages = _pages()
    fetched: list[str] = []

    def fetch_page(u: str) -> str:
        fetched.append(u)
        # normalize key
        key = u.split("#")[0]
        if key not in pages and key.rstrip("/") in pages:
            key = key.rstrip("/")
        return pages[key]

    html = pages["https://zhaopin.example.com/campus"]
    posts = enumerate_list_with_pagination(
        "https://zhaopin.example.com/campus",
        html,
        fetch_page=fetch_page,
        lookback_months=6,
        today=TODAY,
        channel_project="校园招聘",
        channel_bucket="校招",
    )
    titles = [p.title for p in posts]
    assert "智能制造岗" in titles
    assert "财务共享岗" in titles
    assert "冶金工艺岗" in titles
    assert "边界窗内岗" in titles
    assert "窗外过旧岗" not in titles
    assert "更旧岗A" not in titles
    # 第2页出现窗外日期后不再请求第3页
    assert not any("page=3" in u for u in fetched)
    assert any("page=2" in u for u in fetched)


def test_pagination_stops_when_page_all_known():
    """整页岗均已入库时不再翻下一页（深度采集加速）。"""
    pages = _pages()
    fetched: list[str] = []

    def fetch_page(u: str) -> str:
        fetched.append(u)
        key = u.split("#")[0]
        if key not in pages and key.rstrip("/") in pages:
            key = key.rstrip("/")
        return pages[key]

    html = pages["https://zhaopin.example.com/campus"]
    posts = enumerate_list_with_pagination(
        "https://zhaopin.example.com/campus",
        html,
        fetch_page=fetch_page,
        lookback_months=6,
        today=TODAY,
        channel_project="校园招聘",
        channel_bucket="校招",
        known_urls=set(),
        known_titles={"智能制造岗", "财务共享岗"},
    )
    assert len(posts) == 2
    assert not any("page=2" in u for u in fetched)


def test_paginate_intern_stops_when_page_all_old():
    pages = _pages()
    fetched: list[str] = []

    def fetch_page(u: str) -> str:
        fetched.append(u)
        return pages[u.split("#")[0]]

    html = pages["https://zhaopin.example.com/intern"]
    posts = enumerate_list_with_pagination(
        "https://zhaopin.example.com/intern",
        html,
        fetch_page=fetch_page,
        lookback_months=6,
        today=TODAY,
        channel_project="实习生招聘",
        channel_bucket="日常实习",
    )
    titles = [p.title for p in posts]
    assert titles == ["投资经理助理实习生"]
    assert "过旧实习岗" not in titles
    assert any("page=2" in u for u in fetched)


def test_single_page_without_fetch_keeps_all_cards():
    """无 fetch 时不按日期窗裁剪，兼容单页站 / 离线 HTML。"""
    html = (FIXTURES / "minmetals_portal_list.html").read_text(encoding="utf-8")
    posts = enumerate_list_with_pagination(
        "https://zhaopin.example.com/campus",
        html,
        fetch_page=None,
        lookback_months=6,
        today=TODAY,
        channel_project="校园招聘",
        channel_bucket="校招",
    )
    assert len(posts) == 3
