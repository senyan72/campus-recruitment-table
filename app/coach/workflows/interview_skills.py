"""面试故事库与反馈 workflows。"""

from __future__ import annotations

from typing import Any

from app.coach.ai.mock_interview import (
    build_questions,
    build_storybank,
    feedback_for_turn,
    overall_feedback,
)
from app.coach.database import CoachDB
from app.coach.guardrails import ensure_disclaimer
from app.coach.workflows.base import WorkflowRegistry, make_workflow
from app.timeutil import utc_now_iso


def register_interview_workflows(registry: WorkflowRegistry) -> None:
    registry.register(
        make_workflow(
            name="interview.storybank",
            version="v1-rules",
            description="从已确认事实生成 STAR+R 故事库",
            input_schema={"type": "object", "required": ["user_id"]},
            output_schema={"type": "object"},
            handler=_storybank,
        )
    )
    registry.register(
        make_workflow(
            name="interview.feedback",
            version="v1-rules",
            description="单题结构化反馈蓝图（无录用概率）",
            input_schema={
                "type": "object",
                "required": ["question", "answer"],
                "properties": {"question": {"type": "object"}, "answer": {"type": "string"}},
            },
            output_schema={"type": "object"},
            handler=lambda p, c: feedback_for_turn(question=p.get("question") or {}, answer=p.get("answer") or ""),
        )
    )
    registry.register(
        make_workflow(
            name="interview.mock.start",
            version="v1-rules",
            description="开始文字模拟面试（知识包选题）",
            input_schema={"type": "object", "required": ["user_id"]},
            output_schema={"type": "object"},
            handler=_mock_start,
        )
    )
    registry.register(
        make_workflow(
            name="interview.mock.answer",
            version="v1-rules",
            description="回答当前题并返回反馈；一次一问",
            input_schema={"type": "object", "required": ["session_id", "answer"]},
            output_schema={"type": "object"},
            handler=_mock_answer,
        )
    )
    registry.register(
        make_workflow(
            name="interview.debrief",
            version="v1-rules",
            description="真实面试后快速复盘（P1，已实现最小版）",
            input_schema={"type": "object", "required": ["user_id", "notes"]},
            output_schema={"type": "object"},
            handler=_debrief,
        )
    )


def _storybank(payload: dict[str, Any], context: dict[str, Any] | None) -> dict[str, Any]:
    db: CoachDB = (context or {})["db"]
    user_id = payload["user_id"]
    facts = db.list_confirmed_facts(user_id)
    strengths_row = db.fetchone(
        "SELECT payload_json FROM strengths WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    )
    strengths = db.loads(strengths_row["payload_json"], []) if strengths_row else []
    stories = build_storybank(facts=facts, strengths=strengths)
    now = utc_now_iso()
    bid = db.new_id("sb_")
    existing = db.fetchone("SELECT id FROM storybank WHERE user_id=? ORDER BY updated_at DESC LIMIT 1", (user_id,))
    if existing:
        db.execute(
            "UPDATE storybank SET payload_json=?, updated_at=? WHERE id=?",
            (db.dumps(stories), now, existing["id"]),
        )
        bid = existing["id"]
    else:
        db.execute(
            "INSERT INTO storybank(id, user_id, payload_json, created_at, updated_at) VALUES(?,?,?,?,?)",
            (bid, user_id, db.dumps(stories), now, now),
        )
    return ensure_disclaimer({"storybank_id": bid, "stories": stories})


def _mock_start(payload: dict[str, Any], context: dict[str, Any] | None) -> dict[str, Any]:
    db: CoachDB = (context or {})["db"]
    questions = build_questions(job=payload.get("job"), track=payload.get("track") or "campus_general")
    sid = db.new_id("iv_")
    db.execute(
        "INSERT INTO interview_sessions(id, user_id, job_json, status, questions_json, turns_json, created_at) VALUES(?,?,?,?,?,?,?)",
        (
            sid,
            payload["user_id"],
            db.dumps(payload.get("job") or {}),
            "active",
            db.dumps(questions),
            db.dumps([]),
            utc_now_iso(),
        ),
    )
    current = questions[0] if questions else None
    return ensure_disclaimer(
        {
            "session_id": sid,
            "index": 0,
            "total": len(questions),
            "question": current,
            "note": "一次只答一题；语音 STT 生产化暂缓，当前文字兜底",
        }
    )


def _mock_answer(payload: dict[str, Any], context: dict[str, Any] | None) -> dict[str, Any]:
    db: CoachDB = (context or {})["db"]
    row = db.fetchone("SELECT * FROM interview_sessions WHERE id=?", (payload["session_id"],))
    if not row:
        raise ValueError("面试会话不存在")
    questions = db.loads(row["questions_json"], [])
    turns = db.loads(row["turns_json"], [])
    idx = len(turns)
    if idx >= len(questions):
        raise ValueError("本场问题已答完")
    q = questions[idx]
    fb = feedback_for_turn(question=q, answer=payload.get("answer") or "")
    turns.append({"question": q, "answer": payload.get("answer"), "feedback": fb})
    done = len(turns) >= len(questions)
    overall = overall_feedback(turns) if done else None
    db.execute(
        "UPDATE interview_sessions SET turns_json=?, status=?, feedback_json=? WHERE id=?",
        (
            db.dumps(turns),
            "completed" if done else "active",
            db.dumps(overall) if overall else None,
            row["id"],
        ),
    )
    nxt = questions[len(turns)] if not done else None
    return ensure_disclaimer(
        {
            "feedback": fb,
            "done": done,
            "next_question": nxt,
            "overall": overall,
            "index": len(turns),
            "total": len(questions),
        }
    )


def _debrief(payload: dict[str, Any], context: dict[str, Any] | None) -> dict[str, Any]:
    db: CoachDB = (context or {})["db"]
    notes = str(payload.get("notes") or "")
    tasks = [
        {
            "title": "根据面试复盘补充故事库一条 Reflection",
            "reason": notes[:80],
            "estimated_minutes": 20,
            "source": "interview.debrief",
        }
    ]
    now = utc_now_iso()
    created = []
    for t in tasks:
        tid = db.new_id("task_")
        db.execute(
            "INSERT INTO action_tasks(id, user_id, title, reason, status, source, estimated_minutes, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                tid,
                payload["user_id"],
                t["title"],
                t["reason"],
                "open",
                t["source"],
                t["estimated_minutes"],
                now,
            ),
        )
        created.append({**t, "id": tid})
    return ensure_disclaimer({"notes": notes, "tasks": created})
