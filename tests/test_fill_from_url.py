"""从链接识别填充：详情 / 列表页解析与表单字段合并。"""

from pathlib import Path

from app.collector.fill_from_url import (
    FillCandidate,
    _moka_direct_candidate_from_context,
    find_matching_fill_candidate,
    match_fill_candidate,
    merge_fill_into_form,
    normalize_job_link_fields,
    parse_result_to_form_fields,
    pick_fill_url,
    reidentify_job_fields,
    resolve_jobs_from_url,
)
from app.collector.adapters.base import ParseResult

FIXTURES = Path(__file__).parent / "fixtures"

DETAIL_HTML = """
<html><head><title>招聘系统--招聘详细</title></head>
<body><article>
后端开发工程师<br/>
招聘类型：校园招聘<br/>
工作类型：全职<br/>
截止时间：2025-12-31<br/>
工作地点：北京<br/>
工作职责：<br/>
1. 负责后端服务开发与维护<br/>
任职要求：<br/>
1. 熟悉 Python<br/>
</article></body></html>
"""


def test_pick_fill_url_prefers_apply():
    assert pick_fill_url("https://a.example/apply", "https://b.example/src") == "https://a.example/apply"
    assert pick_fill_url("", "https://b.example/src") == "https://b.example/src"
    assert pick_fill_url("not-a-url", "https://b.example/src") == "https://b.example/src"


def test_normalize_job_link_fields_apply_then_campus_fallback():
    apply, source = normalize_job_link_fields(
        apply_or_detail="https://ats.example.com/job/9",
        list_or_campus="https://campus.example.com/school",
        page_url="https://campus.example.com/school",
    )
    assert apply == "https://ats.example.com/job/9"
    assert source == "https://campus.example.com/school"

    # 仅有列表/校招页：只保留内部来源，不伪装成具体岗位网申。
    apply2, source2 = normalize_job_link_fields(
        apply_or_detail=None,
        list_or_campus="https://campus.example.com/school",
        page_url="https://campus.example.com/school",
    )
    assert apply2 == ""
    assert source2 == "https://campus.example.com/school"

    fields = parse_result_to_form_fields(
        ParseResult(
            title="后端开发工程师",
            recruit_project="校园招聘",
            recruit_bucket="校招",
            jd_text="岗位职责：开发",
            apply_url=None,
            confidence=0.7,
        ),
        page_url="https://campus.example.com/school",
    )
    assert fields["apply_url"] == ""
    assert fields["source_url"] == "https://campus.example.com/school"
    assert "后端" in fields["title"]
    assert "招聘官网" not in fields["title"]


def test_find_matching_requires_exact_title_not_substring():
    cands = [
        FillCandidate(
            fields={"title": "后端开发工程师", "apply_url": "https://x.example/a"},
            label="后端",
            summary="后端",
        ),
    ]
    assert (
        find_matching_fill_candidate(cands, title="后端", apply_url="") is None
    )
    hit = find_matching_fill_candidate(
        cands, title="后端开发工程师", apply_url=""
    )
    assert hit is not None


def test_resolve_detail_page_fills_fields():
    result = resolve_jobs_from_url(
        "https://campus.example.com/detail/99",
        html=DETAIL_HTML,
        fetch=False,
        ocr_enabled=False,
        keep_company="测试公司",
    )
    assert result.ok, result.error
    assert result.total == 1
    assert not result.is_list
    fields = result.candidates[0].fields
    assert "后端" in (fields.get("title") or "") or fields.get("work_location") == "北京"
    assert fields.get("work_location") == "北京"
    assert fields.get("deadline") == "2025-12-31"
    assert fields.get("recruit_bucket") == "校招" or fields.get("recruit_project") == "校园招聘"
    assert fields.get("graduation_batch")  # 默认或解析届别
    assert fields.get("company") == "测试公司"
    assert fields.get("apply_url")
    assert fields.get("source_url")


def test_resolve_list_page_multiple_candidates():
    html = (FIXTURES / "job_list_table.html").read_text(encoding="utf-8")
    result = resolve_jobs_from_url(
        "https://campus.example.com/school",
        html=html,
        fetch=False,
        ocr_enabled=False,
    )
    assert result.ok, result.error
    assert result.total >= 3
    assert result.is_list or result.total > 1
    titles = [c.fields.get("title") for c in result.candidates]
    assert "编辑岗（2026应届生）" in titles
    assert "营销岗（2026应届生）" in titles
    for c in result.candidates:
        assert c.fields.get("work_location") == "北京市-丰台区"
        assert "2026届" in (c.fields.get("graduation_batch") or "")


def test_resolve_empty_url_error():
    result = resolve_jobs_from_url("")
    assert not result.ok
    assert "链接" in (result.error or "")


