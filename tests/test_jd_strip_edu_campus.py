"""JD 重复元信息剥离、学历抽取、校园招聘识别、重新识别落库。"""

from pathlib import Path

from app.collector.fill_from_url import (
    apply_reidentify_fields,
    reidentify_job_fields,
    summarize_reidentify_changes,
)
from app.collector.filters import campus_recognition_action, is_clear_social_hire_job
from app.collector.label_fields import (
    extract_education_from_title,
    extract_education_requirement,
    extract_labeled_fields,
    normalize_headcount,
    normalize_salary_range,
    summarize_jd_dedupe,
    strip_redundant_jd_meta,
)

SALARY_HEADCOUNT_JD = """后端开发工程师
薪资范围：
面议
招聘人数：
若干
2026-04-21
工作职责：
1. 负责后端开发
任职资格：
1. 本科及以上
"""
from app.collector.adapters.router import parse_html
from app.db.local import LocalDB

GOODIX_LIKE_JD = """数字IC设计工程师
校园招聘
全职
工作地点：深圳
工作职责：
1. 负责数字 IC 设计与验证
任职要求：
1. 硕士及以上学历
2. 熟悉 Verilog
"""


def test_strip_redundant_jd_meta_goodix_header():
    cleaned = strip_redundant_jd_meta(
        GOODIX_LIKE_JD,
        title="数字IC设计工程师",
        recruit_project="校园招聘",
        work_location="深圳",
        extra_values=["全职"],
    )
    assert cleaned
    assert "数字IC设计工程师" not in cleaned.splitlines()[0]
    assert "校园招聘" not in cleaned
    assert "全职" not in cleaned.splitlines()[0] if cleaned else True
    assert "工作地点" not in cleaned
    assert "工作职责" in cleaned
    assert "数字 IC" in cleaned or "IC" in cleaned
    assert "任职要求" in cleaned
    assert "Verilog" in cleaned


def test_extract_education_from_requirements():
    assert extract_education_requirement(GOODIX_LIKE_JD) == "硕士及以上"
    labels = extract_labeled_fields("学历要求：本科\n工作职责：开发\n")
    assert labels.get("education") == "本科"
    assert extract_education_requirement("任职要求：本科及以上，计算机相关", labels={}) == "本科及以上"
    assert extract_education_requirement("岗位职责：写代码") is None


def test_extract_education_from_title_paren():
    assert extract_education_from_title("研发工程师（硕士）") == "硕士"
    assert extract_education_from_title("管培生(博士)") == "博士"
    assert extract_education_from_title("工艺工程师（硕士研究生及以上）") == "硕士及以上"
    assert extract_education_requirement("岗位职责：写代码", title="分析师（本科）") == "本科"
    # JD 正文优先于标题副信号
    assert (
        extract_education_requirement(
            "任职要求：\n1. 博士学历\n",
            title="分析师（本科）",
        )
        == "博士"
    )


def test_summarize_jd_dedupe_lists_removed_header():
    cleaned = strip_redundant_jd_meta(
        GOODIX_LIKE_JD,
        title="数字IC设计工程师",
        recruit_project="校园招聘",
        work_location="深圳",
        extra_values=["全职"],
    )
    summary = summarize_jd_dedupe(GOODIX_LIKE_JD, cleaned)
    assert "去掉" in summary or "重复" in summary
    assert "校园招聘" in summary or "全职" in summary or "工作地点" in summary


def test_parse_html_strips_meta_and_fills_education():
    body = GOODIX_LIKE_JD.replace("\n", "<br/>")
    html = f"<html><body><article>{body}</article></body></html>"
    result = parse_html("https://campus.example.com/detail/goodix", html, ocr_enabled=False)
    assert result.work_location == "深圳" or result.recruit_project == "校园招聘"
    assert result.education in ("硕士", "硕士及以上")
    jd = result.jd_text or ""
    assert "工作职责" in jd or "任职要求" in jd
    # 页眉重复行应已剥离
    assert not jd.lstrip().startswith("数字IC设计工程师\n校园招聘")


def test_campus_recognition_deletes_social_keeps_campus():
    campus = {
        "title": "后端开发",
        "recruit_project": "校园招聘",
        "jd_text": "招聘类别：校园招聘\n工作职责：开发",
    }
    intern = {
        "title": "数据分析实习",
        "recruit_project": "日常实习",
        "recruit_bucket": "日常实习",
        "jd_text": "实习生招聘",
    }
    social = {
        "title": "高级工程师",
        "recruit_project": "社会招聘",
        "jd_text": "招聘类别：社会招聘\n工作职责：架构",
    }
    social_in_text = {
        "title": "运维",
        "recruit_project": "",
        "raw_category": "社会招聘",
        "jd_text": "欢迎社会人才加入",
    }
    assert campus_recognition_action(campus) == "keep"
    assert campus_recognition_action(intern) == "keep"
    assert campus_recognition_action(social) == "delete"
    assert is_clear_social_hire_job(social_in_text)
    assert campus_recognition_action(social_in_text) == "delete"


