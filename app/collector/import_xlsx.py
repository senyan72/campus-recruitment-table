"""从本地汇总 xlsx 导入 companies 种子（禁止直接发布旧岗位）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.collector.filters import (
    clean_company_nature,
    is_trusted_ats_url,
    looks_like_url,
    prefer_apply_urls,
    should_skip_sheet,
)
from app.collector.xlsx_reader import iter_sheet_rows, list_sheets
from app.db.local import LocalDB, normalize_company_name


ProgressCb = Callable[[str], None]


@dataclass
class ImportResult:
    sheets_seen: list[str] = field(default_factory=list)
    sheets_skipped: list[str] = field(default_factory=list)
    companies_upserted: int = 0
    rows_read: int = 0
    rows_skipped: int = 0
    path: str = ""


@dataclass
class FileImportResult:
    """单文件导入汇报。"""

    path: str
    sheets_seen: list[str] = field(default_factory=list)
    sheets_skipped: list[str] = field(default_factory=list)
    companies_upserted: int = 0
    rows_read: int = 0
    rows_skipped: int = 0
    companies_before: int = 0
    companies_after: int = 0
    error: str | None = None

    @property
    def companies_delta(self) -> int:
        return self.companies_after - self.companies_before


@dataclass
class MultiImportResult:
    """多文件导入汇总。"""

    files: list[FileImportResult] = field(default_factory=list)
    companies_before: int = 0
    companies_after: int = 0

    @property
    def companies_delta(self) -> int:
        return self.companies_after - self.companies_before

    @property
    def companies_upserted(self) -> int:
        return sum(f.companies_upserted for f in self.files)

    def summary_lines(self) -> list[str]:
        lines = [
            f"合计：文件 {len(self.files)}，companies 增量 {self.companies_delta}"
            f"（{self.companies_before} → {self.companies_after}），"
            f"写入/更新行 {self.companies_upserted}"
        ]
        for f in self.files:
            name = Path(f.path).name
            if f.error:
                lines.append(f"· {name}：失败 — {f.error}")
                continue
            lines.append(
                f"· {name}：处理 sheet {len(f.sheets_seen)}，跳过 sheet {len(f.sheets_skipped)}，"
                f"写入/更新 {f.companies_upserted}，companies 增量 {f.companies_delta}"
            )
        return lines


# 各 sheet 可能的表头别名 → 标准字段
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("公司/单位名称", "招聘单位", "公司名称", "单位名称", "企业名称"),
    "company_nature": ("企业性质",),
    "industry": ("行业分类（来自企查查数据）", "行业分类", "行业"),
    "hint_apply": ("网申/投递地址", "投递地址", "网申地址"),
    "source_url": ("公告原文链接", "招聘原文", "原文链接"),
    "official_site": ("官方网站", "官网"),
    "title": ("官方标题", "招聘标题", "标题"),
    "recruit_project": ("招聘项目", "校招类型"),
}


def _norm_header(h: str) -> str:
    return (h or "").strip().replace("\n", "").replace(" ", "")


def _find_header_row(rows: list[list[str]], max_scan: int = 15) -> tuple[int, dict[str, int]] | None:
    for i, row in enumerate(rows[:max_scan]):
        mapping: dict[str, int] = {}
        normed = [_norm_header(c) for c in row]
        for field, aliases in HEADER_ALIASES.items():
            for alias in aliases:
                a = _norm_header(alias)
                for idx, cell in enumerate(normed):
                    if cell == a or (a and a in cell):
                        mapping[field] = idx
                        break
                if field in mapping:
                    break
        if "name" in mapping:
            return i, mapping
    return None


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def row_to_company(row: list[str], colmap: dict[str, int], sheet_name: str) -> dict[str, Any] | None:
    name = _cell(row, colmap.get("name"))
    if not name or len(name) < 2:
        return None
    if name.startswith("offer") or "注意事项" in name:
        return None
    nature = clean_company_nature(_cell(row, colmap.get("company_nature")))
    industry = _cell(row, colmap.get("industry")) or None
    hints: list[str] = []
    for key in ("hint_apply", "source_url", "official_site"):
        url = _cell(row, colmap.get(key))
        if looks_like_url(url):
            hints.append(url.strip())
    hints = prefer_apply_urls(hints, limit=12)
    ats = [u for u in hints if is_trusted_ats_url(u)]
    # 有可信 ATS 网申时直接标 official，流水线可立刻采集，无需先人工源验证
    verify = "official" if ats else "unverified"
    return {
        "name": name,
        "name_norm": normalize_company_name(name),
        "company_nature": nature,
        "industry": industry,
        "hint_apply_urls": hints,
        "career_urls": ats or [],
        "source_sheets": [sheet_name],
        "verify_status": verify,
        "notes": "xlsx 种子；禁止直接发布旧岗",
    }


def import_xlsx_to_companies(
    db: LocalDB,
    xlsx_path: Path | str,
    *,
    sheet_names: list[str] | None = None,
    limit_per_sheet: int | None = None,
    progress: ProgressCb | None = None,
) -> ImportResult:
    """全量导入相关 sheet 并去重写入 companies。绝不写入 jobs。"""
    path = Path(xlsx_path)
    if not path.exists():
        raise FileNotFoundError(f"找不到种子文件: {path}")

    result = ImportResult(path=str(path))
    all_sheets = list_sheets(path)
    targets = sheet_names or all_sheets

    for sheet in targets:
        if sheet not in all_sheets:
            result.sheets_skipped.append(sheet)
            continue
        if should_skip_sheet(sheet):
            result.sheets_skipped.append(sheet)
            if progress:
                progress(f"跳过 sheet: {sheet}")
            continue

        result.sheets_seen.append(sheet)
        if progress:
            progress(f"导入 sheet: {sheet}")

        # 先读出前若干行找表头，再继续迭代（轻量实现：读全表对内存可接受）
        rows = list(iter_sheet_rows(path, sheet))
        found = _find_header_row(rows)
        if not found:
            result.rows_skipped += len(rows)
            if progress:
                progress(f"未识别表头，跳过: {sheet}")
            continue
        header_idx, colmap = found
        data_rows = rows[header_idx + 1 :]
        if limit_per_sheet is not None:
            data_rows = data_rows[:limit_per_sheet]

        for row in data_rows:
            result.rows_read += 1
            company = row_to_company(row, colmap, sheet)
            if not company:
                result.rows_skipped += 1
                continue
            db.upsert_company(company)
            result.companies_upserted += 1

    if progress:
        progress(
            f"完成：sheet={len(result.sheets_seen)} 写入/更新行={result.companies_upserted} "
            f"跳过 sheet={result.sheets_skipped}"
        )
    return result


def import_xlsx_paths_to_companies(
    db: LocalDB,
    xlsx_paths: list[Path | str],
    *,
    sheet_names: list[str] | None = None,
    limit_per_sheet: int | None = None,
    progress: ProgressCb | None = None,
) -> MultiImportResult:
    """依次导入多份 xlsx；全量去重写入 companies，绝不写入 jobs。"""
    multi = MultiImportResult(companies_before=db.count_companies())
    for raw in xlsx_paths:
        path = Path(raw)
        before = db.count_companies()
        if progress:
            progress(f"导入文件: {path.name}")
        try:
            one = import_xlsx_to_companies(
                db,
                path,
                sheet_names=sheet_names,
                limit_per_sheet=limit_per_sheet,
                progress=progress,
            )
            multi.files.append(
                FileImportResult(
                    path=str(path),
                    sheets_seen=list(one.sheets_seen),
                    sheets_skipped=list(one.sheets_skipped),
                    companies_upserted=one.companies_upserted,
                    rows_read=one.rows_read,
                    rows_skipped=one.rows_skipped,
                    companies_before=before,
                    companies_after=db.count_companies(),
                )
            )
        except Exception as exc:  # noqa: BLE001
            multi.files.append(
                FileImportResult(
                    path=str(path),
                    companies_before=before,
                    companies_after=db.count_companies(),
                    error=str(exc),
                )
            )
            if progress:
                progress(f"导入失败 {path.name}: {exc}")
    multi.companies_after = db.count_companies()
    if progress:
        for line in multi.summary_lines():
            progress(line)
    return multi
