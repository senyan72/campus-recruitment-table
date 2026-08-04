"""官方源探测与校验。"""

from __future__ import annotations

from typing import Any, Callable

import httpx

from app.collector.adapters.router import DEFAULT_HEADERS
from app.collector.extract import host_of
from app.collector.filters import (
    is_aggregator_text,
    is_trusted_ats_url,
    looks_like_url,
    parse_url_list,
    prefer_apply_urls,
)
from app.db.local import LocalDB

ProgressCb = Callable[[str], None]

CAREER_PATHS = (
    "/campus",
    "/campus/",
    "/recruit",
    "/recruit/",
    "/career",
    "/careers",
    "/school",
    "/joinus",
    "/about/recruit",
    "/jobs",
)


def probe_url(url: str, timeout: float = 12.0) -> tuple[bool, int, str]:
    """返回 (可达, status_code, final_url)。"""
    try:
        with httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=timeout) as client:
            resp = client.get(url)
            return 200 <= resp.status_code < 400, resp.status_code, str(resp.url)
    except Exception:
        return False, 0, url


def guess_career_urls_from_hints(hints: list[str]) -> list[str]:
    out: list[str] = []
    for h in hints:
        if not looks_like_url(h):
            continue
        out.append(h.strip())
        # ATS/详情链接本身即可，不必再猜路径
        if is_trusted_ats_url(h):
            continue
        from urllib.parse import urljoin, urlparse

        parsed = urlparse(h)
        if parsed.scheme and parsed.netloc:
            base = f"{parsed.scheme}://{parsed.netloc}"
            for path in CAREER_PATHS:
                out.append(urljoin(base, path))
    return prefer_apply_urls(out, limit=20)


def verify_official_source(company_name: str, url: str, html_snippet: str = "") -> tuple[str, str]:
    """
    粗校验：返回 (verify_status, reason)
    official / unverified / rejected
    """
    if is_aggregator_text(f"{company_name} {url} {html_snippet}"):
        return "rejected", "命中汇总黑名单"
    host = host_of(url)
    if not host:
        return "unverified", "无效 URL"
    # 可信 ATS：种子里的网申地址可直接视为官方采集源
    if is_trusted_ats_url(url):
        return "official", "可信 ATS/网申域名"
    # 名称片段与域名弱匹配（中文公司难匹配拼音域名，仅作加分）
    name_compact = "".join(ch for ch in company_name if ch.isalnum())
    if name_compact and any(ch.lower() in host for ch in name_compact if ch.isascii() and len(ch) > 2):
        return "official", "域名与公司名弱匹配"
    # 公众号等仍需人工确认
    if "mp.weixin.qq.com" in host:
        return "unverified", "公众号链接待人工确认官方归属"
    return "unverified", "待验证官方归属"


def promote_trusted_seeds(db: LocalDB, *, limit: int = 3000) -> int:
    """
    将已有 hint_apply_urls 中含可信 ATS 的种子提升为 official，
    并写入 career_urls，使官网采集无需先人工点源验证。
    """
    companies = db.list_companies_with_urls(limit=limit, prefer_ats=True, verify_status="unverified")
    promoted = 0
    for company in companies:
        hints = parse_url_list(company.get("hint_apply_urls"))
        careers = parse_url_list(company.get("career_urls"))
        ats = [u for u in prefer_apply_urls(hints + careers, limit=10) if is_trusted_ats_url(u)]
        if not ats:
            continue
        if is_aggregator_text(f"{company.get('name') or ''} {' '.join(ats)}"):
            continue
        db.update_company_verify(company["id"], "official", ats)
        promoted += 1
    return promoted


def discover_for_company(db: LocalDB, company: dict[str, Any]) -> dict[str, Any]:
    hints = parse_url_list(company.get("hint_apply_urls"))
    candidates = guess_career_urls_from_hints(hints)
    # 可信 ATS 可跳过全量探测，直接采用
    ats_direct = [u for u in candidates if is_trusted_ats_url(u)]
    alive: list[str] = []
    if ats_direct:
        for url in ats_direct[:5]:
            ok, _, final = probe_url(url)
            alive.append(final if ok else url)
    else:
        for url in candidates:
            ok, _, final = probe_url(url)
            if ok:
                alive.append(final)

    status = company.get("verify_status") or "unverified"
    reason = "无可用链接"
    chosen = prefer_apply_urls(alive, limit=5)
    if chosen:
        status, reason = verify_official_source(company.get("name") or "", chosen[0])
        if status == "official":
            db.update_company_verify(company["id"], "official", chosen)
        elif status == "rejected":
            db.update_company_verify(company["id"], "rejected", chosen)
            db.enqueue_source_verify(company["id"], "url", chosen[0], reason)
        else:
            db.update_company_verify(company["id"], "unverified", chosen)
            db.enqueue_source_verify(company["id"], "url", chosen[0], reason)
    else:
        db.enqueue_source_verify(company["id"], "url", hints[0] if hints else "", "链接探测失败")

    return {"company_id": company["id"], "alive": chosen, "status": status, "reason": reason}


def batch_discover(
    db: LocalDB,
    *,
    limit: int = 50,
    only_unverified: bool = True,
    progress: ProgressCb | None = None,
) -> list[dict[str, Any]]:
    # 优先探测「有 hint URL / ATS」的公司，提高首轮可产出
    if only_unverified:
        companies = db.list_companies_with_urls(
            limit=limit, prefer_ats=True, verify_status="unverified"
        )
        if len(companies) < limit:
            # 补足无 URL 的 unverified（少见）
            have = {c["id"] for c in companies}
            extra = [
                c
                for c in db.list_companies(verify_status="unverified", limit=limit)
                if c["id"] not in have
            ]
            companies.extend(extra[: max(0, limit - len(companies))])
    else:
        companies = db.list_companies_with_urls(limit=limit, prefer_ats=True)

    results = []
    for i, company in enumerate(companies, 1):
        if progress:
            progress(f"探测官方源 ({i}/{len(companies)}): {company['name']}")
        results.append(discover_for_company(db, company))
    return results
