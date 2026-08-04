"""清理本地噪声标题与重复岗位、超期岗位（软删 status=deleted）。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.collector.filters import (
    is_closed_or_referral_jd,
    is_closed_or_referral_title,
    is_company_plus_recruit_title,
    is_noise_title,
    is_past_retention,
    is_portal_shell_record,
    normalize_job_title,
    normalize_url_for_dedupe,
    portal_entry_title,
    resolve_retention_days,
    retention_cutoff,
    title_company_mismatch,
)
from app.db.local import LocalDB


def _quality_score(job: dict[str, Any]) -> tuple[int, float, int]:
    jd_len = len((job.get("jd_text") or "").strip())
    conf = float(job.get("confidence") or 0.0)
    has_detail = 0 if is_portal_shell_record(
        title=job.get("title"), jd_text=job.get("jd_text"), source_url=job.get("source_url")
    ) else 1
    return (has_detail, conf, jd_len)


def deduplicate_jobs(
    db: LocalDB, *, company_keys: set[str] | None = None
) -> dict[str, Any]:
    """软删重复岗位；指定公司时只处理这些公司的待审和显示中岗位。"""
    jobs = db.list_jobs(status=["pending_review", "active"], limit=50000)
    if company_keys is not None:
        keys = {str(key).strip() for key in company_keys if str(key).strip()}
        jobs = [
            job
            for job in jobs
            if str(job.get("company_id") or job.get("company") or "").strip() in keys
        ]

    by_company: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        key = str(job.get("company_id") or job.get("company") or "").strip()
        if key:
            by_company.setdefault(key, []).append(job)

    to_delete: list[str] = []
    kept_ids: list[str] = []
    for group in by_company.values():
        by_title: dict[str, list[dict[str, Any]]] = {}
        for job in group:
            title = normalize_job_title(job.get("title"))
            if title:
                by_title.setdefault(title, []).append(job)
        for cluster in by_title.values():
            if len(cluster) <= 1:
                continue
            best = max(cluster, key=_quality_score)
            kept_ids.append(best["id"])
            to_delete.extend(job["id"] for job in cluster if job["id"] != best["id"])

    deleted_ids = list(dict.fromkeys(to_delete))
    deleted = db.soft_delete_jobs(deleted_ids)
    return {
        "deleted": deleted,
        "deleted_ids": deleted_ids,
        "kept_ids": list(dict.fromkeys(kept_ids)),
        "companies": len(by_company),
    }


def cleanup_noise_and_duplicate_jobs(db: LocalDB) -> dict[str, Any]:
    """
    软删噪声标题（含关停/内推）、详情关停/仅内推、标题公司不符、
    同公司重复（同 URL / 同归一化标题），以及对仅门户空壳的公司最多保留 1 条。
    """
    # 待审核 + 已显示中的正常岗一并清理噪声/重复
    jobs = db.list_jobs(status=["pending_review", "active"], limit=50000)
    to_delete: list[str] = []
    reasons: dict[str, str] = {}

    # 1) 噪声标题 / 关停·内推 / 「公司名+校园招聘」壳标题 / 详情正文关停·仅内推
    for j in jobs:
        if is_closed_or_referral_title(j.get("title")) or is_noise_title(j.get("title")):
            to_delete.append(j["id"])
            reasons[j["id"]] = (
                "closed_or_referral_title"
                if is_closed_or_referral_title(j.get("title"))
                else "noise_title"
            )
        elif is_closed_or_referral_jd(j.get("jd_text")):
            to_delete.append(j["id"])
            reasons[j["id"]] = "closed_or_referral_jd"
        # 不再因「公司名+校园招聘 / 未列具体岗位」删除；异常口径改为超3个月未更新

    deleted_noise = set(to_delete)

    # 2) 标题公司名严重不符
    for j in jobs:
        if j["id"] in deleted_noise:
            continue
        if title_company_mismatch(j.get("company"), j.get("title")):
            to_delete.append(j["id"])
            reasons[j["id"]] = "company_mismatch"

    deleted_set = set(to_delete)
    remain = [j for j in jobs if j["id"] not in deleted_set]

    # 3) 同公司 + URL / 归一化标题去重，保留质量最高的一条
    by_company: dict[str, list[dict[str, Any]]] = {}
    for j in remain:
        key = j.get("company_id") or j.get("company") or ""
        by_company.setdefault(key, []).append(j)

    for _ck, group in by_company.items():
        # 仅同归一化标题去重；同 URL 但不同真实岗位名保留多行
        by_title: dict[str, list[dict[str, Any]]] = {}
        by_url_same_title: dict[str, list[dict[str, Any]]] = {}
        for j in group:
            nt = normalize_job_title(j.get("title"))
            if nt:
                by_title.setdefault(nt, []).append(j)
            u = normalize_url_for_dedupe(j.get("apply_url") or "") or normalize_url_for_dedupe(
                j.get("source_url") or ""
            )
            if u and nt:
                by_url_same_title.setdefault(f"{u}||{nt}", []).append(j)

        for cluster in list(by_title.values()) + list(by_url_same_title.values()):
            if len(cluster) <= 1:
                continue
            best = max(cluster, key=_quality_score)
            for j in cluster:
                if j["id"] != best["id"] and j["id"] not in deleted_set:
                    to_delete.append(j["id"])
                    reasons[j["id"]] = "duplicate"
                    deleted_set.add(j["id"])

        # 4) 该公司若只剩门户空壳，最多留 1 条并规范化标题
        survivors = [j for j in group if j["id"] not in deleted_set]
        shells = [
            j
            for j in survivors
            if is_portal_shell_record(
                title=j.get("title"), jd_text=j.get("jd_text"), source_url=j.get("source_url")
            )
        ]
        details = [j for j in survivors if j not in shells]
        if details:
            for j in shells:
                if j["id"] not in deleted_set:
                    to_delete.append(j["id"])
                    reasons[j["id"]] = "portal_shell_extra"
                    deleted_set.add(j["id"])
        elif len(shells) > 1:
            best = max(shells, key=_quality_score)
            for j in shells:
                if j["id"] != best["id"] and j["id"] not in deleted_set:
                    to_delete.append(j["id"])
                    reasons[j["id"]] = "portal_shell_extra"
                    deleted_set.add(j["id"])
            # 规范化保留条标题
            nice = portal_entry_title(best.get("company"), best.get("recruit_project"))
            if (best.get("title") or "").strip() != nice:
                db.upsert_job(
                    {
                        **best,
                        "title": nice,
                        "status": best.get("status") or "pending_review",
                    }
                )

    # 去重 id 保序
    seen_ids: set[str] = set()
    uniq_ids: list[str] = []
    for jid in to_delete:
        if jid not in seen_ids:
            seen_ids.add(jid)
            uniq_ids.append(jid)

    n = db.soft_delete_jobs(uniq_ids)
    noise_n = sum(1 for i in uniq_ids if reasons.get(i) == "noise_title")
    closed_title_n = sum(1 for i in uniq_ids if reasons.get(i) == "closed_or_referral_title")
    closed_jd_n = sum(1 for i in uniq_ids if reasons.get(i) == "closed_or_referral_jd")
    shell_title_n = sum(1 for i in uniq_ids if reasons.get(i) == "company_recruit_shell")
    mismatch_n = sum(1 for i in uniq_ids if reasons.get(i) == "company_mismatch")
    dup_n = sum(1 for i in uniq_ids if reasons.get(i) == "duplicate")
    shell_n = sum(1 for i in uniq_ids if reasons.get(i) == "portal_shell_extra")

    summary = (
        f"清理完成：软删 {n} 条（噪声标题 {noise_n}，关停/内推标题 {closed_title_n}，"
        f"详情关停/仅内推 {closed_jd_n}，公司名+校园招聘 {shell_title_n}，"
        f"公司不符 {mismatch_n}，重复 {dup_n}，多余门户空壳 {shell_n}）。"
        f"本地仍 active {db.count_jobs('active')}。"
        f"请到「岗位显示」再点「推送云端」同步 deleted。"
    )
    db.add_digest(summary)
    return {
        "deleted": n,
        "noise_title": noise_n,
        "closed_or_referral_title": closed_title_n,
        "closed_or_referral_jd": closed_jd_n,
        "company_recruit_shell": shell_title_n,
        "company_mismatch": mismatch_n,
        "duplicate": dup_n,
        "portal_shell_extra": shell_n,
        "deleted_ids": uniq_ids,
        "active_remaining": db.count_jobs("active"),
        "text": summary,
    }


def cleanup_expired_jobs(
    db: LocalDB,
    *,
    retention_days: int | None = None,
    today: date | datetime | None = None,
) -> dict[str, Any]:
    """
    软删发布/更新参照日早于保留期的 active 岗位，并忽略关联的待审异常条目。

    参照日 = open_at / updated_at / created_at 中可解析的最晚日期（见 is_past_retention）。
    不删除 companies 种子。默认保留 365 天。
    """
    days = resolve_retention_days(retention_days)
    cutoff = retention_cutoff(retention_days=days, today=today)
    jobs = db.list_jobs(status=["pending_review", "active"], limit=50000)
    to_delete = [j["id"] for j in jobs if is_past_retention(j, retention_days=days, today=today)]
    n = db.soft_delete_jobs(to_delete)

    review_n = 0
    for item in db.list_review_queue("pending", limit=50000):
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if is_past_retention(payload, retention_days=days, today=today):
            db.resolve_review(item["id"], "ignored")
            review_n += 1

    summary = (
        f"保留清理：软删超 {days} 天岗位 {n} 条"
        f"（参照日 < {cutoff.isoformat()}），"
        f"忽略待审异常 {review_n} 条；本地仍 active {db.count_jobs('active')}。"
    )
    if n or review_n:
        db.add_digest(summary)
    return {
        "deleted": n,
        "review_ignored": review_n,
        "retention_days": days,
        "cutoff": cutoff.isoformat(),
        "deleted_ids": to_delete,
        "active_remaining": db.count_jobs("active"),
        "text": summary,
    }
