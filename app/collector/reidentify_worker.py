"""Run local re-identification outside Tk and commit completed jobs incrementally."""

from __future__ import annotations

import multiprocessing as mp
import queue
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from app.collector.adapters.base import ParseResult
from app.collector.fill_from_url import FillCandidate, FillFromUrlResult

Message = tuple[str, Any]
EmitCb = Callable[[str, Any], None]


def serialize_candidate(candidate: FillCandidate) -> dict[str, Any]:
    hint = asdict(candidate.list_hint) if candidate.list_hint is not None else None
    return {
        "fields": dict(candidate.fields or {}),
        "label": candidate.label,
        "summary": candidate.summary,
        "needs_fetch": bool(candidate.needs_fetch),
        "detail_url": candidate.detail_url,
        "list_hint": hint,
        "outside_lookback": bool(candidate.outside_lookback),
    }


def deserialize_candidate(payload: dict[str, Any]) -> FillCandidate:
    raw_hint = payload.get("list_hint")
    hint = ParseResult(**raw_hint) if isinstance(raw_hint, dict) else None
    return FillCandidate(
        fields=dict(payload.get("fields") or {}),
        label=str(payload.get("label") or ""),
        summary=str(payload.get("summary") or ""),
        needs_fetch=bool(payload.get("needs_fetch")),
        detail_url=str(payload.get("detail_url") or "") or None,
        list_hint=hint,
        outside_lookback=bool(payload.get("outside_lookback")),
    )


def serialize_result(result: FillFromUrlResult) -> dict[str, Any]:
    return {
        "ok": bool(result.ok),
        "error": result.error,
        "candidates": [serialize_candidate(c) for c in result.candidates],
        "is_list": bool(result.is_list),
        "total": int(result.total or 0),
        "adapter": result.adapter,
        "page_url": result.page_url,
        "no_job_posting": bool(result.no_job_posting),
        "notice": result.notice,
        "outside_lookback_count": int(result.outside_lookback_count or 0),
    }


def deserialize_result(payload: dict[str, Any]) -> FillFromUrlResult:
    return FillFromUrlResult(
        ok=bool(payload.get("ok")),
        error=str(payload.get("error") or "") or None,
        candidates=[
            deserialize_candidate(c)
            for c in payload.get("candidates") or []
            if isinstance(c, dict)
        ],
        is_list=bool(payload.get("is_list")),
        total=int(payload.get("total") or 0),
        adapter=str(payload.get("adapter") or "") or None,
        page_url=str(payload.get("page_url") or ""),
        no_job_posting=bool(payload.get("no_job_posting")),
        notice=str(payload.get("notice") or "") or None,
        outside_lookback_count=int(payload.get("outside_lookback_count") or 0),
    )


def _discover(task: dict[str, Any], emit: EmitCb) -> dict[str, Any]:
    from app.collector.fill_from_url import discover_portal_jobs_from_url, pick_fill_url

    job = dict(task.get("job") or {})
    url = pick_fill_url(job.get("apply_url"), job.get("source_url"))
    if not job:
        raise ValueError("岗位不存在")
    if not url:
        raise ValueError("无可用网申/原文链接")
    result = discover_portal_jobs_from_url(
        url,
        fetch=True,
        keep_company=task.get("seed_company_name"),
        list_collect_months=task.get("list_collect_months"),
        list_limit=max(1, int(task.get("batch_size") or 15)),
        timeout=max(1.0, float(task.get("timeout") or 45.0)),
        progress=lambda msg: emit("progress", str(msg)),
    )
    return {
        "mode": "discovery",
        "item_id": str(task.get("item_id") or ""),
        "job": job,
        "seed_company_name": task.get("seed_company_name"),
        "batch_size": max(1, int(task.get("batch_size") or 15)),
        "result": serialize_result(result),
    }


