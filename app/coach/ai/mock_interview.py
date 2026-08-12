"""模拟面试与反馈蓝图。"""

from __future__ import annotations

from typing import Any

from app.coach.guardrails import ensure_disclaimer, reject_hire_probability_language
from app.coach.knowledge import knowledge_context_for_interview
from app.coach.ai import try_complete_json_or_none


def build_questions(
    *,
    job: dict[str, Any] | None = None,
    track: str = "campus_general",
    limit: int = 4,
) -> list[dict[str, Any]]:
    ctx = knowledge_context_for_interview(track=track)
    questions = []
    for q in ctx["questions"][:limit]:
        questions.append(
            {
                "id": q.get("id"),
                "question": q.get("question"),
                "intent": q.get("intent"),
                "followups": q.get("followups") or [],
            }
        )
    if job and job.get("title"):
        questions.insert(
            0,
            {
                "id": "job_fit",
                "question": f"结合「{job.get('title')}」这个岗位，你为什么觉得自己匹配？",
                "intent": "岗位动机",
                "followups": ["你用哪段经历证明？"],
            },
        )
    return questions[:limit]


def feedback_for_turn(*, question: dict[str, Any], answer: str) -> dict[str, Any]:
    text = (answer or "").strip()
    reject_hire_probability_language(text)
    llm = try_complete_json_or_none(
        task="interview.feedback",
        schema_name="interview_feedback",
        system="根据题目与回答输出反馈 JSON，字段: what_heard, working[], gaps[], priority_move, next_step, structure_ok, uses_experience, job_relevance, quote, question。不得编造经历。",
        user=f"question={question}\nanswer={text}\nknowledge={knowledge_context_for_interview()}",
    )
    if isinstance(llm, dict) and llm.get("what_heard"):
        try:
            reject_hire_probability_language(str(llm))
            return ensure_disclaimer(
                {
                    "what_heard": llm.get("what_heard"),
                    "working": list(llm.get("working") or [])[:5],
                    "gaps": list(llm.get("gaps") or [])[:5],
                    "priority_move": llm.get("priority_move") or "补充证据后重答",
                    "next_step": llm.get("next_step") or "进入下一题或重练本题",
                    "structure_ok": bool(llm.get("structure_ok")),
                    "uses_experience": bool(llm.get("uses_experience")),
                    "job_relevance": llm.get("job_relevance") or "中",
                    "quote": str(llm.get("quote") or text)[:80],
                    "question": question.get("question"),
                    "engine": "llm",
                }
            )
        except ValueError:
            pass
    structure_ok = len(text) >= 40
    uses_experience = any(k in text for k in ("我负责", "我做了", "项目", "实习", "结果"))
    strengths = []
    gaps = []
    if structure_ok:
        strengths.append("回答有一定完整度")
    else:
        gaps.append("过短，缺少背景-行动-结果")
    if uses_experience:
        strengths.append("提到了具体经历")
    else:
        gaps.append("缺少可核验的个人经历")
    if not strengths:
        strengths.append("已提交回答，可继续追问细化")
    fb = {
        "what_heard": text[:120] or "（空回答）",
        "working": strengths,
        "gaps": gaps,
        "priority_move": gaps[0] if gaps else "保持证据导向，补充结果验证",
        "next_step": "用一条已确认事实重答本题，或进入下一题",
        "structure_ok": structure_ok,
        "uses_experience": uses_experience,
        "job_relevance": "中",
        "quote": text[:80],
        "question": question.get("question"),
        "engine": "rules",
    }
    return ensure_disclaimer(fb)


def overall_feedback(turns: list[dict[str, Any]]) -> dict[str, Any]:
    improvements = []
    for t in turns:
        fb = t.get("feedback") or {}
        improvements.extend(list(fb.get("gaps") or [])[:1])
    tasks = [
        {"title": "把最长项目改写成 STAR+R 故事", "estimated_minutes": 25},
        {"title": "针对薄弱题再练一轮", "estimated_minutes": 15},
    ]
    result = {
        "improvements": improvements[:5] or ["继续用证据回答，避免空泛形容词"],
        "recommended_retry_questions": [t.get("question") for t in turns[:2]],
        "action_tasks": tasks,
    }
    return ensure_disclaimer(result)


def build_storybank(*, facts: list[dict[str, Any]], strengths: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stories = []
    for i, f in enumerate(facts[:5]):
        text = str(f.get("text") or "")
        stories.append(
            {
                "id": f"story_{i+1}",
                "title": text[:24] or f"故事{i+1}",
                "situation": "（待用户补充背景）",
                "task": "（待用户补充任务）",
                "action": text,
                "result": "（若有可核验结果请确认后填写）",
                "reflection": "下次可更快对齐目标与指标",
                "fact_ids": [f.get("id")],
                "related_strengths": [
                    s.get("name") for s in strengths if s.get("fact_ref") == f.get("id")
                ],
            }
        )
    return stories
