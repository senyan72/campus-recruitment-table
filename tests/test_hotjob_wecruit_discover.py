"""wecruit/hotjob school.html：SPA 壳不得当岗位；API 枚举校招/实习多岗。"""

from __future__ import annotations

import json
from pathlib import Path

from app.collector.adapters import hotjob
from app.collector.fill_from_url import (
    discover_portal_jobs_from_url,
    fields_look_like_no_job_posting,
    resolve_jobs_from_url,
)
from app.collector.filters import is_chrome_shell_title, is_noise_title
from app.collector.portal_nav import expand_career_channel_urls, hotjob_pb_channel_urls

FIXTURES = Path(__file__).parent / "fixtures"
SCHOOL_URL = "https://wecruit.hotjob.cn/SU62e0f5dd0dcad44de6f2662a/pb/school.html"
INTERNS_URL = "https://wecruit.hotjob.cn/SU62e0f5dd0dcad44de6f2662a/pb/interns.html"
TENANT = "SU62e0f5dd0dcad44de6f2662a"


def _campus_payload() -> dict:
    return json.loads((FIXTURES / "hotjob_lczq_campus_list.json").read_text(encoding="utf-8"))


def _intern_payload() -> dict:
    return json.loads((FIXTURES / "hotjob_lczq_intern_list.json").read_text(encoding="utf-8"))


def test_ie_browser_tip_is_noise_not_job():
    tip = "✅ Chrome | ✅ Firefox | ✅ Edge | ✅ Safari"
    assert is_noise_title(tip)
    assert is_chrome_shell_title(tip)
    assert fields_look_like_no_job_posting(
        {
            "title": tip,
            "jd_text": "【温馨提示】检测到您正在使用兼容模式/旧版IE浏览器，功能可能无法正常使用。",
        }
    )


def test_spa_shell_not_usable_job_without_api(monkeypatch):
    html = (FIXTURES / "hotjob_wecruit_school_shell.html").read_text(encoding="utf-8")
    assert hotjob.is_spa_list_shell(html)
    parsed = hotjob.parse_hotjob(SCHOOL_URL, html)
    assert parsed.extras.get("is_list") is True
    assert not parsed.title
    monkeypatch.setattr(hotjob, "fetch_position_list", lambda *a, **k: {})
    # 无 API 时不得把 IE 提示当成 1 条岗位
    result = resolve_jobs_from_url(
        SCHOOL_URL,
        html=html,
        fetch=False,
        ocr_enabled=False,
        discover_portal=True,
        list_limit=40,
    )
    assert not result.ok or result.total == 0
    assert result.no_job_posting or not result.candidates


def test_hotjob_pb_sibling_urls():
    links = hotjob_pb_channel_urls(SCHOOL_URL)
    assert links["campus"].endswith("/pb/school.html")
    assert links["intern"].endswith("/pb/interns.html")
    assert "social" in links
    expanded = expand_career_channel_urls(SCHOOL_URL, "<html></html>")
    assert any(u.endswith("/pb/school.html") for u in expanded)
    assert any(u.endswith("/pb/interns.html") for u in expanded)
    assert not any(u.endswith("/pb/social.html") for u in expanded)
    # posDetail 也要能合成频道（迪卡侬等从详情重采）
    detail = f"https://wecruit.hotjob.cn/{TENANT}/pb/posDetail.html?postId=abc&postType=intern"
    d_links = hotjob_pb_channel_urls(detail)
    assert d_links["intern"].endswith("/pb/interns.html")
    d_exp = expand_career_channel_urls(detail, "<html><div id='root'></div></html>")
    assert any(u.endswith("/pb/interns.html") for u in d_exp)
    assert any(u.endswith("/pb/school.html") for u in d_exp)


def test_row_to_result_maps_fields():
    row = _campus_payload()["data"]["pageForm"]["pageData"][0]
    pr = hotjob.row_to_result(row, tenant=TENANT, list_url=SCHOOL_URL, channel="campus")
    assert pr is not None
    assert "销售交易岗" in (pr.title or "")
    assert pr.work_location and "深圳" in pr.work_location
    assert pr.headcount == "若干"
    assert pr.recruit_project == "校园招聘"
    assert pr.recruit_bucket == "校招"
    assert pr.extras.get("company") == "联储证券"
    assert "2026-05-28" in (pr.extras or {}).get("list_updated_at", "")
    assert pr.apply_url and "posDetail.html" in pr.apply_url and "postId=" in pr.apply_url
    assert "postType=school" in (pr.apply_url or "")
    assert pr.extras.get("portal_channel") == "campus"


def test_enumerate_campus_from_api_fixture(monkeypatch):
    payload = _campus_payload()

    def fake_fetch(tenant, recruit_type, **kwargs):
        assert tenant == TENANT
        assert int(recruit_type) == 1
        return payload

    monkeypatch.setattr(hotjob, "fetch_position_list", fake_fetch)
    posts = hotjob.enumerate_positions(SCHOOL_URL, limit=40)
    assert len(posts) == 16
    titles = {p.title for p in posts}
    assert any("销售交易岗" in (t or "") for t in titles)
    assert all(p.apply_url and "postId=" in (p.apply_url or "") for p in posts)
    assert all(p.extras.get("company") == "联储证券" for p in posts)


def test_discover_portal_returns_campus_and_intern(monkeypatch):
    campus = _campus_payload()
    intern = _intern_payload()
    shell = (FIXTURES / "hotjob_wecruit_school_shell.html").read_text(encoding="utf-8")

    def fake_fetch(tenant, recruit_type, **kwargs):
        rt = int(recruit_type)
        if rt == 1:
            return campus
        if rt == 12:
            return intern
        return {"state": "200", "data": {"pageForm": {"pageData": [], "totalPage": 0}}}

    monkeypatch.setattr(hotjob, "fetch_position_list", fake_fetch)

    def fake_html(url: str, timeout: float = 60.0) -> str:
        return shell

    result = discover_portal_jobs_from_url(
        SCHOOL_URL,
        html=shell,
        fetch=True,
        ocr_enabled=False,
        list_limit=200,
        list_collect_months=24,
        fetch_html=fake_html,
        keep_company="联储证券",
    )
    assert result.ok, result.error
    assert result.total >= 16
    titles = [c.fields.get("title") or "" for c in result.candidates]
    assert any("销售交易岗" in t for t in titles)
    assert any("实习生" in t or "实习" in t for t in titles)
    assert not any("Chrome" in t for t in titles)
    assert not any("温馨提示" in (c.fields.get("jd_text") or "") for c in result.candidates)
    # 申请链优先详情
    sample = next(c for c in result.candidates if "销售交易岗" in (c.fields.get("title") or ""))
    assert "posDetail.html" in (sample.fields.get("apply_url") or "")
    assert sample.fields.get("company") == "联储证券"
    assert sample.fields.get("work_location")
    assert sample.fields.get("headcount")


def test_form_urlencoded_not_json_required():
    """文档化约束：API 需 form；此处仅校验 helper 默认头。"""
    assert "x-www-form-urlencoded" in hotjob.DEFAULT_HEADERS.get("Content-Type", "")
    # 浏览器同源优先；www 仅为回退
    assert "wecruit.hotjob.cn" in hotjob.API_HOST
    assert hotjob.API_HOSTS[0].endswith("wecruit.hotjob.cn")
