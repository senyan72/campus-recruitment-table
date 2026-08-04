"""轻量 xlsx 读取（绕过样式解析问题）。"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


@dataclass
class SheetInfo:
    name: str
    path: str


def list_sheets(xlsx_path: Path | str) -> list[str]:
    return [s.name for s in _sheet_infos(Path(xlsx_path))]


def iter_sheet_rows(xlsx_path: Path | str, sheet_name: str) -> Iterator[list[str]]:
    path = Path(xlsx_path)
    infos = {s.name: s for s in _sheet_infos(path)}
    if sheet_name not in infos:
        raise KeyError(f"未找到 sheet: {sheet_name}")
    with zipfile.ZipFile(path) as z:
        shared = _load_shared_strings(z)
        yield from _iter_rows(z, infos[sheet_name].path, shared)


def iter_all_sheets(xlsx_path: Path | str) -> Iterator[tuple[str, list[list[str]]]]:
    path = Path(xlsx_path)
    with zipfile.ZipFile(path) as z:
        shared = _load_shared_strings(z)
        for info in _sheet_infos(path):
            rows = list(_iter_rows(z, info.path, shared))
            yield info.name, rows


def _sheet_infos(path: Path) -> list[SheetInfo]:
    with zipfile.ZipFile(path) as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rid_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
        out: list[SheetInfo] = []
        for sh in wb.findall("m:sheets/m:sheet", NS):
            name = sh.attrib.get("name") or ""
            rid = sh.attrib.get(REL_NS)
            target = rid_map.get(rid or "", "")
            if not target.startswith("xl/"):
                target = "xl/" + target.lstrip("/")
            out.append(SheetInfo(name=name, path=target))
        return out


def _load_shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    out: list[str] = []
    for si in root.findall("m:si", NS):
        texts = [t.text or "" for t in si.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")]
        out.append("".join(texts))
    return out


def _iter_rows(z: zipfile.ZipFile, sheet_path: str, shared: list[str]) -> Iterator[list[str]]:
    root = ET.fromstring(z.read(sheet_path))
    for row in root.findall("m:sheetData/m:row", NS):
        cells: dict[int, str] = {}
        max_idx = -1
        for c in row.findall("m:c", NS):
            ref = c.attrib.get("r", "A1")
            col = _col_index(ref)
            max_idx = max(max_idx, col)
            cells[col] = _cell_value(c, shared)
        if max_idx < 0:
            yield []
            continue
        yield [cells.get(i, "") for i in range(max_idx + 1)]


def _col_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha())
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch.upper()) - ord("A") + 1)
    return n - 1


def _cell_value(c: ET.Element, shared: list[str]) -> str:
    t = c.attrib.get("t")
    if t == "inlineStr":
        is_el = c.find("m:is", NS)
        if is_el is None:
            return ""
        texts = [x.text or "" for x in is_el.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")]
        return "".join(texts)
    v = c.find("m:v", NS)
    if v is None or v.text is None:
        return ""
    if t == "s":
        try:
            return shared[int(v.text)]
        except (ValueError, IndexError):
            return ""
    return v.text
