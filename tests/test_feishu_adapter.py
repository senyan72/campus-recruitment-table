from pathlib import Path

from app.collector.adapters import feishu
from app.collector.filters import is_noise_title

FIXTURES = Path(__file__).parent / "fixtures"


def test_extract_detail_links_filters_nav():
    html = (FIXTURES / "feishu_list.html").read_text(encoding="utf-8")
    base = "https://demo.jobs.feishu.cn/campus/position"
    links = feishu.extract_detail_links(base, html)
    assert len(links) == 3
    assert all("/position/" in u and u.endswith("/detail") for u in links)
    assert not any("login" in u or "register" in u for u in links)


def test_parse_feishu_detail_four_fields():
    html = (FIXTURES / "feishu_detail.html").read_text(encoding="utf-8")
    url = "https://demo.jobs.feishu.cn/campus/position/10001/detail"
    result = feishu.parse_feishu_detail(url, html)
    assert result.title == "后端开发工程师"
    assert result.jd_text and "岗位介绍" in result.jd_text
    assert result.recruit_project in ("正式", "校园招聘") or result.recruit_bucket == "校招"
    assert result.work_location == "北京"
    assert result.raw_category == "技术类"


def test_job_post_to_result_from_api_shape():
    item = {
        "id": "9001",
        "title": "算法实习生",
        "description": "参与推荐算法迭代与效果评估。" * 3,
        "requirement": "熟悉 Python 与机器学习基础。",
        "city_list": [{"name": "杭州"}, {"name": "北京"}],
        "recruit_type": {"name": "实习"},
        "job_category": {"name": "算法"},
    }
    r = feishu.job_post_to_result(
        item, apply_url="https://demo.jobs.feishu.cn/campus/position/9001/detail"
    )
    assert r.title == "算法实习生"
    assert r.work_location == "杭州 / 北京"
    assert r.recruit_bucket == "日常实习"
    assert r.jd_text and "推荐算法" in r.jd_text
    assert not is_noise_title(r.title)


def test_card_meta_and_channel():
    city, typ, cat = feishu.parse_card_meta_from_text("广州 | 实习 | 运营类")
    assert city == "广州"
    assert typ == "实习"
    assert cat == "运营类"
    host, channel = feishu.extract_host_channel(
        "https://vrfi1sk8a0.jobs.feishu.cn/379481/position/1/detail"
    )
    assert host.startswith("vrfi")
    assert channel == "379481"
