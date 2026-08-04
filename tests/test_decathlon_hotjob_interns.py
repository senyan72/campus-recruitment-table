"""迪卡侬中国 wecruit/hotjob 实习：posDetail 上卷 + API 枚举多岗，SPA 壳不当岗位。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.collector.adapters import hotjob
from app.collector.fill_from_url import (
    discover_portal_jobs_from_url,
    parse_result_to_form_fields,
    resolve_jobs_from_url,
)
from app.collector.portal_nav import (
    channel_from_url,
    derive_channel_list_url,
    expand_career_channel_urls,
    hotjob_pb_channel_urls,
)

FIXTURES = Path(__file__).parent / "fixtures"
TENANT = "SU64631fe6bef57c0907f133c4"
INTERNS_URL = f"https://wecruit.hotjob.cn/{TENANT}/pb/interns.html"
SCHOOL_URL = f"https://wecruit.hotjob.cn/{TENANT}/pb/school.html"
DETAIL_URL = (
    f"https://wecruit.hotjob.cn/{TENANT}/pb/posDetail.html"
    "?postId=6a6c060aba2dc64cbb941064&postType=intern"
)


def _intern_payload() -> dict:
    return json.loads(
        (FIXTURES / "hotjob_decathlon_intern_list.json").read_text(encoding="utf-8")
    )


def _campus_payload() -> dict:
    return json.loads(
        (FIXTURES / "hotjob_decathlon_campus_list.json").read_text(encoding="utf-8")
    )


def _shell() -> str:
    return (FIXTURES / "hotjob_decathlon_interns_shell.html").read_text(encoding="utf-8")


def test_posdetail_expands_to_interns_and_school():
    assert channel_from_url(DETAIL_URL) == "intern"
    links = hotjob_pb_channel_urls(DETAIL_URL)
    assert links["intern"].endswith("/pb/interns.html")
    assert links["campus"].endswith("/pb/school.html")
    list_u, ch = derive_channel_list_url(DETAIL_URL)
    assert ch == "intern"
    assert list_u and list_u.endswith("/pb/interns.html")
    expanded = expand_career_channel_urls(DETAIL_URL, _shell())
    assert any(u.endswith("/pb/interns.html") for u in expanded)
    assert any(u.endswith("/pb/school.html") for u in expanded)


def test_spa_shell_not_one_chrome_job(monkeypatch):
    """SPA 壳无表/卡时：无 API 补充不得把 IE/Chrome 提示当岗位。"""
    html = _shell()
    assert hotjob.is_spa_list_shell(html)
    monkeypatch.setattr(hotjob, "fetch_position_list", lambda *a, **k: {})
    result = resolve_jobs_from_url(
        INTERNS_URL,
        html=html,
        fetch=False,
        ocr_enabled=False,
        discover_portal=True,
        list_limit=40,
    )
    assert not result.ok or result.total == 0
    titles = [c.fields.get("title") or "" for c in result.candidates]
    assert not any("Chrome" in t for t in titles)


def test_enumerate_interns_from_api_fixture(monkeypatch):
    payload = _intern_payload()

    def fake_fetch(tenant, recruit_type, **kwargs):
        assert tenant == TENANT
        assert int(recruit_type) == 12
        return payload

    monkeypatch.setattr(hotjob, "fetch_position_list", fake_fetch)
    posts = hotjob.enumerate_positions(INTERNS_URL, limit=40)
    assert len(posts) == 5
    titles = {p.title for p in posts}
    assert "色彩与平面实习生" in titles
    assert "迪卡侬零售支持财务分析部门实习生" in titles
    assert all(p.apply_url and "posDetail.html" in (p.apply_url or "") for p in posts)
    assert all("postType=intern" in (p.apply_url or "") for p in posts)
    sample = next(p for p in posts if p.title == "色彩与平面实习生")
    fields = parse_result_to_form_fields(sample, page_url=INTERNS_URL, keep_company="迪卡侬中国")
    assert fields.get("open_at", "").startswith("2026-07-15")
    assert "posDetail.html" in (fields.get("apply_url") or "")
    assert "postType=intern" in (fields.get("apply_url") or "")
    assert fields.get("company") == "迪卡侬中国"


def test_enumerate_from_posdetail_uses_intern_channel(monkeypatch):
    payload = _intern_payload()
    seen_referers: list[str] = []

    def fake_fetch(tenant, recruit_type, **kwargs):
        assert int(recruit_type) == 12
        seen_referers.append(str(kwargs.get("list_url") or ""))
        return payload

    monkeypatch.setattr(hotjob, "fetch_position_list", fake_fetch)
    posts = hotjob.enumerate_positions(DETAIL_URL, limit=40)
    assert len(posts) == 5
    assert any(r.endswith("/pb/interns.html") for r in seen_referers)


def test_discover_from_posdetail_returns_multiple_interns(monkeypatch):
    intern = _intern_payload()
    campus = _campus_payload()
    shell = _shell()

    def fake_fetch(tenant, recruit_type, **kwargs):
        rt = int(recruit_type)
        if rt == 12:
            return intern
        if rt == 1:
            return campus
        return {"state": "200", "data": {"pageForm": {"pageData": [], "totalPage": 0}}}

    monkeypatch.setattr(hotjob, "fetch_position_list", fake_fetch)

    def fake_html(url: str, timeout: float = 60.0) -> str:
        return shell

    result = discover_portal_jobs_from_url(
        DETAIL_URL,
        html=shell,
        fetch=True,
        ocr_enabled=False,
        list_limit=200,
        list_collect_months=6,
        fetch_html=fake_html,
        keep_company="迪卡侬中国",
    )
    assert result.ok, result.error
    assert result.total >= 5
    titles = [c.fields.get("title") or "" for c in result.candidates]
    assert "色彩与平面实习生" in titles
    assert "迪卡侬零售支持财务分析部门实习生" in titles
    assert not any("Chrome" in t for t in titles)
    sample = next(c for c in result.candidates if c.fields.get("title") == "色彩与平面实习生")
    assert "posDetail.html" in (sample.fields.get("apply_url") or "")
    assert (sample.fields.get("open_at") or "").startswith("2026-07-15")


def test_discover_from_interns_list(monkeypatch):
    intern = _intern_payload()
    campus = _campus_payload()
    shell = _shell()

    def fake_fetch(tenant, recruit_type, **kwargs):
        rt = int(recruit_type)
        if rt == 12:
            return intern
        if rt == 1:
            return campus
        return {"state": "200", "data": {"pageForm": {"pageData": [], "totalPage": 0}}}

    monkeypatch.setattr(hotjob, "fetch_position_list", fake_fetch)

    result = discover_portal_jobs_from_url(
        INTERNS_URL,
        html=shell,
        fetch=True,
        ocr_enabled=False,
        list_limit=200,
        list_collect_months=6,
        fetch_html=lambda u, timeout=60.0: shell,
        keep_company="迪卡侬中国-实习生",
    )
    assert result.ok, result.error
    assert result.total >= 5
    assert any("管理培训生" in (c.fields.get("title") or "") for c in result.candidates)


def test_maintenance_payload_detected():
    assert hotjob.is_api_maintenance_text("系统正在维护中，请稍候访问...")
    assert not hotjob.is_api_maintenance_text('{"state":"200"}')
    # 整页 HTML 偶含「维护」不得误判
    long_html = "<!DOCTYPE html><html><body>" + ("岗位维护说明" * 80) + "</body></html>"
    assert not hotjob.is_api_maintenance_text(long_html)


def test_page_url_candidates_include_www_fallback():
    cands = hotjob.page_url_candidates(INTERNS_URL)
    assert cands[0] == INTERNS_URL
    assert any("www.hotjob.cn" in u for u in cands)
    assert hotjob.is_waf_block_page(
        '<title>405</title><script>errors.aliyun.com</script><textarea id="renderData">',
        status_code=405,
    )
    assert not hotjob.is_waf_block_page(_shell(), status_code=200)


def test_discover_api_when_html_fetch_fails(monkeypatch):
    """HTML 全失败时仍靠 listPosition 枚举（WAF 405 场景）。"""
    intern = _intern_payload()
    campus = _campus_payload()

    def fake_fetch(tenant, recruit_type, **kwargs):
        rt = int(recruit_type)
        if rt == 12:
            return intern
        if rt == 1:
            return campus
        return {"state": "200", "data": {"pageForm": {"pageData": [], "totalPage": 0}}}

    monkeypatch.setattr(hotjob, "fetch_position_list", fake_fetch)

    def boom(url: str, timeout: float = 60.0) -> str:
        raise httpx.HTTPStatusError(
            "405",
            request=httpx.Request("GET", url),
            response=httpx.Response(
                405, text='<title>405</title>errors.aliyun.com<textarea id="renderData">'
            ),
        )

    result = discover_portal_jobs_from_url(
        INTERNS_URL,
        fetch=True,
        ocr_enabled=False,
        list_limit=200,
        list_collect_months=6,
        fetch_html=boom,
        keep_company="迪卡侬中国",
    )
    assert result.ok, result.error
    assert result.total >= 5
    titles = [c.fields.get("title") or "" for c in result.candidates]
    assert "色彩与平面实习生" in titles
    assert not any("Chrome" in t for t in titles)


def test_fetch_html_stops_when_wecruit_waf_blocks(monkeypatch):
    """router.fetch_html：命中 WAF 后不再改域名重试。"""
    from app.collector.adapters import router as adapter_router

    shell = _shell()
    calls: list[str] = []

    class _Resp:
        def __init__(self, status_code: int, text: str, url: str):
            self.status_code = status_code
            self.text = text
            self.request = httpx.Request("GET", url)

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"{self.status_code}",
                    request=self.request,
                    response=httpx.Response(self.status_code),
                )

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url: str):
            calls.append(url)
            if "wecruit.hotjob.cn" in url:
                return _Resp(
                    405,
                    '<title>405</title><img src="https://errors.aliyun.com/x.png">'
                    '<textarea id="renderData">{}</textarea>',
                    url,
                )
            return _Resp(200, shell, url)

    monkeypatch.setattr(adapter_router.httpx, "Client", _Client)
    with pytest.raises(adapter_router.AccessRestrictedError, match="疑似反爬/访问受限"):
        adapter_router.fetch_html(INTERNS_URL, timeout=10)
    assert calls == [INTERNS_URL]
    assert any("wecruit.hotjob.cn" in u for u in calls)


def test_html_table_primary_skips_api(monkeypatch):
    """有 HTML 表岗位时不得改走 listPosition（页面源码优先）。"""
    from app.collector.fill_from_url import enumerate_list_posts

    html = (FIXTURES / "atl_hotjob_interns_list.html").read_text(encoding="utf-8")
    atl = (
        "https://wecruit.hotjob.cn/SU66c8012c1eb80543256bf330/pb/interns.html"
        "?projectCode=ATL2026"
    )
    calls: list[tuple] = []

    def boom(*a, **k):
        calls.append((a, k))
        raise AssertionError("API must not run when HTML table has jobs")

    monkeypatch.setattr(hotjob, "fetch_position_list", boom)
    _ad, posts = enumerate_list_posts(atl, html, limit=40)
    assert not calls
    assert len(posts) >= 10
    assert any("电芯工艺实习生" in (p.title or "") for p in posts)
