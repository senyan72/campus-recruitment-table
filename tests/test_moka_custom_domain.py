"""Moka 自定义域名（远景 envision-career.com）识别与详情 API。"""

from __future__ import annotations

from unittest.mock import patch

from app.collector.adapters import moka
from app.collector.adapters.router import pick_parser, parse_html
from app.collector.fill_from_url import (
    FillCandidate,
    _enrich_moka_via_job_api,
)


ENVISION_URL = "https://envision-career.com/campus-recruitment/envisiongroup/43123/"
MOKAHR_URL = "https://app.mokahr.com/campus-recruitment/zte/46903"


def test_can_handle_custom_domain_campus_path():
    assert moka.can_handle(ENVISION_URL)
    assert moka.can_handle(MOKAHR_URL)
    assert not moka.can_handle("https://example.com/careers")


def test_pick_parser_custom_domain():
    assert pick_parser(ENVISION_URL)[1] == "moka"


def test_parse_html_detects_moka_init_data_on_generic_url():
    payload = (
        "{&quot;org&quot;:{&quot;id&quot;:&quot;envisiongroup&quot;,"
        "&quot;name&quot;:&quot;远景&quot;,&quot;siteId&quot;:43123},"
        "&quot;aesIv&quot;:&quot;0123456789abcdef&quot;,"
        "&quot;jobs&quot;:[],&quot;jobStats&quot;:{&quot;total&quot;:0}}"
    )
    html = f'<html><body><input id="init-data" value="{payload}"/></body></html>'
    # 裸域名无 path 时仍可凭 init-data 切到 moka
    result = parse_html("https://envision-career.com/", html, ocr_enabled=False)
    assert (result.extras or {}).get("adapter") == "moka"


def test_enrich_moka_via_job_api_uses_detail(monkeypatch):
    list_hint = moka.job_dict_to_result(
        {
            "id": "27bad45c-f2fd-477b-b96d-68e62c41ad0c",
            "title": "数字孪生建模与算法优化工程师",
            "orgId": "envisiongroup",
            "siteId": 43123,
        },
        list_url=ENVISION_URL,
        company="远景",
        source="moka_list_api",
    )
    assert list_hint is not None
    list_hint.extras["aes_iv"] = "0123456789abcdef"
    list_hint.extras["needs_fetch"] = True

    cand = FillCandidate(
        fields={
            "title": list_hint.title or "",
            "apply_url": list_hint.apply_url or ENVISION_URL,
        },
        label=list_hint.title or "",
        summary="",
        needs_fetch=True,
        detail_url=list_hint.apply_url,
        list_hint=list_hint,
    )

    detail = {
        "id": "27bad45c-f2fd-477b-b96d-68e62c41ad0c",
        "title": "数字孪生建模与算法优化工程师",
        "orgId": "envisiongroup",
        "jobDescription": "<p>我们需要你：</p><p>1、负责风机系统智能运营；</p>"
        "<p>我们期望你：</p><p>1、硕士以上；</p>",
    }

    with patch.object(moka, "fetch_job_detail", return_value=detail) as mocked:
        out = _enrich_moka_via_job_api(
            cand,
            detail_url=cand.detail_url or "",
            page_url=ENVISION_URL,
            timeout=30.0,
            keep_company="远景",
        )
    assert mocked.called
    assert out is not None
    jd = out.fields.get("jd_text") or ""
    assert "智能运营" in jd
    assert "硕士" in jd
