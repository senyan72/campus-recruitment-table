"""官方公众号 RSS 采集（依赖外部 RSS 桥）。"""

from __future__ import annotations

import re
from typing import Any, Callable
from xml.etree import ElementTree as ET

import httpx

from app.collector.adapters.router import DEFAULT_HEADERS, parse_job_url
from app.collector.extract import (
    compute_confidence,
    route_auto_publish_failure,
    should_auto_publish,
    text_is_aggregator,
)
from app.collector.filters import map_recruit_bucket
from app.db.local import LocalDB

ProgressCb = Callable[[str], None]


def fetch_rss_items(feed_url: str, timeout: float = 20.0) -> list[dict[str, str]]:
    with httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=timeout) as client:
        resp = client.get(feed_url)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    items: list[dict[str, str]] = []
    # RSS 2.0
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        if title and link:
            items.append({"title": title, "link": link, "description": desc})
    # Atom
    if not items:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("a:entry", ns):
            title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
            link_el = entry.find("a:link", ns)
            link = (link_el.attrib.get("href") if link_el is not None else "") or ""
            summary = (entry.findtext("a:summary", default="", namespaces=ns) or "").strip()
            if title and link:
                items.append({"title": title, "link": link, "description": summary})
    return items


def build_wechat_feed_url(bridge_base: str, wechat_name: str) -> str:
    base = bridge_base.rstrip("/")
    # 常见 wewe-rss / wechat2rss 约定：/feeds/{name}.xml 或 /feed/{name}
    if "{name}" in base:
        return base.replace("{name}", wechat_name)
    return f"{base}/{wechat_name}.xml"


def collect_company_rss(
    db: LocalDB,
    company: dict[str, Any],
    feed_url: str,
    *,
    block_patterns: list[str] | None = None,
) -> dict[str, int]:
    stats = {"parsed": 0, "published": 0, "queued": 0, "errors": 0}
    try:
        items = fetch_rss_items(feed_url)
    except Exception as exc:  # noqa: BLE001
        db.enqueue_review(
            "rss_error",
            {"company_id": company["id"], "feed": feed_url, "error": str(exc)},
            "RSS 拉取失败",
        )
        stats["errors"] += 1
        return stats

    for item in items[:15]:
        title = item["title"]
        link = item["link"]
        if text_is_aggregator(title, item.get("description"), block_patterns):
            stats["queued"] += 1
            db.enqueue_review("aggregator", {"title": title, "url": link}, "RSS 命中汇总特征")
            continue
        from app.collector.filters import (
            is_closed_or_referral_jd,
            is_noise_title,
            is_social_hiring,
            is_target_campus_or_intern,
            resolve_grad_batch,
            title_company_mismatch,
        )

        if is_noise_title(title):
            continue
        # 仅处理疑似招聘文
        if not re.search(r"招聘|校招|实习|秋招|春招|管培|启航|星计划", title):
            continue
        result = parse_job_url(link, fetch=True)
        stats["parsed"] += 1
        job_title = result.title or title
        jd_blob = result.jd_text or item.get("description")
        if is_noise_title(job_title) or is_closed_or_referral_jd(jd_blob):
            continue
        if title_company_mismatch(company.get("name"), job_title):
            db.enqueue_review(
                "low_confidence",
                {"title": job_title, "url": link, "company": company.get("name")},
                "噪声标题或公司名不符",
            )
            stats["queued"] += 1
            continue
        if is_social_hiring(
            title=job_title,
            jd_text=result.jd_text if isinstance(result.jd_text, str) else jd_blob,
            recruit_project=result.recruit_project,
        ):
            project = result.recruit_project or "社会招聘"
            bucket = None if result.recruit_bucket == "校招" else result.recruit_bucket
        else:
            project = result.recruit_project or "校园招聘"
            bucket = result.recruit_bucket or map_recruit_bucket(project, title)
        aggregator = False
        conf = compute_confidence(
            title=job_title,
            source_url=link,
            jd_text=result.jd_text,
            recruit_bucket=bucket,
            official_source=(company.get("verify_status") == "official"),
            aggregator=aggregator,
            company=company.get("name"),
        )
        batch = resolve_grad_batch(title=job_title, jd_text=jd_blob if isinstance(jd_blob, str) else None)
        job = {
            "company_id": company["id"],
            "company": company["name"],
            "company_nature": company.get("company_nature"),
            "industry": company.get("industry"),
            "title": job_title,
            "source_url": link,
            "apply_url": result.apply_url or link,
            "recruit_project": project,
            "recruit_bucket": bucket,
            "work_location": result.work_location,
            "open_at": (result.extras or {}).get("published_at")
            if isinstance(result.extras, dict)
            else None,
            "graduation_batch": batch,
            "jd_text": jd_blob,
            "job_tags": result.job_tags,
            "parse_status": result.parse_status,
            "confidence": conf,
            "status": "pending_review",
        }
        ok_target, kind, target_reason = is_target_campus_or_intern(
            title=job_title,
            jd_text=jd_blob,
            recruit_bucket=bucket,
            recruit_project=project,
            graduation_batch=batch,
            open_at=job.get("open_at"),
            published_at=job.get("open_at"),
        )
        if not ok_target:
            db.enqueue_review(kind or "not_target_hiring", job, target_reason)
            stats["queued"] += 1
            continue
        ok, reason = should_auto_publish(
            verify_status=company.get("verify_status") or "unverified",
            title=job["title"],
            source_url=link,
            recruit_project=project,
            confidence=conf,
            aggregator=aggregator,
            company=company.get("name"),
            jd_text=jd_blob,
        )
        if ok:
            db.upsert_job(job)
            stats["published"] += 1
        else:
            # 源未验证 → 源验证；内容/规则问题 → 异常队列
            route_auto_publish_failure(db, company, job, reason, stats)
    return stats


def run_rss_collect(
    db: LocalDB,
    bridge_base: str,
    *,
    limit_companies: int = 30,
    progress: ProgressCb | None = None,
) -> dict[str, int]:
    if not bridge_base:
        return {"parsed": 0, "published": 0, "queued": 0, "errors": 0}
    companies = [
        c
        for c in db.list_companies(verify_status="official", limit=limit_companies * 2)
        if c.get("wechat_name")
    ][:limit_companies]
    block = db.list_blocklist()
    total = {"parsed": 0, "published": 0, "queued": 0, "errors": 0}
    for i, company in enumerate(companies, 1):
        feed = build_wechat_feed_url(bridge_base, company["wechat_name"])
        if progress:
            progress(f"采集公众号 RSS ({i}/{len(companies)}): {company['name']}")
        stats = collect_company_rss(db, company, feed, block_patterns=block)
        for k in total:
            total[k] += stats[k]
    return total