def _selected(task: dict[str, Any], db_path: str | Path, emit: EmitCb) -> dict[str, Any]:
    from app.collector.fill_from_url import (
        apply_reidentify_fields,
        enrich_candidate_detail,
        fields_look_like_no_job_posting,
        find_matching_fill_candidate,
        summarize_reidentify_changes,
    )
    from app.db.local import LocalDB

    db = LocalDB(Path(db_path))
    item_id = str(task.get("item_id") or "")
    before = dict(task.get("before") or {})
    seed = str(task.get("seed_company_name") or "") or None
    candidates = [
        deserialize_candidate(c)
        for c in task.get("candidates") or []
        if isinstance(c, dict)
    ]
    page_url = str(task.get("page_url") or "")
    timeout = max(1.0, float(task.get("timeout") or 45.0))
    original_match = find_matching_fill_candidate(
        candidates,
        title=before.get("title"),
        source_url=before.get("source_url"),
        apply_url=before.get("apply_url"),
    )
    original_index = candidates.index(original_match) if original_match in candidates else -1

    ok_n = del_n = fail_n = inserted_n = updated_n = 0
    errors: list[str] = []
    notes: list[str] = []
    focus_id: str | None = item_id or None
    updated_original = False

    for idx, candidate in enumerate(candidates):
        try:
            enriched = enrich_candidate_detail(
                candidate,
                page_url=page_url,
                keep_company=seed,
                timeout=timeout,
            )
        except Exception:
            enriched = candidate
        fields = enriched.fields or {}
        title = str(fields.get("title") or "未命名")[:24]
        if fields_look_like_no_job_posting(fields):
            if len(candidates) == 1 and (original_index in (-1, idx)):
                deleted = db.soft_delete_jobs([item_id])
                if deleted:
                    del_n += 1
                    notes.append(f"{title}：页面无招聘信息，已删除")
                    emit(
                        "item",
                        {"action": "deleted", "job_id": item_id, "title": title},
                    )
                else:
                    fail_n += 1
                    errors.append(f"{title}：删除失败")
                break
            fail_n += 1
            errors.append(f"{title}：无有效岗位信息，已跳过")
            emit("item", {"action": "failed", "title": title})
            continue

        try:
            if idx == original_index and not updated_original:
                merged = apply_reidentify_fields(before, fields, seed_company_name=seed)
                merged["id"] = item_id
                merged["status"] = "pending_review"
                db.upsert_job_with_action(merged)
                db.sync_seed_urls_from_job_fields(before, merged)
                changes = summarize_reidentify_changes(before, merged)
                note = "原行已更新 " + "；".join(changes[:4]) if changes else "原行字段无实质变更"
                updated_original = True
                updated_n += 1
                job_id = item_id
                action = "updated"
            else:
                base: dict[str, Any] = {
                    "company": fields.get("company") or before.get("company") or "未知企业",
                    "group_name": before.get("group_name"),
                    "status": "pending_review",
                }
                merged = apply_reidentify_fields(base, fields, seed_company_name=seed)
                merged.pop("id", None)
                merged["status"] = "pending_review"
                job_id, db_action = db.upsert_job_with_action(merged)
                db.sync_seed_urls_from_job_fields(base, merged)
                if db_action == "inserted":
                    inserted_n += 1
                    action = "inserted"
                    note = f"新增待审：{title}"
                else:
                    updated_n += 1
                    action = "updated"
                    note = f"已有岗位已更新：{title}"
            ok_n += 1
            focus_id = job_id
            notes.append(note)
            emit(
                "item",
                {
                    "action": action,
                    "job_id": job_id,
                    "title": title,
                    "completed": idx + 1,
                    "total": len(candidates),
                },
            )
            emit("progress", f"局部重采：详情进度 {idx + 1}/{len(candidates)}，已写入 {ok_n}")
        except Exception as exc:  # noqa: BLE001
            fail_n += 1
            errors.append(f"{title}：保存失败：{exc}")
            emit("item", {"action": "failed", "title": title})

    found_n = int(task.get("found_n") or len(candidates))
    selected_n = int(task.get("selected_n") or len(candidates))
    notes.insert(
        0,
        f"识别到 {found_n} 个，已勾选 {selected_n} 个；"
        f"新增 {inserted_n} / 更新 {updated_n} / 失败 {fail_n}",
    )
    return {
        "mode": "completed",
        "ok_n": ok_n,
        "del_n": del_n,
        "fail_n": fail_n,
        "errors": errors,
        "notes": notes,
        "focus_id": focus_id,
    }


