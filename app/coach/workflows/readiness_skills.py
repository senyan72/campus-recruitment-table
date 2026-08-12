"""准备度 workflows。"""

from __future__ import annotations

from typing import Any

from app.coach.ai.readiness import assess_readiness
from app.coach.database import CoachDB
from app.coach.workflows.base import WorkflowRegistry, make_workflow
from app.timeutil import utc_now_iso


def register_readiness_workflows(registry: WorkflowRegistry) -> None:
    registry.register(
        make_workflow(
            name="readiness.assess",
            version="v1-rules",
            description="文本式准备度阶段判断（无能力总分）",
            input_schema={
                "type": "object",
                "required": ["user_id"],
                "properties": {"user_id": {"type": "string"}, "answers": {"type": "object"}},
            },
            output_schema={"type": "object"},
            handler=_assess,
        )
    )
    registry.register(
        make_workflow(
            name="readiness.revise",
            version="v1-rules",
            description="纠正后重新生成准备度版本",
            input_schema={
                "type": "object",
                "required": ["user_id"],
                "properties": {
                    "user_id": {"type": "string"},
                    "answers": {"type": "object"},
                    "corrections": {"type": "object"},
                },
            },
            output_schema={"type": "object"},
            handler=_revise,
        )
    )


def _assess(payload: dict[str, Any], context: dict[str, Any] | None) -> dict[str, Any]:
    db: CoachDB = (context or {})["db"]
    user_id = payload["user_id"]
    facts = db.list_confirmed_facts(user_id)
    report = assess_readiness(answers=payload.get("answers") or {}, facts=facts)
    version = 1
    last = db.fetchone(
        "SELECT version FROM readiness_assessments WHERE user_id=? ORDER BY version DESC LIMIT 1",
        (user_id,),
    )
    if last:
        version = int(last["version"]) + 1
    rid = db.new_id("rdy_")
    db.execute(
        "INSERT INTO readiness_assessments(id, user_id, version, status, confidence, report_json, created_at) VALUES(?,?,?,?,?,?,?)",
        (rid, user_id, version, "active", report.get("confidence"), db.dumps(report), utc_now_iso()),
    )
    report["id"] = rid
    report["version"] = version
    return report


def _revise(payload: dict[str, Any], context: dict[str, Any] | None) -> dict[str, Any]:
    answers = dict(payload.get("answers") or {})
    answers.update(payload.get("corrections") or {})
    return _assess({**payload, "answers": answers}, context)
