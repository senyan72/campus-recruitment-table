"""Admin 采集流水线：探测 → 官网/RSS → 日报；深度采集见 deep。"""

from __future__ import annotations

from typing import Any, Callable

from app.collector.cleanup import cleanup_expired_jobs
from app.collector.deep import run_deep_collect
from app.collector.discover import batch_discover, promote_trusted_seeds
from app.collector.filters import resolve_lookback_days
from app.collector.rss import run_rss_collect
from app.collector.website import run_website_collect
from app.db.local import LocalDB, utc_now

ProgressCb = Callable[[str], None]


def run_collect_pipeline(
    db: LocalDB,
    *,
    bridge_base: str = "",
    discover_limit: int = 60,
    collect_limit: int = 40,
    lookback_days: int | None = None,
    collect_months: int | None = None,
    retention_days: int | None = None,
    progress: ProgressCb | None = None,
) -> dict[str, Any]:
    """
    快速小测流水线：每轮限量探测/采集（默认 60/40）。
    全量近 3 个月请用 run_deep_collect_pipeline。
    """
    days = resolve_lookback_days(lookback_days=lookback_days, collect_months=collect_months)
    jobs_before = db.count_jobs()

    if progress:
        progress("提升带可信 ATS 网申的种子为 official…")
    promoted = promote_trusted_seeds(db, limit=3000)
    if progress:
        progress(f"已自动标记 official：{promoted} 家")

    if progress:
        progress("开始官方源探测（优先有 URL 的公司）…")
    discovered = batch_discover(db, limit=discover_limit, progress=progress)
    official_n = sum(1 for d in discovered if d.get("status") == "official")

    if progress:
        progress("开始官网/ATS 采集（快速小测）…")
    web = run_website_collect(
        db,
        limit_companies=collect_limit,
        lookback_days=days,
        require_real_jobs=False,
        progress=progress,
    )

    if progress:
        progress("开始公众号 RSS 采集…")
    rss = run_rss_collect(db, bridge_base, limit_companies=collect_limit, progress=progress)

    jobs_after = db.count_jobs()
    jobs_added = max(0, jobs_after - jobs_before)
    review_n = len(db.list_review_queue("pending", limit=5000))
    source_n = len(db.list_source_verify("pending", limit=5000))
    official_total = len(db.list_companies(verify_status="official", limit=5000))

    summary = (
        f"[{utc_now()}] 【快速小测】自动标记 official {promoted}；"
        f"探测 {len(discovered)} 家（本轮新官方 {official_n}）；"
        f"官网 解析{web['parsed']}/发布{web['published']}/队列{web['queued']}/"
        f"过期{web.get('stale', 0)}/错{web['errors']}；"
        f"RSS 解析{rss['parsed']}/发布{rss['published']}/队列{rss['queued']}/错{rss['errors']}；"
        f"岗位新增 {jobs_added}（本地共 {jobs_after}）；"
        f"待审异常 {review_n}，待验证源 {source_n}。"
    )
    from app.collector.promote_intern import promote_internship_from_review

    intern_promo = promote_internship_from_review(db)
    if intern_promo.get("promoted"):
        summary = summary + f" 另将异常中实习岗转正常 {intern_promo['promoted']} 条。"
        jobs_after = db.count_jobs()
        review_n = len(db.list_review_queue("pending", limit=5000))

    retention = cleanup_expired_jobs(db, retention_days=retention_days)
    if retention.get("deleted") or retention.get("review_ignored"):
        summary = summary + f" 保留清理软删 {retention['deleted']}，忽略异常 {retention['review_ignored']}。"
        jobs_after = db.count_jobs()
        review_n = len(db.list_review_queue("pending", limit=5000))

    db.add_digest(summary)
    finished = utc_now()
    db.set_meta("last_scan_finished_at", finished)
    db.set_meta("last_scan_mode", "quick")
    db.set_meta("last_scan_summary", summary)
    if progress:
        progress(summary)

    return {
        "text": summary,
        "mode": "quick",
        "finished_at": finished,
        "lookback_days": days,
        "promoted": promoted,
        "discovered": len(discovered),
        "new_official": official_n,
        "official_total": official_total,
        "web": web,
        "rss": rss,
        "retention": retention,
        "jobs_before": jobs_before,
        "jobs_after": jobs_after,
        "jobs_added": jobs_added,
        "review_pending": review_n,
        "source_pending": source_n,
        "intern_promoted": intern_promo.get("promoted", 0),
    }


def run_deep_collect_pipeline(
    db: LocalDB,
    *,
    lookback_days: int | None = None,
    collect_months: int | None = None,
    list_collect_months: int | None = None,
    retention_days: int | None = None,
    batch_size: int = 25,
    concurrency: int = 2,
    max_retries: int = 2,
    max_company_batches: int | None = None,
    max_companies: int | None = None,
    resume: bool = True,
    reset: bool = False,
    parse_supplement_limit: int | None = None,
    parse_supplement_timeout: float | None = None,
    progress: ProgressCb | None = None,
) -> dict[str, Any]:
    """深度采集近 3 个月：扫完有 URL 的公司，可中断续跑。"""
    return run_deep_collect(
        db,
        lookback_days=lookback_days,
        collect_months=collect_months,
        list_collect_months=list_collect_months,
        retention_days=retention_days,
        batch_size=batch_size,
        concurrency=concurrency,
        max_retries=max_retries,
        max_company_batches=max_company_batches,
        max_companies=max_companies,
        resume=resume,
        reset=reset,
        parse_supplement_limit=parse_supplement_limit,
        parse_supplement_timeout=parse_supplement_timeout,
        progress=progress,
    )


def publish_review_item(
    db: LocalDB,
    review_id: str,
    *,
    payload_override: dict | None = None,
) -> bool:
    """将异常队列条目确认发布为 job。可传入编辑后的 payload_override。

    成功时返回 True。若修正了原文/网申链接，会同步覆盖关联公司种子 URL。
    """
    item = db.get_review_item(review_id)
    if not item or item.get("status") != "pending":
        return False
    original = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    payload = payload_override if payload_override is not None else (item.get("payload") or {})
    if not isinstance(payload, dict):
        return False
    if not payload.get("title") or not payload.get("source_url"):
        return False
    if payload_override is not None:
        db.update_review_payload(review_id, payload)
    payload = dict(payload)
    payload["status"] = "active"
    jid = db.upsert_job(payload)
    db.remove_cloud_revoke_ids([jid])
    # 审核时修正的原文/网申链接覆盖公司种子，避免下次再采坏链
    db.sync_seed_urls_from_job_fields(original if isinstance(original, dict) else {}, payload)
    db.resolve_review(review_id, "published")
    return True
