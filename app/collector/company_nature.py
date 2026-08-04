"""基于公开搜索摘要补全企业性质；仅接受明确证据。"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup


@dataclass(frozen=True)
class CompanyNatureResult:
    nature: str | None = None
    evidence: str = ""
    source_url: str = ""
    error: str = ""


_NATURE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("事业单位", ("事业单位", "事业编制单位")),
    ("央企", ("中央企业", "央企", "国务院国资委监管", "中央直属企业")),
    ("国企", ("国有企业", "国企", "国有独资", "国有控股", "省属企业", "市属国企")),
    ("合资", ("中外合资", "合资企业", "合资公司")),
    ("外企", ("外商独资", "外资企业", "外企", "外商投资企业")),
    ("民企", ("民营企业", "民企", "民营控股", "民营公司")),
)


def _company_aliases(name: str) -> tuple[str, ...]:
    raw = re.sub(r"\s+", "", name or "").strip()
    aliases = [raw] if raw else []
    short = re.sub(
        r"(?:股份)?有限公司$|有限责任公司$|股份公司$|集团公司$|集团$|公司$",
        "",
        raw,
    ).strip()
    if len(short) >= 4 and short not in aliases:
        aliases.append(short)
    return tuple(aliases)


def _rss_items(xml_text: str) -> list[tuple[str, str]]:
    root = ET.fromstring(xml_text)
    items: list[tuple[str, str]] = []
    for item in root.findall(".//item"):
        title = item.findtext("title") or ""
        desc = item.findtext("description") or ""
        link = item.findtext("link") or ""
        text = html.unescape(re.sub(r"<[^>]+>", " ", f"{title} {desc}"))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            items.append((text, link.strip()))
    return items


def _sogou_items(page_html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(page_html or "", "lxml")
    items: list[tuple[str, str]] = []
    for block in soup.select("div.vrwrap"):
        text = re.sub(r"\s+", " ", block.get_text(" ", strip=True)).strip()
        link = ""
        anchor = block.find("a", href=True)
        if anchor is not None:
            link = str(anchor.get("href") or "").strip()
        if text:
            items.append((text, link))
    return items


def infer_company_nature_from_items(
    company_name: str,
    items: list[tuple[str, str]],
) -> CompanyNatureResult:
    """从含公司名的搜索摘要判定；类别冲突时不返回结果。"""
    aliases = _company_aliases(company_name)
    evidence_by_nature: dict[str, list[tuple[str, str]]] = {}
    for text, url in items:
        compact = re.sub(r"\s+", "", text)
        if aliases and not any(alias in compact for alias in aliases):
            continue
        for nature, phrases in _NATURE_RULES:
            if any(phrase in text for phrase in phrases):
                evidence_by_nature.setdefault(nature, []).append((text, url))
                # 央企文本通常也会写“国企”，以更具体的首个类别为准。
                break

    if not evidence_by_nature:
        return CompanyNatureResult()
    ranked = sorted(evidence_by_nature.items(), key=lambda item: len(item[1]), reverse=True)
    if len(ranked) > 1 and len(ranked[0][1]) == len(ranked[1][1]):
        return CompanyNatureResult(error="搜索摘要存在相互冲突的企业性质")
    nature, evidence = ranked[0]
    sample, source_url = evidence[0]
    return CompanyNatureResult(nature=nature, evidence=sample[:300], source_url=source_url)


def search_company_nature(
    company_name: str,
    *,
    timeout: float = 5.0,
    fetcher: Callable[[str, float], str] | None = None,
) -> CompanyNatureResult:
    """通过中文搜索结果摘要补全性质；网络失败时返回空结果。"""
    name = (company_name or "").strip()
    if not name:
        return CompanyNatureResult(error="公司名称为空")
    aliases = _company_aliases(name)
    search_name = aliases[-1] if aliases else name
    query = quote_plus(f"{search_name} 企业性质")
    search_url = f"https://www.sogou.com/web?query={query}"
    try:
        if fetcher is not None:
            body = fetcher(search_url, float(timeout))
        else:
            with httpx.Client(
                timeout=max(1.0, float(timeout)),
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 campus-jobs/1.0"},
            ) as client:
                response = client.get(search_url)
                response.raise_for_status()
                body = response.text
        stripped = (body or "").lstrip()
        items = _rss_items(body) if stripped.startswith("<?xml") or stripped.startswith("<rss") else _sogou_items(body)
        return infer_company_nature_from_items(name, items)
    except Exception as exc:  # noqa: BLE001 - enrichment must never stop collection
        return CompanyNatureResult(error=str(exc)[:300])
