"""带日志的 LLM 调用封装。"""

from __future__ import annotations

import time
from typing import Any

from app.coach.ai import get_model_adapter, try_complete_json_or_none
from app.coach.ai.context import build_companion_context, context_as_prompt_block
from app.coach.database import CoachDB
from app.coach.guardrails import validate_llm_payload
from app.timeutil import utc_now_iso


def log_llm_call(
    db: CoachDB | None,
    *,
    task: str,
    schema_name: str,
    ok: bool,
    fallback: bool,
    latency_ms: int,
    provider: str = "",
    model: str = "",
    error: str = "",
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
) -> None:
    if not db:
        return
    try:
        db.execute(
            "INSERT INTO llm_call_logs(id, task, schema_name, provider, model, ok, fallback, latency_ms, error, prompt_tokens, completion_tokens, total_tokens, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                db.new_id("llm_"),
                task,
                schema_name,
                provider,
                model,
                1 if ok else 0,
                1 if fallback else 0,
                latency_ms,
                (error or "")[:500],
                prompt_tokens,
                completion_tokens,
                total_tokens,
                utc_now_iso(),
            ),
        )
    except Exception:
        pass


def invoke_companion_llm(
    db: CoachDB,
    *,
    user_id: str,
    task: str,
    schema_name: str,
    system: str,
    user_extra: str,
    query: str | None = None,
    track: str = "campus_general",
    job: dict[str, Any] | None = None,
    validator: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """返回 (payload, engine) engine=llm|rules|none"""
    ctx = build_companion_context(db, user_id, query=query, track=track, job=job)
    user = f"{user_extra}\n\n--- companion_context ---\n{context_as_prompt_block(ctx)}"
    adapter = get_model_adapter()
    provider = getattr(adapter, "provider", "") or getattr(adapter, "model_version", "")
    model = getattr(adapter, "model", "") or getattr(adapter, "model_version", "")
    t0 = time.perf_counter()
    raw = try_complete_json_or_none(task=task, system=system, user=user, schema_name=schema_name)
    latency = int((time.perf_counter() - t0) * 1000)
    if raw is None:
        log_llm_call(
            db,
            task=task,
            schema_name=schema_name,
            ok=False,
            fallback=True,
            latency_ms=latency,
            provider=str(provider),
            model=str(model),
            error="llm_unavailable",
        )
        return None, "rules"
    try:
        if validator:
            validate_llm_payload(raw, validator=validator, confirmed_fact_ids={f["id"] for f in ctx["confirmed_facts"]})
        meta = raw.get("_meta") if isinstance(raw.get("_meta"), dict) else {}
        log_llm_call(
            db,
            task=task,
            schema_name=schema_name,
            ok=True,
            fallback=False,
            latency_ms=latency,
            provider=str(meta.get("provider") or provider),
            model=str(meta.get("model") or model),
            prompt_tokens=meta.get("prompt_tokens"),
            completion_tokens=meta.get("completion_tokens"),
            total_tokens=meta.get("total_tokens"),
        )
        raw["engine"] = "llm"
        return raw, "llm"
    except ValueError as e:
        log_llm_call(
            db,
            task=task,
            schema_name=schema_name,
            ok=False,
            fallback=True,
            latency_ms=latency,
            provider=str(provider),
            model=str(model),
            error=str(e),
        )
        return None, "rules"
