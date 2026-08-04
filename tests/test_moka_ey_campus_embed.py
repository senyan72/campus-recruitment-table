"""安永 Moka 校园门户：#init-data HTML 主键抽取（无 API）。"""

from __future__ import annotations

from pathlib import Path

from app.collector.adapters import moka
from app.collector.fill_from_url import parse_result_to_form_fields, resolve_jobs_from_url
from app.collector.filters import is_noise_title
from app.collector.label_fields import split_jd_sections

FIXTURES = Path(__file__).parent / "fixtures"
HTML = (FIXTURES / "moka_ey_campus_init_data.html").read_text(encoding="utf-8")
LIST_URL = "https://app.mokahr.com/m/campus-recruitment/ey/166374#/jobs"
INTERN_ID = "7ffd08ae-d2de-4c6d-8030-ab800266716a"
CAMPUS_ID = "3f07f2cc-f41e-4a61-9203-7ff249953423"


def test_load_init_data_and_embedded_jobs():
    data = moka.load_init_data(HTML)
    assert data and data["org"]["name"] == "安永"
    jobs = moka.extract_embedded_jobs(HTML)
    assert len(jobs) == 2
    titles = {j["title"] for j in jobs}
    assert "2026-2027安永审计寒假实习生招聘项目" in titles
    assert "2026-2027年安永秋季应届毕业生校园招聘项目" in titles


def test_enumerate_positions_html_primary_no_network():
    posts = moka.enumerate_positions(LIST_URL, HTML, limit=40, fetch_api=False)
    assert len(posts) == 2
    by_title = {p.title: p for p in posts}
    intern = by_title["2026-2027安永审计寒假实习生招聘项目"]
    assert intern.work_location == "中国内地"
    assert (intern.extras or {}).get("published_at") == "2026-07-31"
    assert (intern.extras or {}).get("company") == "安永"
    assert INTERN_ID in (intern.apply_url or "")
    assert intern.recruit_bucket in ("日常实习", "校招") or "实习" in (intern.recruit_project or "")

    campus = by_title["2026-2027年安永秋季应届毕业生校园招聘项目"]
    assert (campus.extras or {}).get("published_at") == "2026-07-22"
    assert campus.work_location == "中国内地"


def test_resolve_jobs_from_url_fetch_false():
    result = resolve_jobs_from_url(
        LIST_URL,
        html=HTML,
        fetch=False,
        ocr_enabled=False,
        discover_portal=False,
        list_limit=40,
        keep_company=None,
    )
    assert result.ok
    assert result.total == 2
    titles = [c.fields.get("title") for c in result.candidates]
    assert "Moka，智能化招聘管理系统" not in titles
    assert any("寒假实习" in (t or "") for t in titles)
    assert any("秋季应届" in (t or "") for t in titles)
    intern = next(c for c in result.candidates if "寒假实习" in (c.fields.get("title") or ""))
    assert intern.fields.get("company") == "安永"
    assert intern.fields.get("work_location") == "中国内地"
    assert intern.fields.get("open_at") == "2026-07-31"
    assert INTERN_ID in (intern.fields.get("apply_url") or "")
    # 列表源码无 JD 正文 → 职责/要求为空（非解析失败）
    duties, reqs = split_jd_sections(intern.fields.get("jd_text"))
    assert duties == "" and reqs == ""


def test_detail_hash_picks_one_job():
    url = f"https://app.mokahr.com/m/campus-recruitment/ey/166374#/job/{INTERN_ID}"
    pr = moka.parse_moka_detail(url, HTML)
    assert pr.title == "2026-2027安永审计寒假实习生招聘项目"
    assert pr.work_location == "中国内地"
    assert (pr.extras or {}).get("published_at") == "2026-07-31"
    fields = parse_result_to_form_fields(pr, page_url=url)
    assert fields["open_at"] == "2026-07-31"
    assert fields["company"] == "安永"


def test_moka_footer_is_noise_title():
    assert is_noise_title("Moka，智能化招聘管理系统")
