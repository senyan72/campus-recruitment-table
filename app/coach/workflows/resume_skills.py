"""简历多场景 workflows。"""

from __future__ import annotations

from typing import Any

from app.coach.ai.resume_suggest import (
    apply_approved_suggestions,
    diff_versions,
    generate_suggestions,
    wrap_suggest_result,
)
from app.coach.database import CoachDB
from app.coach.guardrails import filter_approved_only
from app.coach.models import RESUME_SUGGEST_SCENARIOS
from app.coach.workflows.base import WorkflowRegistry, make_workflow
from app.timeutil import utc_now_iso


def register_resume_workflows(registry: WorkflowRegistry) -> None:
    registry.register(
        make_workflow(
            name="resume.suggest",
            version="v1-rules",
            description="通用简历建议（scenario=general）",
            input_schema={"type": "object", "required": ["user_id", "version_id"]},
            output_schema={"type": "object"},
            handler=lambda p, c: _suggest(p, c, scenario="general"),
        )
    )
    for scenario in ("bullets", "quantify", "keywords"):
        registry.register(
            make_workflow(
                name=f"resume.suggest.{scenario}",
                version="v1-rules",
                description=f"简历建议场景：{scenario}",
                input_schema={"type": "object", "required": ["user_id", "version_id"]},
                output_schema={"type": "object"},
                handler=lambda p, c, s=scenario: _suggest(p, c, scenario=s),
            )
        )
    registry.register(
        make_workflow(
            name="resume.generate",
            version="v1-rules",
            description="仅应用已批准且非 needs_proof 的建议生成新版本",
            input_schema={"type": "object", "required": ["user_id", "version_id", "suggestions"]},
            output_schema={"type": "object"},
            handler=_generate,
        )
    )
    registry.register(
        make_workflow(
            name="resume.diff",
            version="v1-rules",
            description="版本结构化差异",
            input_schema={"type": "object", "required": ["before", "after"]},
            output_schema={"type": "object"},
            handler=lambda p, c: {"diff": diff_versions(p.get("before") or {}, p.get("after") or {})},
        )
    )
    registry.register(
        make_workflow(
            name="resume.export",
            version="v1-rules",
            description="导出文本（DOCX/云存储暂缓）",
            input_schema={"type": "object", "required": ["sections"]},
            output_schema={"type": "object"},
            handler=_export,
        )
    )


def _load_version(db: CoachDB, version_id: str) -> dict[str, Any]:
    row = db.fetchone("SELECT * FROM resume_versions WHERE id=?", (version_id,))
    if not row:
        raise ValueError("简历版本不存在")
    row["sections"] = db.loads(row.pop("sections_json"), {})
    return row


def _suggest(payload: dict[str, Any], context: dict[str, Any] | None, *, scenario: str) -> dict[str, Any]:
    if scenario not in RESUME_SUGGEST_SCENARIOS:
        raise ValueError(f"不支持的 scenario: {scenario}")
    db: CoachDB = (context or {})["db"]
    version = _load_version(db, payload["version_id"])
    facts = db.list_confirmed_facts(payload["user_id"])
    suggestions = generate_suggestions(
        sections=version["sections"],
        job=payload.get("job"),
        facts=facts,
        scenario=scenario,
    )
    sid = db.new_id("rsg_")
    db.execute(
        "INSERT INTO resume_suggestions(id, version_id, user_id, scenario, payload_json, created_at) VALUES(?,?,?,?,?,?)",
        (sid, payload["version_id"], payload["user_id"], scenario, db.dumps(suggestions), utc_now_iso()),
    )
    result = wrap_suggest_result(suggestions, scenario)
    result["suggestion_set_id"] = sid
    return result


def _generate(payload: dict[str, Any], context: dict[str, Any] | None) -> dict[str, Any]:
    db: CoachDB = (context or {})["db"]
    version = _load_version(db, payload["version_id"])
    approved = filter_approved_only(list(payload.get("suggestions") or []))
    applied = apply_approved_suggestions(version["sections"], approved)
    new_id = db.new_id("rv_")
    db.execute(
        "INSERT INTO resume_versions(id, document_id, user_id, parent_id, label, sections_json, created_at) VALUES(?,?,?,?,?,?,?)",
        (
            new_id,
            version["document_id"],
            payload["user_id"],
            version["id"],
            payload.get("label") or "定制版",
            db.dumps(applied["sections"]),
            utc_now_iso(),
        ),
    )
    return {
        "version_id": new_id,
        "sections": applied["sections"],
        "applied_suggestions": applied["applied_suggestions"],
        "diff": diff_versions(version["sections"], applied["sections"]),
    }


def _export(payload: dict[str, Any], context: dict[str, Any] | None) -> dict[str, Any]:
    sections = payload.get("sections") or {}
    lines = [f"# {k}\n{v}" for k, v in sections.items()]
    return {"format": "txt", "content": "\n\n".join(lines), "note": "DOCX/对象存储生产化暂缓"}
