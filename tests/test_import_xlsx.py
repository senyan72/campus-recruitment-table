from pathlib import Path

from app.collector.import_xlsx import row_to_company
from app.db.local import LocalDB


def test_row_to_company_cleans_nature():
    colmap = {"name": 0, "company_nature": 1, "hint_apply": 2, "industry": 3}
    row = ["测试科技", "私企", "https://example.com/a", "互联网"]
    company = row_to_company(row, colmap, "春招汇总表")
    assert company is not None
    assert company["name"] == "测试科技"
    assert company["company_nature"] == "私企"
    assert company["hint_apply_urls"] == ["https://example.com/a"]

    dirty = ["脏数据公司", "2025校招启动公告标题很长", "", ""]
    cleaned = row_to_company(dirty, colmap, "春招汇总表")
    assert cleaned is not None
    assert cleaned["company_nature"] is None


def test_company_dedupe(tmp_path: Path):
    db = LocalDB(tmp_path / "t.db")
    db.upsert_company(
        {
            "name": "海天集团",
            "company_nature": "私企",
            "hint_apply_urls": ["https://a.example"],
            "source_sheets": ["实习汇总表"],
        }
    )
    db.upsert_company(
        {
            "name": "海天集团",
            "hint_apply_urls": ["https://b.example"],
            "source_sheets": ["春招汇总表"],
        }
    )
    assert db.count_companies() == 1
    company = db.list_companies(limit=10)[0]
    assert "https://a.example" in company["hint_apply_urls"]
    assert "https://b.example" in company["hint_apply_urls"]
