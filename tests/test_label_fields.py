"""标签：值 结构化抽取单测。"""

from pathlib import Path

from app.collector.adapters.router import parse_html
from app.collector.label_fields import (
    apply_labeled_fields,
    extract_labeled_fields,
    map_labeled_recruit,
    merge_jd_sections,
    split_jd_sections,
    strip_redundant_jd_meta,
)
from app.collector.adapters.base import ParseResult
from app.collector.filters import is_intern_hiring, is_social_hiring

FIXTURES = Path(__file__).resolve().parent / "fixtures"


SAMPLE_JD = """
后端开发工程师
招聘类型：校园招聘
工作类型：全职
发布时间：2025-10-11
截止时间：2025-12-31
工作地点：北京
工作职责：
1. 负责后端服务开发与维护
2. 参与需求分析与技术方案
任职要求：
1. 熟悉 Python
2. 了解常见 Web 框架
"""


def test_extract_labeled_fields_campus_sample():
    labels = extract_labeled_fields(SAMPLE_JD)
    assert labels["recruit_type"] == "校园招聘"
    assert labels["work_type"] == "全职"
    assert labels["open_at"] == "2025-10-11"
    assert labels["deadline"] == "2025-12-31"
    assert labels["work_location"] == "北京"
    assert "后端服务" in labels["jd_duties"]
    assert "熟悉 Python" in labels["jd_requirements"]


def test_extract_open_at_strips_latest_suffix():
    labels = extract_labeled_fields(
        "设计实习\n发布时间：2026-07-31 最新\n工作地点：上海\n工作职责：\n设计\n"
    )
    assert labels.get("open_at") == "2026-07-31"
    labels2 = extract_labeled_fields(
        "设计实习\n岗位发布时间：2026年7月31日最新发布\n工作地点：上海\n"
    )
    assert labels2.get("open_at") == "2026-07-31"


def test_extract_salary_and_headcount_labels():
    text = (
        "算法工程师\n"
        "薪资：15-25K\n"
        "招聘人数：2人\n"
        "工作职责：\n1. 模型训练\n"
    )
    labels = extract_labeled_fields(text)
    assert labels.get("salary_range") == "15-25K"
    assert labels.get("headcount") == "2人"
    # 标签在、值为空 → 不写入（保持缺省）
    empty_val = extract_labeled_fields("薪资范围：\n\n招聘人数：\n\n工作职责：\n开发\n")
    assert "salary_range" not in empty_val
    assert "headcount" not in empty_val


def test_map_labeled_recruit_campus_and_intern():
    project, bucket = map_labeled_recruit("校园招聘", "全职")
    assert project == "校园招聘"
    assert bucket == "校招"

    project, bucket = map_labeled_recruit("校园招聘", "实习")
    assert bucket == "日常实习"
    assert "实习" in (project or "")

    project, bucket = map_labeled_recruit("社会招聘", None)
    assert project == "社会招聘"
    assert bucket != "校招"
    assert bucket is None
    assert is_social_hiring(recruit_project=project, title="工程师")


def test_recruit_category_social_not_campus_bucket():
    """招聘类别：社会招聘 → 不得判为校招 bucket；进社招异常口径。"""
    from app.collector.filters import is_target_campus_or_intern, map_recruit_bucket

    text = (
        "环保工程师\n"
        "招聘类别：社会招聘\n"
        "工作类型：全职\n"
        "工作地点：上海\n"
        "工作职责：\n1. 环境治理\n"
    )
    labels = extract_labeled_fields(text)
    assert labels.get("recruit_type") == "社会招聘"
    project, bucket = map_labeled_recruit(
        labels.get("recruit_type"), labels.get("work_type"), title="环保工程师"
    )
    assert project == "社会招聘"
    assert bucket != "校招"
    assert map_recruit_bucket(project, "环保工程师校招岗") != "校招"

    result = apply_labeled_fields(
        ParseResult(title="2027届校招环保工程师", recruit_project="校园招聘", recruit_bucket="校招"),
        labels,
    )
    assert result.recruit_project == "社会招聘"
    assert result.recruit_bucket != "校招"
    assert is_social_hiring(
        title=result.title,
        jd_text=text,
        recruit_project=result.recruit_project,
    )
    ok, kind, _ = is_target_campus_or_intern(
        title=result.title,
        jd_text=text,
        recruit_project=result.recruit_project,
        recruit_bucket=result.recruit_bucket,
    )
    assert not ok
    assert kind == "not_target_hiring"

    html = f"<html><body><article>{text.replace(chr(10), '<br/>')}</article></body></html>"
    parsed = parse_html("https://campus.example.com/detail/social", html, ocr_enabled=False)
    assert parsed.recruit_project == "社会招聘"
    assert parsed.recruit_bucket != "校招"


