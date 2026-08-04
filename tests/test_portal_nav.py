"""五矿类门户：频道识别、卡片列表、详情字段。"""

from pathlib import Path

from app.collector.adapters.router import parse_html
from app.collector.fill_from_url import enumerate_list_posts, resolve_jobs_from_url
from app.collector.filters import is_intern_hiring
from app.collector.label_fields import extract_labeled_fields, merge_jd_sections
from app.collector.portal_nav import (
    channel_from_url,
    derive_channel_list_url,
    detect_recruit_channel,
    enumerate_job_cards,
    expand_career_channel_urls,
    extract_channel_nav_links,
    extract_detail_heading_title,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_channel_from_url_path():
    assert channel_from_url("https://zhaopin.example.com/campus/list") == "campus"
    assert channel_from_url("https://zhaopin.example.com/intern/jobs") == "intern"
    assert channel_from_url("https://zhaopin.example.com/social/index") == "social"


def test_nav_active_campus_channel():
    html = (FIXTURES / "minmetals_portal_list.html").read_text(encoding="utf-8")
    ch, project, bucket = detect_recruit_channel("https://zhaopin.example.com/home", html)
    assert ch == "campus"
    assert project == "校园招聘"
    assert bucket == "校招"


def test_nav_active_social_not_campus():
    html = (FIXTURES / "minmetals_social_nav.html").read_text(encoding="utf-8")
    ch, project, bucket = detect_recruit_channel("https://zhaopin.example.com/social", html)
    assert ch == "social"
    assert project == "社会招聘"
    assert bucket is None


def test_enumerate_job_cards_three_posts():
    html = (FIXTURES / "minmetals_portal_list.html").read_text(encoding="utf-8")
    posts = enumerate_job_cards(
        "https://zhaopin.example.com/campus",
        html,
        channel_project="校园招聘",
        channel_bucket="校招",
    )
    titles = [p.title for p in posts]
    assert titles == ["科技研发岗", "财务共享岗", "冶金工艺岗"]
    assert posts[0].work_location == "长沙"
    assert posts[0].extras.get("company") == "长沙矿山研究院有限责任公司"
    assert posts[0].recruit_project == "校园招聘"
    assert posts[0].recruit_bucket == "校招"


def test_social_cards_not_campus_bucket():
    html = (FIXTURES / "minmetals_social_nav.html").read_text(encoding="utf-8")
    _, project, bucket = detect_recruit_channel("https://zhaopin.example.com/social", html)
    posts = enumerate_job_cards(
        "https://zhaopin.example.com/social",
        html,
        channel_project=project,
        channel_bucket=bucket,
    )
    assert len(posts) == 1
    assert posts[0].recruit_project == "社会招聘"
    assert posts[0].recruit_bucket != "校招"


def test_detail_heading_and_labeled_fields():
    html = (FIXTURES / "minmetals_portal_detail.html").read_text(encoding="utf-8")
    assert extract_detail_heading_title(html) == "科技研发岗"
    result = parse_html("https://zhaopin.example.com/campus/detail/101", html, ocr_enabled=False)
    assert result.title == "科技研发岗"
    assert result.work_location and "长沙" in result.work_location
    assert result.raw_category == "技术管理类"
    assert result.extras.get("company") == "长沙矿山研究院有限责任公司"
    assert result.recruit_project == "校园招聘"
    assert result.recruit_bucket == "校招"
    jd = result.jd_text or ""
    assert "专业要求" in jd
    assert "工作职责" in jd
    assert "任职要求" in jd
    assert "矿山智能装备" in jd


def test_merge_jd_includes_major():
    merged = merge_jd_sections(
        None,
        major="采矿工程",
        duties="做研发",
        requirements="硕士学历",
    )
    assert "专业要求" in merged
    assert "工作职责" in merged
    assert "任职要求" in merged


def test_resolve_jobs_from_portal_list():
    html = (FIXTURES / "minmetals_portal_list.html").read_text(encoding="utf-8")
    result = resolve_jobs_from_url(
        "https://zhaopin.example.com/campus",
        html=html,
        fetch=False,
        ocr_enabled=False,
    )
    assert result.ok
    assert result.total >= 3
    titles = [c.fields.get("title") for c in result.candidates]
    assert "科技研发岗" in titles
    assert result.candidates[0].fields.get("company") == "长沙矿山研究院有限责任公司" or any(
        c.fields.get("company") for c in result.candidates
    )


def test_enumerate_list_posts_uses_cards():
    html = (FIXTURES / "minmetals_portal_list.html").read_text(encoding="utf-8")
    _adapter, posts = enumerate_list_posts("https://zhaopin.example.com/campus", html)
    assert len(posts) >= 3


def test_extract_major_label():
    text = "专业要求：材料科学\n工作职责：研发\n任职要求：硕士\n"
    labels = extract_labeled_fields(text)
    assert labels.get("jd_major") == "材料科学"
    assert "研发" in (labels.get("jd_duties") or "")


def test_nav_active_intern_channel():
    html = (FIXTURES / "minmetals_intern_list.html").read_text(encoding="utf-8")
    ch, project, bucket = detect_recruit_channel("https://zhaopin.example.com/home", html)
    assert ch == "intern"
    assert project == "实习生招聘"
    assert bucket == "日常实习"


def test_extract_channel_nav_links_finds_intern():
    html = (FIXTURES / "minmetals_portal_list.html").read_text(encoding="utf-8")
    links = extract_channel_nav_links("https://zhaopin.example.com/campus", html)
    assert "intern" in links
    assert "/intern" in links["intern"]
    assert "campus" in links
    expanded = expand_career_channel_urls("https://zhaopin.example.com/campus", html)
    assert any("/intern" in u for u in expanded)
    assert any("/campus" in u for u in expanded)
    assert not any("/social" in u for u in expanded)


def test_intern_list_cards_and_bucket():
    html = (FIXTURES / "minmetals_intern_list.html").read_text(encoding="utf-8")
    _, project, bucket = detect_recruit_channel("https://zhaopin.example.com/intern", html)
    posts = enumerate_job_cards(
        "https://zhaopin.example.com/intern",
        html,
        channel_project=project,
        channel_bucket=bucket,
    )
    assert len(posts) == 2
    assert posts[0].title == "投资经理助理实习生"
    assert posts[0].extras.get("company") == "五矿证券有限公司"
    assert posts[0].recruit_project == "实习生招聘"
    assert posts[0].recruit_bucket == "日常实习"
    assert is_intern_hiring(
        title=posts[0].title,
        recruit_bucket=posts[0].recruit_bucket,
        recruit_project=posts[0].recruit_project,
    )


def test_intern_detail_seven_fields():
    html = (FIXTURES / "minmetals_intern_detail.html").read_text(encoding="utf-8")
    assert extract_detail_heading_title(html) == "投资经理助理实习生"
    result = parse_html("https://zhaopin.example.com/intern/detail/201", html, ocr_enabled=False)
    assert result.title == "投资经理助理实习生"
    assert result.work_location and "深圳" in result.work_location
    assert result.raw_category == "私募股权投资"
    assert result.extras.get("company") == "五矿证券有限公司"
    assert result.recruit_project == "实习生招聘"
    assert result.recruit_bucket == "日常实习"
    jd = result.jd_text or ""
    assert "专业要求" in jd and "金融" in jd
    assert "工作职责" in jd and "尽调" in jd
    assert "任职要求" in jd and "本科" in jd
    assert is_intern_hiring(
        title=result.title,
        recruit_bucket=result.recruit_bucket,
        recruit_project=result.recruit_project,
    )


def test_intern_detail_empty_sections_stay_empty():
    html = (FIXTURES / "minmetals_intern_detail_sparse.html").read_text(encoding="utf-8")
    result = parse_html("https://zhaopin.example.com/intern/detail/201", html, ocr_enabled=False)
    assert result.title == "投资经理助理实习生"
    assert result.extras.get("company") == "五矿证券有限公司"
    assert not result.work_location
    assert not result.raw_category
    labels = (result.extras or {}).get("labeled_fields") or {}
    assert "jd_major" not in labels
    assert "jd_duties" not in labels
    assert "jd_requirements" not in labels
    jd = result.jd_text or ""
    assert "专业要求" not in jd
    assert "工作职责" not in jd
    assert "任职要求" not in jd


def test_resolve_intern_list_from_url():
    html = (FIXTURES / "minmetals_intern_list.html").read_text(encoding="utf-8")
    result = resolve_jobs_from_url(
        "https://zhaopin.example.com/intern",
        html=html,
        fetch=False,
        ocr_enabled=False,
    )
    assert result.ok
    assert result.total >= 2
    titles = [c.fields.get("title") for c in result.candidates]
    assert "投资经理助理实习生" in titles
    assert any(c.fields.get("recruit_bucket") == "日常实习" for c in result.candidates)


def test_lc_style_cards_parse_date_and_headcount():
    """联储证券式卡片：岗位名/类别/地点/更新日期/招聘人数。"""
    html = (FIXTURES / "lc_style_career_list.html").read_text(encoding="utf-8")
    posts = enumerate_job_cards(
        "https://career.example.com/campus",
        html,
        channel_project="校园招聘",
        channel_bucket="校招",
    )
    assert len(posts) == 3
    assert posts[0].title == "销售交易岗"
    assert posts[0].work_location == "深圳"
    assert posts[0].raw_category == "销售类"
    assert posts[0].headcount == "3"
    assert "2026-06-10" in (posts[0].extras or {}).get("list_updated_at", "")
    assert posts[1].headcount == "2人" or posts[1].headcount == "2"
    assert posts[2].headcount == "若干"


def test_derive_list_url_from_detail():
    list_url, ch = derive_channel_list_url(
        "https://career.example.com/campus/detail/501"
    )
    assert ch == "campus"
    assert list_url.endswith("/campus")
    expanded = expand_career_channel_urls(
        "https://career.example.com/campus/detail/501",
        (FIXTURES / "lc_style_career_detail.html").read_text(encoding="utf-8"),
    )
    assert any(u.rstrip("/").endswith("/campus") for u in expanded)
    assert any("/intern" in u for u in expanded)
    assert not any("/social" in u for u in expanded)
