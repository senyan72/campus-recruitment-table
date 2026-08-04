"""将异常队列中的实习相关条目自动发布为正常岗位。"""

from __future__ import annotations

from typing import Any

from app.collector.filters import has_internship_signal, map_recruit_bucket
from app.collector.pipeline import publish_review_item
from app.db.local import LocalDB


def _payload_is_internship(payload: dict[str, Any]) -> bool:
    if (payload.get("recruit_bucket") or "").strip() == "日常实习":
        return True
    return has_internship_signal(
        payload.get("title"),
        payload.get("jd_text"),
        payload.get("recruit_project"),
        payload.get("recruit_bucket"),
    )


def promote_internship_from_review(db: LocalDB, *, limit: int = 5000) -> dict[str, Any]:
    """异常队列里带实习信号的 pending 项 → 发布为 active。"""
    items = db.list_review_queue("pending", limit=limit)
    promoted = 0
    skipped = 0
    for item in items:
        payload = item.get("payload") or {}
        if not isinstance(payload, dict):
            skipped += 1
            continue
        if not _payload_is_internship(payload):
            skipped += 1
            continue
        payload = dict(payload)
        if not payload.get("recruit_bucket"):
            payload["recruit_bucket"] = (
                map_recruit_bucket(payload.get("recruit_project"), payload.get("title"))
                or "日常实习"
            )
        if not payload.get("recruit_project"):
            payload["recruit_project"] = "日常实习"
        if publish_review_item(db, item["id"], payload_override=payload):
            promoted += 1
        else:
            skipped += 1
    summary = f"已将异常队列中 {promoted} 条实习岗发布为正常岗位（跳过 {skipped}）。"
    if promoted:
        db.add_digest(summary)
    return {"promoted": promoted, "skipped": skipped, "text": summary}
