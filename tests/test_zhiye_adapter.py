import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from app.collector.adapters import zhiye

FIXTURES = Path(__file__).parent / "fixtures"
OVT_LIST_URL = (
    "https://ovt-omnivision.zhiye.com/campus/jobs?"
    'LocId=[{"id":"1100","label":"北京市"}]'
)


def _load_fixture_rows() -> list[dict]:
    data = json.loads((FIXTURES / "zhiye_ovt_jobad_page.json").read_text(encoding="utf-8-sig"))
    return data["Data"]


def test_jobad_row_to_result_from_fixture():
    rows = _load_fixture_rows()
    assert len(rows) >= 3
    first = rows[0]
    r = zhiye.jobad_row_to_result(first, list_url=OVT_LIST_URL)
    assert r is not None
    assert r.title == "模拟测试工程师-27届(J11638)"
    assert r.jd_text and "岗位职责" in r.jd_text and "任职要求" in r.jd_text
    assert "芯片模拟模块" in r.jd_text
    assert r.recruit_bucket == "校招"
    assert r.apply_url == "https://ovt-omnivision.zhiye.com/campus/detail?jobAdId=511187533"
    assert r.extras.get("open_at") == "2026-07-20"
    assert r.extras.get("source") == "zhiye_api"
    assert r.extras.get("needs_fetch") is False


def test_jobad_row_to_result_all_fixture_titles():
    rows = _load_fixture_rows()
    titles = {
        zhiye.jobad_row_to_result(row, list_url=OVT_LIST_URL).title
        for row in rows
    }
    assert "模拟测试工程师-27届(J11638)" in titles
    assert "SH111工艺整合工程师（硕博）(J11600)" in titles
    assert "SH101-Backend Design Engineer(J11754)" in titles


def test_enumerate_positions_html_table_first():
    html = (FIXTURES / "job_list_table.html").read_text(encoding="utf-8")
    base = "https://demo.zhiye.com/campus/jobs"
    posts = zhiye.enumerate_positions(base, html, limit=10, fetch_api=False)
    assert len(posts) == 3
    assert all(p.extras.get("adapter") == "zhiye" for p in posts)
    assert posts[0].title and "编辑岗" in posts[0].title


def test_is_spa_list_shell():
    shell = """<!DOCTYPE html><html><body><div id="app" class="recruitment-portal"></div></body></html>"""
    assert zhiye.is_spa_list_shell(shell)
    table_html = (FIXTURES / "job_list_table.html").read_text(encoding="utf-8")
    assert not zhiye.is_spa_list_shell(table_html)


def test_categories_from_campus_url():
    cats = zhiye.categories_from_url(OVT_LIST_URL)
    assert cats == [["2"], ["3"]]


def test_enumerate_positions_api_mocked():
    fixture_bytes = (FIXTURES / "zhiye_ovt_jobad_page.json").read_bytes()
    shell = """<!DOCTYPE html><html><body><div id="app" class="recruitment-portal"></div></body></html>"""

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = fixture_bytes

    mock_client = MagicMock()
    mock_client.post.return_value = mock_resp
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)

    with patch("app.collector.adapters.zhiye.httpx.Client", return_value=mock_client):
        posts = zhiye.enumerate_positions(OVT_LIST_URL, shell, limit=10, max_pages=1)

    assert len(posts) == 3
    assert any("27届" in (p.title or "") for p in posts)
    assert all(p.extras.get("source") == "zhiye_api" for p in posts)
    assert mock_client.post.call_count >= 1


def test_branded_root_page_without_links_still_uses_job_api():
    html = """
    <!doctype html><html><head><title>官网 | 科大讯飞招聘</title></head>
    <body><main>品牌介绍与招聘宣传内容</main><script>var BSGlobal = {};</script></body></html>
    """
    api_post = zhiye.jobad_row_to_result(
        _load_fixture_rows()[0], list_url="https://iflytek.zhiye.com"
    )
    assert api_post is not None
    with patch(
        "app.collector.adapters.zhiye.enumerate_positions_via_api",
        return_value=[api_post],
    ) as fetch_api:
        posts = zhiye.enumerate_positions(
            "https://iflytek.zhiye.com", html, limit=50, fetch_api=True
        )
    assert len(posts) == 1
    assert posts[0].title == "模拟测试工程师-27届(J11638)"
    fetch_api.assert_called_once()


def test_safe_referer_strips_query_with_non_ascii():
    url = (
        "https://ovt-omnivision.zhiye.com/campus/jobs?"
        'LocId=[{"id":"1100","label":"北京市"}]'
    )
    ref = zhiye._safe_referer(url, "https://ovt-omnivision.zhiye.com")
    assert ref == "https://ovt-omnivision.zhiye.com/campus/jobs"
    ref.encode("ascii")  # must not raise


def test_fetch_jobad_page_list_decodes_bom(monkeypatch):
    fixture_bytes = (FIXTURES / "zhiye_ovt_jobad_page.json").read_bytes()
    bom_payload = b"\xef\xbb\xbf" + fixture_bytes

    class FakeResp:
        status_code = 200
        content = bom_payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json=None):
            assert url.endswith("/api/JobAd/GetJobAdPageList")
            assert json["PageIndex"] == 0
            return FakeResp()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    rows, total = zhiye.fetch_jobad_page_list(
        OVT_LIST_URL, category=["2"], page_index=0
    )
    assert len(rows) == 3
    assert total == 10