def test_resolve_non_job_page_error():
    html = "<html><head><title>首页</title></head><body><p>欢迎</p></body></html>"
    result = resolve_jobs_from_url(
        "https://portal.example.com/",
        html=html,
        fetch=False,
        ocr_enabled=False,
    )
    assert not result.ok
    assert "岗位" in (result.error or "") or "门户" in (result.error or "")


def test_parse_result_to_form_fields_and_merge():
    pr = ParseResult(
        title="算法工程师",
        recruit_project="校园招聘",
        recruit_bucket="校招",
        work_location="上海",
        deadline="2026-03-01",
        jd_text="岗位职责：建模",
        apply_url="https://ats.example.com/job/1",
        confidence=0.8,
    )
    fields = parse_result_to_form_fields(pr, page_url="https://ats.example.com/job/1")
    assert fields["title"] == "算法工程师"
    assert fields["work_location"] == "上海"

    merged = merge_fill_into_form(
        {"company": "ACME", "title": "旧标题", "jd_text": "旧JD"},
        fields,
    )
    assert merged["company"] == "ACME"
    assert merged["title"] == "算法工程师"
    assert merged["jd_text"] == "岗位职责：建模"


def test_match_fill_candidate_by_title_and_url():
    cands = [
        FillCandidate(
            fields={"title": "营销岗", "apply_url": "https://x.example/a"},
            label="营销岗",
            summary="营销岗",
        ),
        FillCandidate(
            fields={"title": "后端开发工程师", "apply_url": "https://x.example/b"},
            label="后端",
            summary="后端",
        ),
    ]
    by_title = match_fill_candidate(cands, title="后端开发工程师")
    assert by_title is not None
    assert by_title.fields["title"] == "后端开发工程师"
    by_url = match_fill_candidate(
        cands, apply_url="https://x.example/a?utm=1", title="无关"
    )
    assert by_url is not None
    assert by_url.fields["title"] == "营销岗"


def test_reidentify_job_fields_from_html():
    job = {
        "company": "测试公司",
        "title": "旧壳标题",
        "source_url": "https://campus.example.com/detail/99",
        "apply_url": "https://campus.example.com/detail/99",
        "jd_text": "",
        "status": "pending_review",
    }
    action, merged, msg, result = reidentify_job_fields(job, html=DETAIL_HTML, fetch=False)
    assert action == "update", msg
    assert merged is not None
    assert result.ok
    assert merged["company"] == "测试公司"
    assert merged.get("work_location") == "北京" or "后端" in (merged.get("title") or "")
    assert merged.get("deadline") == "2025-12-31"


def test_reidentify_missing_url():
    action, merged, msg, _r = reidentify_job_fields({"company": "A", "title": "T"})
    assert action == "fail"
    assert merged is None
    assert "链接" in msg


def test_reidentify_no_job_posting_deletes():
    from app.collector.fill_from_url import (
        FillFromUrlResult,
        fields_look_like_no_job_posting,
        is_reidentify_transport_error,
        resolve_result_is_no_job_posting,
    )

    assert fields_look_like_no_job_posting(
        {"title": "岗位已关闭", "jd_text": "该职位已关闭"}
    )
    assert fields_look_like_no_job_posting({"title": "招贤纳才", "jd_text": ""})
    assert not fields_look_like_no_job_posting(
        {"title": "后端开发工程师", "jd_text": ""}
    )

    shell_html = """
    <html><head><title>招聘系统--招聘详细</title></head>
    <body><div>欢迎登录个人中心</div><div>校园招聘</div></body></html>
    """
    job = {
        "company": "测试公司",
        "title": "旧岗位",
        "source_url": "https://campus.example.com/detail/gone",
        "apply_url": "https://campus.example.com/detail/gone",
        "jd_text": "旧JD",
        "status": "pending_review",
    }
    action, merged, msg, result = reidentify_job_fields(
        job, html=shell_html, fetch=False
    )
    assert action == "delete", msg
    assert merged is None
    assert "无招聘" in msg
    assert resolve_result_is_no_job_posting(result) or result.no_job_posting

    closed_html = """
    <html><body><article>
    <h1>岗位已关闭</h1>
    <p>该职位已关闭，感谢关注</p>
    </article></body></html>
    """
    action2, merged2, msg2, _result2 = reidentify_job_fields(
        job, html=closed_html, fetch=False
    )
    assert action2 == "delete", msg2
    assert merged2 is None

    assert is_reidentify_transport_error("请求超时，请检查网络后重试")
    assert is_reidentify_transport_error("网络连接失败，无法打开该链接")
    assert not is_reidentify_transport_error("未能识别为岗位页（x.com）")
    assert not resolve_result_is_no_job_posting(
        FillFromUrlResult(
            ok=False,
            error="请求超时，请检查网络后重试",
            no_job_posting=False,
        )
    )