def test_reidentify_persists_fields_to_db(tmp_path: Path):
    db = LocalDB(tmp_path / "reidentify.db")
    jid = db.upsert_job(
        {
            "company": "测试公司",
            "title": "旧壳标题",
            "source_url": "https://campus.example.com/detail/99",
            "apply_url": "https://campus.example.com/detail/99",
            "jd_text": "旧JD",
            "status": "pending_review",
        }
    )
    detail_html = """
    <html><head><title>招聘系统--招聘详细</title></head>
    <body><article>
    后端开发工程师<br/>
    招聘类型：校园招聘<br/>
    工作类型：全职<br/>
    截止时间：2025-12-31<br/>
    工作地点：北京<br/>
    学历要求：本科<br/>
    工作职责：<br/>
    1. 负责后端服务开发与维护<br/>
    任职要求：<br/>
    1. 熟悉 Python<br/>
    </article></body></html>
    """
    job = db.get_job(jid)
    assert job is not None
    action, merged, msg, _r = reidentify_job_fields(job, html=detail_html, fetch=False)
    assert action == "update", msg
    assert merged is not None
    merged["id"] = jid
    merged["status"] = "pending_review"
    db.upsert_job(merged)
    fresh = db.get_job(jid)
    assert fresh is not None
    assert fresh.get("work_location") == "北京"
    assert fresh.get("deadline") == "2025-12-31"
    assert fresh.get("education") in ("本科", "本科及以上")
    assert "后端" in (fresh.get("title") or "") or fresh.get("recruit_project") == "校园招聘"
    jd = fresh.get("jd_text") or ""
    assert "工作职责" in jd or "Python" in jd
    assert "校园招聘" not in jd.split("工作职责")[0] if "工作职责" in jd else True


def test_reidentify_no_job_soft_deletes_in_db(tmp_path: Path):
    db = LocalDB(tmp_path / "reidentify_del.db")
    jid = db.upsert_job(
        {
            "company": "测试公司",
            "title": "将删除岗",
            "source_url": "https://campus.example.com/detail/closed",
            "apply_url": "https://campus.example.com/detail/closed",
            "jd_text": "旧",
            "status": "pending_review",
        }
    )
    closed_html = """
    <html><body><h1>岗位已关闭</h1><p>该职位已关闭</p></body></html>
    """
    job = db.get_job(jid)
    assert job is not None
    action, merged, msg, _r = reidentify_job_fields(job, html=closed_html, fetch=False)
    assert action == "delete", msg
    assert merged is None
    n = db.soft_delete_jobs([jid])
    assert n == 1
    fresh = db.get_job(jid)
    assert fresh is not None
    assert fresh.get("status") == "deleted"


def test_apply_reidentify_fields_overwrites_and_strips():
    before = {
        "company": "汇顶",
        "title": "旧标题",
        "recruit_project": "",
        "work_location": "",
        "jd_text": "旧",
        "education": "",
    }
    filled = {
        "title": "数字IC设计工程师",
        "recruit_project": "校园招聘",
        "recruit_bucket": "校招",
        "work_location": "深圳",
        "education": "硕士",
        "jd_text": GOODIX_LIKE_JD,
        "source_url": "https://x.example/j",
        "apply_url": "https://x.example/j",
    }
    merged = apply_reidentify_fields(before, filled)
    assert merged["company"] == "汇顶"
    assert merged["title"] == "数字IC设计工程师"
    assert merged["work_location"] == "深圳"
    assert merged["education"] == "硕士"
    assert "工作职责" in (merged.get("jd_text") or "")
    assert "工作地点：深圳" not in (merged.get("jd_text") or "")
    changes = summarize_reidentify_changes(before, merged)
    assert any("岗位名称" in c for c in changes)
    assert any("工作地点" in c for c in changes)
    assert any("学历" in c for c in changes)


def test_extract_salary_range_and_headcount_from_labels():
    labels = extract_labeled_fields(SALARY_HEADCOUNT_JD)
    assert labels.get("salary_range") == "面议"
    assert labels.get("headcount") == "若干"
    assert normalize_salary_range("面议") == "面议"
    assert normalize_headcount("若干") == "若干"
    assert normalize_headcount("招若干人") == "若干"
    assert normalize_salary_range("2026-04-21") is None
    assert normalize_headcount("工作职责") is None
    # 缺标签 → 不误填
    bare = extract_labeled_fields("工作职责：\n写代码\n任职资格：\n本科\n")
    assert "salary_range" not in bare
    assert "headcount" not in bare


def test_strip_salary_headcount_from_jd():
    cleaned = strip_redundant_jd_meta(
        SALARY_HEADCOUNT_JD,
        title="后端开发工程师",
        salary_range="面议",
        headcount="若干",
    )
    assert cleaned
    assert "薪资范围" not in cleaned
    assert "招聘人数" not in cleaned
    assert "面议" not in cleaned
    assert "若干" not in cleaned
    assert "工作职责" in cleaned
    assert "任职资格" in cleaned


def test_parse_html_fills_salary_headcount_and_strips():
    body = SALARY_HEADCOUNT_JD.replace("\n", "<br/>")
    html = f"<html><body><article>{body}</article></body></html>"
    result = parse_html("https://campus.example.com/detail/pay", html, ocr_enabled=False)
    assert result.salary_range == "面议"
    assert result.headcount == "若干"
    jd = result.jd_text or ""
    assert "工作职责" in jd or "任职资格" in jd
    assert "薪资范围" not in jd
    assert "招聘人数" not in jd


def test_apply_reidentify_salary_headcount():
    before = {
        "company": "测试",
        "title": "旧",
        "jd_text": "旧",
        "salary_range": "",
        "headcount": "",
    }
    filled = {
        "title": "后端开发工程师",
        "salary_range": "面议",
        "headcount": "若干",
        "jd_text": SALARY_HEADCOUNT_JD,
        "source_url": "https://x.example/pay",
        "apply_url": "https://x.example/pay",
    }
    merged = apply_reidentify_fields(before, filled)
    assert merged["salary_range"] == "面议"
    assert merged["headcount"] == "若干"
    assert "薪资范围" not in (merged.get("jd_text") or "")
    assert "招聘人数" not in (merged.get("jd_text") or "")
    changes = summarize_reidentify_changes(before, merged)
    assert any("薪资" in c for c in changes)
    assert any("招聘人数" in c for c in changes)
