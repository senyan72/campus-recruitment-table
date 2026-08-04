"""深度采集：近 N 天校招，分片扫完有 URL 的 official 公司，可中断续跑。"""

from __future__ import annotations

import time
from typing import Any, Callable

from app.collector.cleanup import cleanup_expired_jobs
from app.collector.discover import promote_trusted_seeds
from app.collector.filters import resolve_list_lookback_months, resolve_lookback_days
from app.collector.website import collect_company_careers
from app.db.local import LocalDB, utc_now

ProgressCb = Callable[[str], None]


def needs_deep_parse_supplement(job: dict[str, Any]) -> bool:
    """Retry only records whose first-pass detail parsing is materially incomplete."""
    url = str(job.get("apply_url") or job.get("source_url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return False
    parse_status = str(job.get("parse_status") or "ok").strip().lower()
    jd_len = len(str(job.get("jd_text") or "").strip())
    return parse_status not in ("", "ok") or jd_len < 80


def supplement_company_parsing(
    db: LocalDB,
    company: dict[str, Any],
    *,
    since: str,
    limit: int = 5,
    timeout: float = 20.0,
    list_collect_months: int | None = None,
    progress: ProgressCb | None = None,
) -> dict[str, int]:
    """Reuse existing re-identification for a bounded set of partial/failed rows."""
    cap = max(0, int(limit))
    counts = {"checked": 0, "updated": 0, "recovered": 0, "failed": 0}
    if cap <= 0:
        return counts

    from app.collector.fill_from_url import reidentify_job_fields
    from app.config import load_config

    company_id = str(company.get("id") or "")
    company_name = str(company.get("name") or "").strip() or None
    ocr_enabled = bool(load_config().get("ocr_enabled"))
    candidates: list[tuple[str, str | None, dict[str, Any]]] = []
    review_kinds = {"missing_fields", "low_confidence", "chrome_title", "parse_error"}

    for review in db.list_company_reviews_since(
        company_id,
        since,
        kinds=review_kinds,
        limit=max(cap * 2, 10),
    ):
        payload = review.get("payload") if isinstance(review.get("payload"), dict) else {}
        item = dict(payload)
        item["source_url"] = item.get("source_url") or item.get("url")
        item["apply_url"] = item.get("apply_url") or item.get("source_url")
        item["company"] = item.get("company") or company_name or "未知企业"
        item["company_id"] = item.get("company_id") or company_id
        # Empty-title chrome/parse errors cannot be matched safely without user choice.
        if item.get("title") and needs_deep_parse_supplement(item):
            candidates.append(("review", str(review.get("id") or "") or None, item))

    for job in db.list_company_jobs_updated_since(
        company_id,
        since,
        limit=max(cap * 4, 20),
    ):
        if needs_deep_parse_supplement(job):
            candidates.append(("job", None, job))

    seen: set[str] = set()
    for source_kind, review_id, job in candidates:
        if counts["checked"] >= cap or db.deep_collect_cancel_requested():
            break
        identity = str(job.get("id") or "") or (
            f"{job.get('title') or ''}|{job.get('apply_url') or job.get('source_url') or ''}"
        )
        if identity in seen:
            continue
        seen.add(identity)
        counts["checked"] += 1
        if progress:
            progress(f"解析补全：{company_name or '未知企业'} · {job.get('title') or '待识别岗位'}")
        action, merged, _message, _raw = reidentify_job_fields(
            job,
            fetch=True,
            ocr_enabled=ocr_enabled,
            seed_company_name=company_name,
            discover_portal=False,
            list_collect_months=list_collect_months,
            list_limit=20,
            timeout=timeout,
        )
        if action != "update" or not merged:
            counts["failed"] += 1
            continue
        merged["company_id"] = merged.get("company_id") or company_id
        merged["status"] = "pending_review"
        _jid, db_action = db.upsert_job_with_action(merged)
        if db_action == "updated":
            counts["updated"] += 1
        if source_kind == "review" and review_id:
            db.resolve_review(review_id, "reidentified")
            counts["recovered"] += 1
    return counts


def format_deep_progress(
    company_idx: int,
    company_total: int,
    job_pending: int | None = None,
    job_total: int | None = None,
) -> str:
    """
    深度采集进度文案。
    公司序号 = 已处理（done/error/skipped）+ 当前 running 的一家（含当前）。
    括号内 = 当前企业本轮待采集岗数 / 列表枚举或预算总岗数。
    """
    base = f"深度采集进度：{company_idx}/{company_total}"
    if job_pending is not None and job_total is not None:
        return f"{base}（{job_pending}/{job_total}）"
    return base


def company_index_from_progress(prog: dict[str, Any]) -> int:
    """已处理含当前序号：processed + (1 if running else 0)。"""
    processed = int(prog.get("processed", 0) or 0)
    running = int(prog.get("running", 0) or 0)
    return processed + (1 if running > 0 else 0)


def job_counts_from_stats(stats: dict[str, int], budget: int) -> tuple[int, int]:
    """
    当前企业岗位进度 (待采集, 总数)。
    待采集 = 枚举总数 - 已处理（新采 + 已入库跳过）；总数优先取 enum_total，否则用预算。
    """
    batch_new = int(stats.get("batch_new", 0) or 0)
    skipped = int(stats.get("skipped_known", 0) or 0)
    enum_total = int(stats.get("enum_total", 0) or 0)
    done = batch_new + skipped
    if enum_total > 0:
        total = enum_total
    elif done > 0:
        total = done
    else:
        total = max(1, budget)
    pending = max(0, total - done)
    return pending, total


def _process_one(
    db: LocalDB,
    company: dict[str, Any],
    *,
    lookback_days: int,
    list_collect_months: int,
    block_patterns: list[str],
    max_retries: int,
    per_company_batch: int,
    max_company_batches: int,
    on_stats: Callable[[dict[str, int]], None] | None = None,
) -> tuple[dict[str, int], str]:
    total: dict[str, int] = {}
    batch_cap = max(1, int(per_company_batch))
    batch_rounds = max(1, int(max_company_batches))

    def merge_stats(target: dict[str, int], current: dict[str, int]) -> None:
        for key, value in current.items():
            if not isinstance(value, (int, float)):
                continue
            n = int(value)
            if key == "enum_total":
                target[key] = max(int(target.get(key, 0) or 0), n)
            else:
                target[key] = int(target.get(key, 0) or 0) + n

    for batch_no in range(1, batch_rounds + 1):
        last_err = ""
        stats: dict[str, int] | None = None
        for attempt in range(max_retries + 1):
            try:
                def report_current(current: dict[str, int]) -> None:
                    if not on_stats:
                        return
                    preview = dict(total)
                    merge_stats(preview, current)
                    preview["company_batch"] = batch_no
                    preview["company_batch_limit"] = batch_rounds
                    on_stats(preview)

                stats = collect_company_careers(
                    db,
                    company,
                    block_patterns=block_patterns,
                    lookback_days=lookback_days,
                    list_collect_months=list_collect_months,
                    max_career_urls=3,
                    require_real_jobs=False,
                    max_new_jobs=batch_cap,
                    on_stats=report_current,
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                if attempt < max_retries:
                    time.sleep(1.5 * (attempt + 1))

        if stats is None:
            total["errors"] = int(total.get("errors", 0) or 0) + 1
            return total, last_err

        merge_stats(total, stats)
        total["company_batches"] = batch_no
        total["company_batch"] = batch_no
        total["company_batch_limit"] = batch_rounds
        if on_stats:
            on_stats(dict(total))

        batch_new = int(stats.get("batch_new", 0) or 0)
        has_more = batch_new >= batch_cap
        if not has_more:
            return total, ""
        if db.deep_collect_cancel_requested():
            total["continuation_pending"] = 1
            return total, ""
        if batch_no >= batch_rounds:
            total["continuation_limited"] = 1
            return total, ""

    return total, ""


def run_deep_collect(
    db: LocalDB,
    *,
    lookback_days: int | None = None,
    collect_months: int | None = None,
    list_collect_months: int | None = None,
    retention_days: int | None = None,
    batch_size: int = 25,
    concurrency: int = 1,
    max_retries: int = 2,
    max_company_batches: int | None = None,
    resume: bool = True,
    reset: bool = False,
    max_companies: int | None = None,
    parse_supplement_limit: int | None = None,
    parse_supplement_timeout: float | None = None,
    progress: ProgressCb | None = None,
) -> dict[str, Any]:
    """
    深度采集近 lookback_days（默认 90）校招/实习。
    - 对有 career/hint 的公司建 SQLite 队列，逐家串行扫完（不卡死在 40 家）
    - 校园/实习列表按 list_collect_months（默认 6）翻页
    - 结束后按 retention_days 软删超期岗位
    - 可中断（request_deep_collect_cancel）后续跑
    """
    days = resolve_lookback_days(lookback_days=lookback_days, collect_months=collect_months)
    list_months = resolve_list_lookback_months(list_collect_months)
    jobs_before = db.count_jobs()

    if progress:
        progress("深度采集：提升可信 ATS 种子…")
    promoted = promote_trusted_seeds(db, limit=5000)

    # 尽可能拉全量带 URL 公司（official 优先已在 list 排序里）
    fetch_n = max_companies or 50000
    companies = db.list_companies_with_urls(
        limit=fetch_n, prefer_ats=True, verify_status=None
    )
    if max_companies:
        companies = companies[:max_companies]

    run_id = db.create_collect_run(companies, reset=reset, resume=resume)
    reclaimed = db.reclaim_stuck_collect_queue(run_id)
    prog = db.collect_queue_progress(run_id)
    from app.collector.fill_from_url import per_company_batch_size
    from app.config import load_config

    cfg = load_config()
    per_co = per_company_batch_size(cfg)
    company_batch_limit = max(
        1,
        int(
            max_company_batches
            if max_company_batches is not None
            else cfg.get("deep_collect_max_company_batches", 10)
        ),
    )
    enrich_limit = int(
        parse_supplement_limit
        if parse_supplement_limit is not None
        else cfg.get("deep_collect_parse_supplement_limit", 5)
    )
    enrich_timeout = float(
        parse_supplement_timeout
        if parse_supplement_timeout is not None
        else cfg.get("deep_collect_parse_supplement_timeout", 20)
    )
    nature_lookup_enabled = bool(cfg.get("company_nature_lookup_enabled", True))
    nature_lookup_timeout = float(cfg.get("company_nature_lookup_timeout", 5) or 5)
    nature_lookup_max_calls = max(0, int(cfg.get("company_nature_lookup_max_calls", 2) or 0))
    if progress:
        extra = f"（恢复 {reclaimed} 家中断）" if reclaimed else ""
        progress(
            format_deep_progress(
                company_index_from_progress(prog),
                prog["total"],
                per_co,
                per_co,
            )
            + extra
        )

    block = db.list_blocklist()
    # 逐家采集：并发固定 1，每轮只 dequeue 一家公司，当前企岗位采完才换下一家
    concurrency = 1
    _ = concurrency  # 保留参数签名，调用方传入的值一律忽略
    batch_size = 1
    _ = batch_size
    totals = {
        "parsed": 0,
        "published": 0,
        "queued": 0,
        "errors": 0,
        "skipped": 0,
        "stale": 0,
        "skipped_known": 0,
        "parse_supplement_checked": 0,
        "parse_supplement_updated": 0,
        "parse_supplement_recovered": 0,
        "parse_supplement_failed": 0,
        "company_batches": 0,
        "continuation_limited": 0,
        "company_nature_checked": 0,
        "company_nature_filled": 0,
        "company_nature_jobs_updated": 0,
    }
    cancelled = False
    nature_checked_company_ids: set[str] = set()
    nature_lookup_calls = 0

    while True:
        if db.deep_collect_cancel_requested():
            cancelled = True
            if progress:
                progress("已收到中断请求，停止后续分片…")
            break

        batch = db.list_collect_queue(run_id, status="pending", limit=1)
        if not batch:
            break

        item = batch[0]
        co = db.get_company(item["company_id"])
        if not co:
            db.update_collect_queue_item(item["id"], status="skipped", last_error="公司不存在")
            continue

        company_id = str(co.get("id") or "")
        if (
            nature_lookup_enabled
            and company_id
            and company_id not in nature_checked_company_ids
            and not str(co.get("company_nature") or "").strip()
            and nature_lookup_calls < nature_lookup_max_calls
        ):
            from app.collector.company_nature import search_company_nature

            nature_checked_company_ids.add(company_id)
            nature_lookup_calls += 1
            totals["company_nature_checked"] += 1
            if progress:
                progress(f"企业性质检索：{co.get('name') or '未知企业'}")
            nature_result = search_company_nature(
                str(co.get("name") or ""),
                timeout=nature_lookup_timeout,
            )
            if nature_result.nature:
                updated_jobs = db.set_company_nature(company_id, nature_result.nature)
                totals["company_nature_filled"] += 1
                totals["company_nature_jobs_updated"] += updated_jobs
                co["company_nature"] = nature_result.nature

        db.update_collect_queue_item(item["id"], status="running", bump_attempts=True)
        company_started_at = utc_now()
        prog = db.collect_queue_progress(run_id)
        company_idx = company_index_from_progress(prog)
        company_total = int(prog["total"])

        if progress:
            progress(format_deep_progress(company_idx, company_total, per_co, per_co))

        def _on_stats(st: dict[str, int], *, _idx=company_idx, _total=company_total) -> None:
            if not progress:
                return
            jp, jt = job_counts_from_stats(st, per_co)
            message = format_deep_progress(_idx, _total, jp, jt)
            batch_no = int(st.get("company_batch", 0) or 0)
            if batch_no:
                message += f"｜{co.get('name') or '当前企业'}第 {batch_no} 批"
            progress(message)

        try:
            stats, err = _process_one(
                db,
                co,
                lookback_days=days,
                list_collect_months=list_months,
                block_patterns=block,
                max_retries=max_retries,
                per_company_batch=per_co,
                max_company_batches=company_batch_limit,
                on_stats=_on_stats,
            )
        except Exception as exc:  # noqa: BLE001
            db.update_collect_queue_item(
                item["id"], status="error", last_error=str(exc)
            )
            totals["errors"] += 1
            if progress:
                p = db.collect_queue_progress(run_id)
                progress(
                    format_deep_progress(
                        company_index_from_progress(p),
                        p["total"],
                    )
                )
            continue

        for k in (
            "parsed",
            "published",
            "queued",
            "errors",
            "skipped",
            "stale",
            "skipped_known",
            "company_batches",
            "continuation_limited",
        ):
            totals[k] += int(stats.get(k, 0))
        continuation_pending = bool(stats.get("continuation_pending"))
        supplement = (
            {"checked": 0, "updated": 0, "recovered": 0, "failed": 0}
            if continuation_pending
            else supplement_company_parsing(
                db,
                co,
                since=company_started_at,
                limit=enrich_limit,
                timeout=enrich_timeout,
                list_collect_months=list_months,
                progress=progress,
            )
        )
        totals["parse_supplement_checked"] += supplement["checked"]
        totals["parse_supplement_updated"] += supplement["updated"]
        totals["parse_supplement_recovered"] += supplement["recovered"]
        totals["parse_supplement_failed"] += supplement["failed"]
        status = "pending" if continuation_pending else "done"
        if err and stats.get("parsed", 0) == 0 and stats.get("published", 0) == 0:
            status = "error"
        if stats.get("errors", 0) and not stats.get("published") and not stats.get("parsed"):
            status = "error"
        db.update_collect_queue_item(
            item["id"],
            status=status,
            last_error=err or None,
            jobs_published=int(stats.get("published", 0)),
            jobs_queued=int(stats.get("queued", 0)),
        )
        if progress:
            p = db.collect_queue_progress(run_id)
            jp, jt = job_counts_from_stats(stats, per_co)
            progress(format_deep_progress(company_index_from_progress(p), p["total"], jp, jt))

        if continuation_pending or db.deep_collect_cancel_requested():
            cancelled = True
            break

    jobs_after = db.count_jobs()
    jobs_added = max(0, jobs_after - jobs_before)
    final = db.collect_queue_progress(run_id)
    summary = (
        f"[{utc_now()}] 深度采集（近{days}天）"
        f"{'已中断' if cancelled else '完成'}："
        f"公司 {final['processed']}/{final['total']} "
        f"（done {final['done']} / err {final['error']} / skip {final['skipped']}）；"
        f"官网 解析{totals['parsed']}/发布{totals['published']}/队列{totals['queued']}/"
        f"跳过已入库{totals.get('skipped_known', 0)}/"
        f"过期跳过{totals['stale']}/错{totals['errors']}；"
        f"企业续批 {totals['company_batches']} 批"
        f"（触及安全上限 {totals['continuation_limited']} 家）；"
        f"解析补全 检查{totals['parse_supplement_checked']}/"
        f"更新{totals['parse_supplement_updated']}/"
        f"异常恢复{totals['parse_supplement_recovered']}/"
        f"失败{totals['parse_supplement_failed']}；"
        f"企业性质 查询{totals['company_nature_checked']}/"
        f"补全{totals['company_nature_filled']}/"
        f"同步岗位{totals['company_nature_jobs_updated']}；"
        f"岗位新增 {jobs_added}（本地共 {jobs_after}）；"
        f"自动标记 official {promoted}。"
    )
    from app.collector.promote_intern import promote_internship_from_review

    intern_promo = promote_internship_from_review(db)
    if intern_promo.get("promoted"):
        summary = summary + f" 另将异常中实习岗转正常 {intern_promo['promoted']} 条。"
        jobs_after = db.count_jobs()

    retention = cleanup_expired_jobs(db, retention_days=retention_days)
    if retention.get("deleted") or retention.get("review_ignored"):
        summary = summary + f" 保留清理软删 {retention['deleted']}，忽略异常 {retention['review_ignored']}。"
        jobs_after = db.count_jobs()

    db.add_digest(summary)
    finished = utc_now()
    db.set_meta(
        "deep_collect_last_progress",
        (
            f"{final['processed']}/{final['total']}|pub={totals['published']}|"
            f"added={jobs_added}|days={days}|cancel={int(cancelled)}"
        ),
    )
    db.set_meta("last_scan_finished_at", finished)
    db.set_meta("last_scan_mode", "deep" + ("_cancelled" if cancelled else ""))
    db.set_meta("last_scan_summary", summary)
    if progress:
        progress(
            format_deep_progress(
                company_index_from_progress(final),
                final["total"],
            )
            + f"｜新增 {jobs_added} 岗"
            + ("｜已中断" if cancelled else "｜完成")
        )

    return {
        "text": summary,
        "finished_at": finished,
        "mode": "deep",
        "run_id": run_id,
        "lookback_days": days,
        "promoted": promoted,
        "cancelled": cancelled,
        "progress": final,
        "web": totals,
        "retention": retention,
        "jobs_before": jobs_before,
        "jobs_after": jobs_after,
        "jobs_added": jobs_added,
        "companies_total": final["total"],
        "companies_processed": final["processed"],
        "intern_promoted": intern_promo.get("promoted", 0),
    }
