"""中兴 Moka：init-data 仅嵌部分岗位，jobs/v2 分页补全。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from app.collector.adapters import moka
from app.collector.fill_from_url import resolve_jobs_from_url

FIXTURES = Path(__file__).parent / "fixtures"
PARTIAL_HTML = (FIXTURES / "moka_zte_campus_init_partial.html").read_text(encoding="utf-8")
API_PAGES = json.loads((FIXTURES / "moka_zte_jobs_api_pages.json").read_text(encoding="utf-8"))
ENVELOPE = json.loads((FIXTURES / "moka_zte_jobs_v2_page1.json").read_text(encoding="utf-8"))
LIST_URL = "https://app.mokahr.com/campus-recruitment/zte/46903#/jobs"


def test_partial_init_data_has_total_gt_embedded():
    data = moka.load_init_data(PARTIAL_HTML)
    assert data is not None
    assert data["org"]["name"] == "中兴通讯"
    jobs = moka.extract_embedded_jobs(PARTIAL_HTML)
    assert len(jobs) == 2
    assert moka.job_stats_total(data=data) == 5
    assert data.get("aesIv")


def test_html_only_without_api_stays_partial():
    posts = moka.enumerate_positions(LIST_URL, PARTIAL_HTML, limit=40, fetch_api=False)
    assert len(posts) == 2
    assert all((p.extras or {}).get("source") == "moka_init_data" for p in posts)


def test_enumerate_paginates_via_api_mock():
    pages = {p["offset"]: p for p in API_PAGES["pages"]}

    def _fake_fetch(org_id, site_id, *, offset=0, limit=50, aes_iv="", list_url=None, timeout=30.0):
        assert org_id == "zte"
        assert str(site_id) == "46903"
        assert aes_iv == API_PAGES["aesIv"]
        page = pages.get(offset) or {"jobs": [], "total": 5}
        return list(page["jobs"]), int(page["total"])

    with patch.object(moka, "fetch_jobs_page", side_effect=_fake_fetch):
        posts = moka.enumerate_positions(
            LIST_URL, PARTIAL_HTML, limit=40, max_pages=8, page_size=3, fetch_api=True
        )
    assert len(posts) == 5
    titles = {p.title for p in posts}
    assert any("测试岗位" in (t or "") for t in titles)
    # 合并后应含 API 补全的条数；来源可为 api 或 init（前两条可能被 HTML 先写入再被 API 覆盖）
    assert sum(1 for p in posts if (p.extras or {}).get("from_api")) >= 3
    assert all("zte/46903" in (p.apply_url or "") and "#/job/" in (p.apply_url or "") for p in posts)


def test_outside_lookback_flag_kept():
    pages = {p["offset"]: p for p in API_PAGES["pages"]}
    # 把第 5 条改成很旧的日期
    old_pages = json.loads(json.dumps(API_PAGES["pages"]))
    old_pages[1]["jobs"][1]["publishedAt"] = "2024-01-01T00:00:00.000Z"
    old_pages[1]["jobs"][1]["openedAt"] = "2024-01-01T00:00:00.000Z"
    old_pages[1]["jobs"][1]["updatedAt"] = "2024-01-01T00:00:00.000Z"
    by_off = {p["offset"]: p for p in old_pages}

    def _fake_fetch(org_id, site_id, *, offset=0, limit=50, aes_iv="", list_url=None, timeout=30.0):
        page = by_off.get(offset) or {"jobs": [], "total": 5}
        return list(page["jobs"]), int(page["total"])

    with patch.object(moka, "fetch_jobs_page", side_effect=_fake_fetch):
        posts = moka.enumerate_positions(
            LIST_URL, PARTIAL_HTML, limit=40, page_size=3, fetch_api=True
        )
    outside = [p for p in posts if (p.extras or {}).get("outside_lookback")]
    assert len(posts) == 5
    assert len(outside) >= 1


def test_decrypt_real_zte_envelope_fixture():
    data = moka.load_init_data(PARTIAL_HTML)
    assert data
    decoded = moka.decrypt_moka_envelope(ENVELOPE, str(data["aesIv"]))
    assert decoded and decoded.get("code") == 0
    jobs = (decoded.get("data") or {}).get("jobs") or []
    total = ((decoded.get("data") or {}).get("jobStats") or {}).get("total")
    assert len(jobs) == 50
    assert int(total) == 126
    assert jobs[0].get("title")


def test_resolve_jobs_uses_api_supplement():
    all_jobs: list[dict] = []
    for p in API_PAGES["pages"]:
        all_jobs.extend(p["jobs"])

    def _fake_fetch(org_id, site_id, *, offset=0, limit=50, aes_iv="", list_url=None, timeout=30.0):
        chunk = all_jobs[int(offset) : int(offset) + int(limit)]
        return list(chunk), 5

    with patch.object(moka, "fetch_jobs_page", side_effect=_fake_fetch):
        result = resolve_jobs_from_url(
            LIST_URL,
            html=PARTIAL_HTML,
            fetch=False,
            ocr_enabled=False,
            discover_portal=True,
            list_limit=200,
            keep_company=None,
        )
    assert result.ok
    assert result.total == 5
    assert result.adapter == "moka"


def test_parse_site_from_url_without_m_prefix():
    org, site, kind = moka.parse_site_from_url(LIST_URL)
    assert org == "zte"
    assert site == 46903
    assert kind == "campus-recruitment"
    apply = moka.detail_url_for_job(LIST_URL, "abc-def")
    assert apply.endswith("#/job/abc-def")
    assert "/campus-recruitment/zte/46903" in apply
