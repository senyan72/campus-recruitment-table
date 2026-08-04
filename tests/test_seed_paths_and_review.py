"""多路径种子配置 / 导入汇总 / 审核状态变更。"""

from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

from app.collector.import_xlsx import (
    MultiImportResult,
    import_xlsx_paths_to_companies,
)
from app.collector.pipeline import publish_review_item
from app.config import resolve_seed_xlsx_paths
from app.db.local import LocalDB
from app.ui.job_editor import merge_job_fields


def test_resolve_seed_xlsx_paths_merges_and_dedupes():
    cfg = {
        "seed_xlsx_path": r"C:\a\春招.xlsx",
        "seed_xlsx_paths": [
            r"C:\a\春招.xlsx",
            r"C:\a\秋招.xlsx",
            r"C:\a\春招.xlsx",
        ],
    }
    paths = resolve_seed_xlsx_paths(cfg)
    assert paths == [r"C:\a\春招.xlsx", r"C:\a\秋招.xlsx"]


def test_resolve_seed_xlsx_paths_string_list():
    cfg = {
        "seed_xlsx_path": "",
        "seed_xlsx_paths": "C:/x/a.xlsx\nC:/x/b.xlsx;C:/x/a.xlsx",
    }
    paths = resolve_seed_xlsx_paths(cfg)
    assert paths == ["C:/x/a.xlsx", "C:/x/b.xlsx"]


def test_resolve_seed_xlsx_paths_fallback_single():
    cfg = {"seed_xlsx_path": r"C:\only.xlsx", "seed_xlsx_paths": []}
    assert resolve_seed_xlsx_paths(cfg) == [r"C:\only.xlsx"]


def _minimal_company_xlsx(path: Path, company: str, sheet: str = "春招汇总表") -> None:
    """写入可被 xlsx_reader 识别的最小 workbook（单 sheet）。"""
    # Shared strings + worksheet with header + one data row
    header = [
        "公司/单位名称",
        "企业性质",
        "网申/投递地址",
        "行业分类",
    ]
    row = [company, "私企", "https://careers.example.com/apply", "互联网"]
    # Build simple OOXML
    ss_items = header + row
    unique = []
    index = {}
    for s in ss_items:
        if s not in index:
            index[s] = len(unique)
            unique.append(s)
    sst = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
    sst.append(
        f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{len(ss_items)}" uniqueCount="{len(unique)}">'
    )
    for s in unique:
        sst.append(f"<si><t>{s}</t></si>")
    sst.append("</sst>")

    def cell_ref(r: int, c: int) -> str:
        return f"{chr(ord('A') + c)}{r}"

    sheet_rows = []
    for r_i, values in enumerate((header, row), start=1):
        cells = []
        for c_i, val in enumerate(values):
            cells.append(
                f'<c r="{cell_ref(r_i, c_i)}" t="s"><v>{index[val]}</v></c>'
            )
        sheet_rows.append(f'<row r="{r_i}">{"".join(cells)}</row>')
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{sheet}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
        'Target="sharedStrings.xml"/>'
        "</Relationships>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        "</Types>"
    )
    with ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        zf.writestr("xl/sharedStrings.xml", "\n".join(sst))


def test_import_xlsx_paths_list_and_delta(tmp_path: Path):
    db = LocalDB(tmp_path / "seed.db")
    a = tmp_path / "spring.xlsx"
    b = tmp_path / "autumn.xlsx"
    _minimal_company_xlsx(a, "甲公司", "春招汇总表")
    _minimal_company_xlsx(b, "乙公司", "25届秋招汇总表")
    missing = tmp_path / "nope.xlsx"

    result = import_xlsx_paths_to_companies(db, [a, b, missing])
    assert isinstance(result, MultiImportResult)
    assert len(result.files) == 3
    assert result.files[0].error is None
    assert result.files[1].error is None
    assert result.files[2].error is not None
    assert result.companies_delta == 2
    assert db.count_companies() == 2
    assert db.count_jobs() == 0
    summary = "\n".join(result.summary_lines())
    assert "companies 增量 2" in summary
    assert "处理 sheet 1" in summary


def test_review_edit_publish_and_soft_delete(tmp_path: Path):
    db = LocalDB(tmp_path / "review.db")
    rid = db.enqueue_review(
        "low_confidence",
        {
            "company": "测试企业",
            "title": "旧标题",
            "source_url": "https://example.com/job/old",
            "recruit_bucket": "校招",
            "work_location": "上海",
        },
        "置信度偏低",
    )
    item = db.get_review_item(rid)
    assert item is not None
    payload = dict(item["payload"])
    edited = merge_job_fields(
        payload,
        {
            "company": "测试企业",
            "title": "新岗位名",
            "recruit_project": "2026秋招",
            "recruit_bucket": "校招",
            "work_location": "北京",
            "deadline": "2026-12-31",
            "source_url": "https://example.com/job/new",
            "apply_url": "https://example.com/apply",
            "jd_text": "负责后端开发",
        },
    )
    assert db.update_review_payload(rid, edited) is True
    assert publish_review_item(db, rid) is True
    jobs = db.list_jobs(status="active")
    assert len(jobs) == 1
    assert jobs[0]["title"] == "新岗位名"
    assert jobs[0]["work_location"] == "北京"
    assert jobs[0]["status"] == "active"
    # 已发布的队列项不能再改
    assert db.update_review_payload(rid, edited) is False

    jid = jobs[0]["id"]
    assert db.soft_delete_jobs([jid]) == 1
    assert db.count_jobs(status="active") == 0
    deleted = db.get_job(jid)
    assert deleted is not None
    assert deleted["status"] == "deleted"


def test_publish_review_with_override(tmp_path: Path):
    db = LocalDB(tmp_path / "ov.db")
    rid = db.enqueue_review(
        "paste",
        {"company": "A", "title": "T1", "source_url": "https://example.com/1"},
        "人工粘贴待审",
    )
    ok = publish_review_item(
        db,
        rid,
        payload_override={
            "company": "A公司",
            "title": "正式标题",
            "source_url": "https://example.com/1",
            "recruit_bucket": "校招",
        },
    )
    assert ok is True
    job = db.list_jobs()[0]
    assert job["title"] == "正式标题"
    assert job["company"] == "A公司"
