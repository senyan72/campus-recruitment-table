"""职位描述 / 我们需要你 等模糊 JD 分段。"""

from __future__ import annotations

from pathlib import Path

from app.collector.filters import is_noise_title
from app.collector.label_fields import (
    extract_labeled_fields,
    split_jd_sections,
)
from app.ui.job_review import display_jd_sections

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_split_jd_sections_zhiwei_miaoshu_fixture():
    jd = (FIXTURES / "jd_zhiwei_miaoshu.txt").read_text(encoding="utf-8")
    duties, reqs = split_jd_sections(jd)
    assert "数字孪生" in duties
    assert "故障预警" in duties
    assert "硕士" in reqs
    assert "Python" in reqs
    labels = extract_labeled_fields(jd)
    assert "数字孪生" in (labels.get("jd_duties") or "")
    assert "硕士" in (labels.get("jd_requirements") or "")


def test_display_jd_sections_zhiwei_miaoshu():
    jd = (FIXTURES / "jd_zhiwei_miaoshu.txt").read_text(encoding="utf-8")
    duties, reqs = display_jd_sections(jd)
    assert "数字孪生" in duties
    assert "硕士" in reqs


def test_split_envision_style_women_need_you():
    jd = (
        "我们需要你：\n"
        "1、负责风机系统智能运营方法开发；\n"
        "2、建立故障预警模型。\n"
        "我们期望你：\n"
        "1、硕士以上学位；\n"
        "2、熟悉 Python。\n"
    )
    duties, reqs = split_jd_sections(jd)
    assert "智能运营" in duties
    assert "故障预警" in duties
    assert "硕士" in reqs
    assert "Python" in reqs


def test_unlabeled_substantial_jd_goes_to_duties():
    jd = (
        "算法工程师\n"
        "校园招聘\n"
        "负责数据平台建设与模型训练，参与线上服务稳定性保障，"
        "与产品团队协作推进需求落地，并输出技术文档。"
    )
    duties, reqs = split_jd_sections(jd)
    assert "数据平台" in duties
    assert reqs == "" or "数据平台" not in reqs


def test_placeholder_xx_title_is_noise():
    assert is_noise_title("xx")
    assert is_noise_title("XXX")
    assert is_noise_title("--")
    assert not is_noise_title("会计")
    assert not is_noise_title("算法工程师")
