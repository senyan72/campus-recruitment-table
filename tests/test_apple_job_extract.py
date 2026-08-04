"""Apple 类搜索列表 / 详情：一岗一行、拒绝 chrome 标题、JD 去导航壳。"""

from pathlib import Path

from app.collector.adapters.generic import parse_generic
from app.collector.adapters.router import parse_html
from app.collector.fill_from_url import enumerate_list_posts
from app.collector.filters import (
    is_chrome_shell_title,
    is_noise_location,
    is_noise_title,
    recover_title_from_jd,
    sanitize_job_title,
    strip_career_nav_boilerplate,
)
from app.collector.label_fields import enrich_with_label_extraction
from app.collector.portal_nav import enumerate_detail_link_jobs, extract_detail_heading_title

FIXTURES = Path(__file__).parent / "fixtures"


def test_chrome_shell_titles_rejected():
    shells = [
        "Search Jobs - 中国内地 - 招贤纳才 (中国)",
        "招贤纳才",
        "Search Jobs",
        "Jobs at Apple",
        "招聘首页",
        "职位列表",
    ]
    for t in shells:
        assert is_chrome_shell_title(t) or is_noise_title(t), t
        assert sanitize_job_title(t) is None, t


def test_recover_title_keeps_job_code():
    jd = (
        "Overview\n"
        "Work at Apple\n"
        "CN-Store Leader 114438029\n"
        "地点：上海\n"
        "作为 Store Leader，你将带领零售团队。\n"
    )
    assert recover_title_from_jd(jd) == "CN-Store Leader 114438029"
    cleaned = strip_career_nav_boilerplate(jd)
    assert cleaned
    assert "Work at Apple" not in cleaned
    assert "Overview" not in cleaned
    assert "CN-Store Leader 114438029" in cleaned


def test_noise_location_team():
    assert is_noise_location("团队")
    assert is_noise_location("Team")
    assert not is_noise_location("上海")


def test_enumerate_apple_search_list_one_row_per_job():
    html = (FIXTURES / "apple_search_list.html").read_text(encoding="utf-8")
    base = "https://jobs.apple.com/zh-cn/search"
    posts = enumerate_detail_link_jobs(base, html, limit=20)
    titles = [p.title for p in posts]
    assert "CN-Store Leader" in titles
    assert "CN-Business Expert" in titles
    assert "CN-Specialist" in titles
    assert len(posts) == 3
    assert all(p.apply_url and "/details/" in (p.apply_url or "") for p in posts)
    assert all(not is_noise_title(p.title) for p in posts)

    _adapter, listed = enumerate_list_posts(base, html, limit=20)
    listed_titles = {p.title for p in listed}
    assert {"CN-Store Leader", "CN-Business Expert", "CN-Specialist"} <= listed_titles


def test_apple_detail_prefers_real_title_and_strips_nav_jd():
    html = (FIXTURES / "apple_job_detail.html").read_text(encoding="utf-8")
    url = "https://jobs.apple.com/zh-cn/details/114438029/cn-store-leader"
    assert extract_detail_heading_title(html) == "CN-Store Leader"

    result = parse_generic(url, html)
    # og:title 是 Search Jobs chrome，不得入库
    assert result.title != "Search Jobs - 中国内地 - 招贤纳才 (中国)"
    assert result.title in ("CN-Store Leader", "CN-Store Leader 114438029")
    assert result.jd_text
    assert "Work at Apple" not in (result.jd_text or "")
    assert "Explore working" not in (result.jd_text or "")
    assert "Related jobs" not in (result.jd_text or "")
    assert "Store Leader" in (result.jd_text or "") or "零售" in (result.jd_text or "")

    enriched = enrich_with_label_extraction(result, html, source_url=url)
    assert enriched.title in ("CN-Store Leader", "CN-Store Leader 114438029")
    assert not is_noise_title(enriched.title)
    if enriched.work_location:
        assert not is_noise_location(enriched.work_location)

    routed = parse_html(url, html)
    assert routed.title in ("CN-Store Leader", "CN-Store Leader 114438029")
