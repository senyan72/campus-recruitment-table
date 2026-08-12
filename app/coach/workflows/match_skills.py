"""匹配解释 workflow。"""

from __future__ import annotations

from typing import Any

from app.coach.ai.matching import match_job, try_llm_match
from app.coach.database import CoachDB
from app.coach.services.jobs_bridge import get_job_for_match
from app.coach.workflows.base import WorkflowRegistry, make_workflow
from app.timeutil import utc_now_iso


def register_match_workflows(registry: WorkflowRegistry) -> None:
    registry.register(
        make_workflow(
            name="match.explain",
            version="v1-rules",
            description="四档匹配 + JD↔证据表 + why/why not + 缺口补强",
            input_schema={
                "type": "object",
                "required": ["user_id", "job"],
                "properties": {"user_id": {"type": "string"}, "job": {"type": "object"}},
            },
            output_schema={"type": "object"},
            handler=_explain,
        )
    )


def _explain(payload: dict[str, Any], context: dict[str, Any] | None) -> dict[str, Any]:
    db: CoachDB = (context or {})["db"]
    user_id = payload["user_id"]
    job = payload.get("job") or {}
    if not job.get("description") and payload.get("job_id"):
        campus_path = (context or {}).get("campus_db_path")
        loaded = get_job_for_match(job_id=str(payload["job_id"]), campus_db_path=campus_path)
        if loaded:
            job = {**loaded, **job}
    facts = db.list_confirmed_facts(user_id)
    strengths_row = db.fetchone(
        "SELECT payload_json FROM strengths WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    )
    strengths = db.loads(strengths_row["payload_json"], []) if strengths_row else []
    llm_result, engine = try_llm_match(db, user_id=user_id, job=job)
    if llm_result is not None:
        result = llm_result
    else:
        result = match_job(job=job, facts=facts, strengths=strengths)
        engine = "rules"
    result["engine"] = engine
    mid = db.new_id("match_")
    db.execute(
        "INSERT INTO job_matches(id, user_id, job_id, job_json, result_json, created_at) VALUES(?,?,?,?,?,?)",
        (mid, user_id, str(job.get("id") or ""), db.dumps(job), db.dumps(result), utc_now_iso()),
    )
    result["match_id"] = mid
    return result
