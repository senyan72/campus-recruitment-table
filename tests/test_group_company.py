"""集团 / 子公司判定：门户种子上识别到其他招聘单位 → 子公司。"""

from __future__ import annotations

from app.collector.filters import (
    companies_same,
    format_group_label,
    resolve_group_and_company,
)
from app.ui.job_review import display_group_name, review_row_values


def test_resolve_minmetals_subsidiary():
    group, company = resolve_group_and_company(
        seed_company_name="中国五矿集团有限公司",
        page_company="长沙矿山研究院有限责任公司",
    )
    assert group == "中国五矿集团"
    assert company == "长沙矿山研究院有限责任公司"


def test_resolve_same_entity_no_group():
    group, company = resolve_group_and_company(
        seed_company_name="中国五矿集团有限公司",
        page_company="中国五矿",
    )
    assert group == ""
    assert "五矿" in company


def test_resolve_no_page_company_uses_seed():
    group, company = resolve_group_and_company(
        seed_company_name="字节跳动",
        page_company=None,
    )
    assert group == ""
    assert company == "字节跳动"


def test_companies_same_strips_suffix():
    assert companies_same("海天集团股份有限公司", "海天集团")
    assert not companies_same("中国五矿集团有限公司", "长沙矿山研究院有限责任公司")


def test_format_group_label():
    assert format_group_label("中国五矿集团有限公司") == "中国五矿集团"


def test_display_group_no_longer_guesses_from_company_alone():
    # 公司名自带「集团」但无门户种子/入库字段 → 不凑集团列
    assert display_group_name({"company": "海天集团股份有限公司"}) == ""
    assert display_group_name({"group_name": "中国五矿集团", "company": "子公司"}) == "中国五矿集团"
    assert (
        display_group_name(
            {"company": "长沙矿山研究院有限责任公司"},
            seed_company_name="中国五矿集团有限公司",
        )
        == "中国五矿集团"
    )


def test_review_row_group_company():
    row = review_row_values(
        {
            "group_name": "中国五矿集团",
            "company": "长沙矿山研究院有限责任公司",
            "title": "科技研发岗",
            "updated_at": "2026-08-01T12:00:00",
            "source_url": "https://example.com/j",
        }
    )
    assert row[0] == "中国五矿集团"
    assert row[1] == "长沙矿山研究院有限责任公司"
