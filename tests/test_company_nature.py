from app.collector.company_nature import (
    infer_company_nature_from_items,
    search_company_nature,
)
from app.collector.filters import recover_location_from_jd
from app.collector.label_fields import extract_labeled_fields, split_jd_sections
from app.db.local import LocalDB


def test_company_nature_requires_named_explicit_evidence():
    result = infer_company_nature_from_items(
        "科大讯飞股份有限公司",
        [("科大讯飞是一家民营企业，主营人工智能产品。", "https://example.com/a")],
    )
    assert result.nature == "民企"
    assert "科大讯飞" in result.evidence

    unrelated = infer_company_nature_from_items(
        "科大讯飞股份有限公司",
        [("某中央企业发布校园招聘公告。", "https://example.com/b")],
    )
    assert unrelated.nature is None


def test_company_nature_rss_fetch_and_db_sync(tmp_path):
    rss = """<?xml version="1.0"?><rss><channel><item>
    <title>测试科技企业简介</title>
    <description>测试科技有限公司为国有控股企业。</description>
    <link>https://example.com/profile</link>
    </item></channel></rss>"""
    result = search_company_nature(
        "测试科技有限公司",
        fetcher=lambda _url, _timeout: rss,
    )
    assert result.nature == "国企"

    db = LocalDB(tmp_path / "nature.db")
    company_id = db.upsert_company({"name": "测试科技有限公司"})
    job_id = db.upsert_job(
        {
            "company_id": company_id,
            "company": "测试科技有限公司",
            "title": "算法工程师",
            "source_url": "https://example.com/job/1",
            "apply_url": "https://example.com/job/1",
            "recruit_bucket": "校招",
        }
    )
    assert db.set_company_nature(company_id, result.nature or "") == 1
    assert db.get_company(company_id)["company_nature"] == "国企"
    assert db.get_job(job_id)["company_nature"] == "国企"


def test_location_and_fuzzy_jd_section_aliases():
    assert recover_location_from_jd("办公地点：安徽省·合肥市\n岗位职责：研发") == "安徽省·合肥市"

    text = (
        "1. 核心职责：\n负责平台架构设计。\n"
        "2. 能力要求：\n本科及以上，熟悉 Python。\n"
        "公司介绍：\n这里不属于任职要求。"
    )
    labels = extract_labeled_fields(text)
    assert "平台架构" in labels["jd_duties"]
    assert "本科及以上" in labels["jd_requirements"]
    duties, requirements = split_jd_sections(text)
    assert "平台架构" in duties
    assert "本科及以上" in requirements
    assert "公司介绍" not in requirements
