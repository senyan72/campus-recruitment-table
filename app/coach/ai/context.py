"""统一上下文组装：已确认事实 + 知识库（含文档切片）。"""

from __future__ import annotations

import json
from typing import Any

from app.coach.database import CoachDB
from app.coach.knowledge import build_llm_knowledge_context, search_document_chunks


def build_companion_context(
    db: CoachDB,
    user_id: str,
    *,
    query: str | None = None,
    track: str = "campus_general",
    job: dict[str, Any] | None = None,
    include_documents: bool = True,
) -> dict[str, Any]:
    facts = db.list_confirmed_facts(user_id)
    pending = db.fetchall(
        "SELECT id, text, kind FROM facts WHERE user_id=? AND status='pending' ORDER BY created_at LIMIT 20",
        (user_id,),
    )
    knowledge = build_llm_knowledge_context(
        track=track,
        query=query,
        include_documents=include_documents,
        db=db if include_documents else None,
        owner_id=user_id,
    )
    ctx = {
        "confirmed_facts": [{"id": f["id"], "text": f.get("text"), "kind": f.get("kind")} for f in facts],
        "pending_facts": [{"id": f["id"], "text": f.get("text")} for f in pending],
        "knowledge": knowledge,
        "job": job or {},
        "rules": [
            "仅引用 confirmed_facts 与 knowledge 中的内容作为事实",
            "无证据不得写具体数字/公司/项目成果",
            "不确定使用 needs_proof 或 信息不足",
            "禁止录用概率与保证 Offer",
        ],
    }
    return ctx


def context_as_prompt_block(ctx: dict[str, Any]) -> str:
    return json.dumps(ctx, ensure_ascii=False, indent=2)