def test_recruit_category_campus_is_campus_bucket():
    """招聘类别：校园招聘 → 校招。"""
    text = "后端开发\n招聘类别：校园招聘\n工作类型：全职\n工作地点：北京\n"
    labels = extract_labeled_fields(text)
    assert labels.get("recruit_type") == "校园招聘"
    project, bucket = map_labeled_recruit(labels.get("recruit_type"), labels.get("work_type"))
    assert project == "校园招聘"
    assert bucket == "校招"
    result = apply_labeled_fields(ParseResult(), labels)
    assert result.recruit_project == "校园招聘"
    assert result.recruit_bucket == "校招"


def test_apply_labeled_fields_into_parse_result():
    result = ParseResult(title=None, jd_text=None)
    labels = extract_labeled_fields(SAMPLE_JD)
    result = apply_labeled_fields(result, labels)
    assert result.work_location == "北京"
    assert result.deadline == "2025-12-31"
    assert result.extras.get("published_at") == "2025-10-11"
    assert result.recruit_project == "校园招聘"
    assert result.recruit_bucket == "校招"
    assert result.jd_text and "工作职责" in result.jd_text
    assert "任职要求" in result.jd_text
    assert "熟悉 Python" in result.jd_text


def test_parse_html_merges_labels():
    body = SAMPLE_JD.replace("\n", "<br/>")
    html = f"""
    <html><head><title>招聘系统--招聘详细</title></head>
    <body><article>{body}</article></body></html>
    """
    result = parse_html("https://campus.example.com/detail/99", html, ocr_enabled=False)
    assert result.work_location == "北京"
    assert result.deadline == "2025-12-31"
    assert result.extras.get("published_at") == "2025-10-11"
    assert result.recruit_project == "校园招聘"
    assert result.recruit_bucket == "校招"
    assert result.jd_text and "任职要求" in result.jd_text


def test_intern_work_type_links_bucket():
    text = "岗位名称：数据分析实习\n招聘类型：校园招聘\n工作类型：实习\n工作地点：上海\n"
    labels = extract_labeled_fields(text)
    result = apply_labeled_fields(ParseResult(), labels)
    assert result.recruit_bucket == "日常实习"
    assert is_intern_hiring(
        title=result.title,
        recruit_bucket=result.recruit_bucket,
        recruit_project=result.recruit_project,
    )


def test_merge_jd_sections_keeps_existing():
    existing = "完整 JD 正文" + ("详" * 80)
    merged = merge_jd_sections(existing, duties="做开发", requirements="会 Python")
    assert existing in merged
    assert "工作职责" in merged
    assert "任职要求" in merged


def test_split_jd_sections_english_decathlon_fixture():
    jd = (FIXTURES / "decathlon_en_intern_jd.txt").read_text(encoding="utf-8")
    duties, reqs = split_jd_sections(jd)
    assert "color and graphic proposals" in duties.lower()
    assert "textile and footwear" in duties.lower()
    assert "Adobe Illustrator" in reqs
    assert "design-related major" in reqs
    assert "What is a Color" not in duties
    labels = extract_labeled_fields(jd)
    assert "proposals" in (labels.get("jd_duties") or "").lower()
    assert "Illustrator" in (labels.get("jd_requirements") or "")
    assert "design-related major" in (labels.get("jd_requirements") or "")


def test_split_jd_sections_mixed_cn_en_fixture():
    jd = (FIXTURES / "decathlon_mixed_cn_en_jd.txt").read_text(encoding="utf-8")
    duties, reqs = split_jd_sections(jd)
    assert "数据收集" in duties
    assert "Excel" in reqs
    assert "财会专业" in reqs


def test_split_jd_sections_what_youll_do_and_qualifications():
    jd = (
        "What you'll do:\n"
        "1. Own weekly reporting\n"
        "Qualifications:\n"
        "1. Bachelor student\n"
        "Who you are:\n"
        "1. Curious about retail\n"
    )
    duties, reqs = split_jd_sections(jd)
    assert "weekly reporting" in duties
    assert "Bachelor" in reqs
    assert "Curious" in reqs


def test_strip_redundant_jd_meta_english_body_start():
    jd = (
        "Color Intern\nIntern\nShanghai\n"
        "Responsibilities:\n"
        "1. Design support\n"
        "Requirements:\n"
        "1. Portfolio\n"
    )
    cleaned = strip_redundant_jd_meta(
        jd,
        title="Color Intern",
        work_location="Shanghai",
        extra_values=["Intern"],
    )
    assert cleaned
    assert cleaned.lstrip().startswith("Responsibilities")
    assert "Design support" in cleaned


def test_type_label_only_accepts_known_values():
    labels = extract_labeled_fields("类型：校园招聘\n其他：无关\n")
    assert labels.get("recruit_type") == "校园招聘"
    labels2 = extract_labeled_fields("类型：蓝色主题\n工作地点：杭州\n")
    assert "recruit_type" not in labels2
    assert labels2.get("work_location") == "杭州"
