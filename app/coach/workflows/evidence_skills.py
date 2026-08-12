"""证据 / 优势 workflows。"""

from __future__ import annotations

from typing import Any

from app.coach.ai.evidence import extract_experience_assets, mine_strengths
from app.coach.database import CoachDB
from app.coach.guardrails import ensure_disclaimer
from app.coach.workflows.base import WorkflowRegistry, make_workflow
from app.timeutil import utc_now_iso


def register_evidence_workflows(registry: WorkflowRegistry) -> None:
    registry.register(
        make_workflow(
            name="evidence.extract",
            version="v1-rules",
            description="从简历/文本提取可追溯经历资产（待确认）",
            input_schema={
                "type": "object",
                "required": ["user_id", "text"],
                "properties": {
                    "user_id": {"type": "string"},
                    "text": {"type": "string"},
                    "source": {"type": "string"},
                },
            },
            output_schema={"type": "object", "properties": {"assets": {"type": "array"}}},
            handler=_extract,
        )
    )
    registry.register(
        make_workflow(
            name="strengths.mine",
            version="v1-rules",
            description="证据→行为→能力→岗位信号；标注缺失证据",
            input_schema={
                "type": "object",
                "required": ["user_id"],
                "properties": {"user_id": {"type": "string"}, "assets": {"type": "array"}},
            },
            output_schema={"type": "object", "properties": {"strengths": {"type": "array"}}},
            handler=_mine,
        )
    )


def _extract(payload: dict[str, Any], context: dict[str, Any] | None) -> dict[str, Any]:
    db: CoachDB = (context or {})["db"]
    user_id = payload["user_id"]
    assets = extract_experience_assets(payload.get("text") or "", source=payload.get("source") or "resume")
    now = utc_now_iso()
    # persist assets bundle + pending facts
    aid = db.new_id("asset_")
    db.execute(
        "INSERT INTO experience_assets(id, user_id, payload_json, created_at) VALUES(?,?,?,?)",
        (aid, user_id, db.dumps(assets), now),
    )
    for a in assets:
        fid = db.new_id("fact_")
        a["fact_id"] = fid
        db.execute(
            "INSERT INTO facts(id, user_id, kind, text, status, meta_json, created_at) VALUES(?,?,?,?,?,?,?)",
            (fid, user_id, "experience", a["text"], "pending", db.dumps({"asset_id": a["id"]}), now),
        )
    return ensure_disclaimer({"asset_bundle_id": aid, "assets": assets})


def _mine(payload: dict[str, Any], context: dict[str, Any] | None) -> dict[str, Any]:
    db: CoachDB = (context or {})["db"]
    user_id = payload["user_id"]
    assets = payload.get("assets")
    if not assets:
        facts = db.list_confirmed_facts(user_id)
        if not facts:
            facts = db.fetchall(
                "SELECT * FROM facts WHERE user_id=? ORDER BY created_at LIMIT 20",
                (user_id,),
            )
        assets = [{"id": f.get("id"), "text": f.get("text")} for f in facts]
    strengths = mine_strengths(assets)
    now = utc_now_iso()
    sid = db.new_id("strset_")
    db.execute(
        "INSERT INTO strengths(id, user_id, payload_json, created_at) VALUES(?,?,?,?)",
        (sid, user_id, db.dumps(strengths), now),
    )
    return ensure_disclaimer({"strengths_id": sid, "strengths": strengths})