def _batch(task: dict[str, Any], db_path: str | Path, emit: EmitCb) -> dict[str, Any]:
    from app.collector.fill_from_url import reidentify_job_fields
    from app.db.local import LocalDB

    db = LocalDB(Path(db_path))
    snapshots = [s for s in task.get("snapshots") or [] if isinstance(s, dict)]
    batch_size = max(1, int(task.get("batch_size") or 15))
    timeout = max(1.0, float(task.get("timeout") or 45.0))
    months = task.get("list_collect_months")
    ok_n = del_n = fail_n = 0
    errors: list[str] = []
    notes: list[str] = []
    focus_id: str | None = None

    for idx, snapshot in enumerate(snapshots):
        item_id = str(snapshot.get("item_id") or "")
        job = dict(snapshot.get("job") or {})
        seed = str(snapshot.get("seed_company_name") or "") or None
        title = str(job.get("title") or item_id)[:24]
        short_id = item_id[:8] if item_id else "无ID"
        emit("progress", f"局部重采进度 {idx + 1}/{len(snapshots)}：{title} [{short_id}]")
        if not job:
            fail_n += 1
            errors.append(f"{item_id[:8]}…：岗位不存在")
            continue
        before = dict(job)
        try:
            action, merged, msg, _raw = reidentify_job_fields(
                job,
                fetch=True,
                seed_company_name=seed,
                discover_portal=True,
                list_collect_months=int(months) if months else None,
                list_limit=batch_size,
                timeout=timeout,
            )
            if action == "delete":
                deleted = db.soft_delete_jobs([item_id])
                if not deleted:
                    raise RuntimeError("删除失败")
                del_n += 1
                notes.append(f"{title}：页面无招聘信息，已删除")
                emit("item", {"action": "deleted", "job_id": item_id, "title": title})
                continue
            if action != "update" or not merged:
                fail_n += 1
                errors.append(f"{title} [{short_id}]：{msg or '未能识别'}")
                emit("item", {"action": "failed", "title": title})
                continue
            merged = dict(merged)
            merged["id"] = item_id
            merged["status"] = "pending_review"
            db.upsert_job_with_action(merged)
            db.sync_seed_urls_from_job_fields(before, merged)
            ok_n += 1
            focus_id = item_id
            notes.append(str(msg or "已更新"))
            emit(
                "item",
                {
                    "action": "updated",
                    "job_id": item_id,
                    "title": title,
                    "completed": idx + 1,
                    "total": len(snapshots),
                },
            )
        except Exception as exc:  # noqa: BLE001
            fail_n += 1
            errors.append(f"{title} [{short_id}]：识别异常：{exc}")
            emit("item", {"action": "failed", "title": title})

    return {
        "mode": "completed",
        "ok_n": ok_n,
        "del_n": del_n,
        "fail_n": fail_n,
        "errors": errors,
        "notes": notes,
        "focus_id": focus_id,
    }


def run_reidentify_task(
    db_path: str | Path,
    task: dict[str, Any],
    emit: EmitCb,
) -> dict[str, Any]:
    mode = str(task.get("mode") or "")
    if mode == "discover":
        return _discover(task, emit)
    if mode == "selected":
        return _selected(task, db_path, emit)
    if mode == "batch":
        return _batch(task, db_path, emit)
    raise ValueError(f"未知局部重采任务：{mode}")


def _queue_emit(message_queue: Any, kind: str, payload: Any, *, critical: bool = False) -> None:
    message: Message = (kind, payload)
    try:
        if critical:
            message_queue.put(message, timeout=5.0)
        else:
            message_queue.put_nowait(message)
    except (queue.Full, ValueError, OSError):
        if critical:
            try:
                message_queue.put(message, timeout=1.0)
            except Exception:
                pass


def reidentify_process_main(db_path: str, task: dict[str, Any], message_queue: Any) -> None:
    try:
        result = run_reidentify_task(
            db_path,
            task,
            lambda kind, payload: _queue_emit(message_queue, kind, payload),
        )
        kind = "discovery" if result.get("mode") == "discovery" else "result"
        _queue_emit(message_queue, kind, result, critical=True)
    except BaseException as exc:  # noqa: BLE001
        _queue_emit(
            message_queue,
            "error",
            {
                "message": str(exc) or exc.__class__.__name__,
                "traceback": traceback.format_exc(limit=20),
            },
            critical=True,
        )
        raise
    finally:
        _queue_emit(message_queue, "finished", None, critical=True)


def start_reidentify_process(
    db_path: str | Path,
    task: dict[str, Any],
) -> tuple[mp.Process, Any]:
    ctx = mp.get_context("spawn")
    message_queue = ctx.Queue(maxsize=256)
    process = ctx.Process(
        target=reidentify_process_main,
        args=(str(Path(db_path)), dict(task), message_queue),
        name="campus-jobs-reidentify",
        daemon=True,
    )
    process.start()
    return process, message_queue


def drain_reidentify_messages(message_queue: Any, *, limit: int = 100) -> list[Message]:
    messages: list[Message] = []
    for _ in range(max(1, int(limit))):
        try:
            item = message_queue.get_nowait()
        except queue.Empty:
            break
        except (EOFError, OSError, ValueError):
            break
        if isinstance(item, tuple) and len(item) == 2:
            messages.append((str(item[0]), item[1]))
    return messages
