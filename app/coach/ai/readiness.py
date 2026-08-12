"""准备度规则引擎。"""

from __future__ import annotations

from typing import Any

from app.coach.guardrails import ensure_disclaimer
from app.coach.models import READINESS_STAGES


def assess_readiness(
    *,
    answers: dict[str, Any],
    facts: list[dict[str, Any]],
) -> dict[str, Any]:
    confirmed = [f for f in facts if f.get("status") == "confirmed"]
    target = str(answers.get("target_role") or answers.get("q1") or "").strip()
    materials = str(answers.get("materials") or answers.get("q2") or "").strip()
    applications = str(answers.get("applications") or answers.get("q3") or "").strip()

    if not target and not confirmed:
        stage = "信息不足"
        blockers = ["缺少目标方向", "缺少已确认经历事实"]
        focus = ["先补充目标城市/岗位", "上传简历并确认事实"]
        confidence = "低"
    elif not materials and len(confirmed) < 2:
        stage = "准备期"
        blockers = ["简历事实未充分确认"]
        focus = ["确认简历事实", "补 1 个可讲述项目"]
        confidence = "中"
    elif "面试" in applications:
        stage = "面试期"
        blockers = ["面试表达稳定性未知"]
        focus = ["完成 1 场模拟面试", "整理故事库"]
        confidence = "中"
    elif applications:
        stage = "投递期"
        blockers = ["投递后跟进不足"]
        focus = ["记录投递进度", "针对优先岗位改简历"]
        confidence = "中"
    else:
        stage = "探索期" if not target else "准备期"
        blockers = ["执行节奏未建立"][:3]
        focus = ["明确本周 3 个行动", "确认目标岗位硬条件"]
        confidence = "中"

    if stage not in READINESS_STAGES:
        stage = "信息不足"

    report = {
        "stage": stage,
        "strengths": [f.get("text") for f in confirmed[:3]],
        "blockers": blockers[:3],
        "missing_info": [] if confirmed else ["已确认事实"],
        "weekly_focus": focus[:3],
        "citations": [
            {"source": "fact", "id": f.get("id"), "text": f.get("text")} for f in confirmed[:5]
        ],
        "confidence": confidence,
        "confidence_reason": "基于已确认事实与问卷作文本阶段判断，非能力总分",
    }
    return ensure_disclaimer(report)


_READINESS_LLM_SYSTEM = """你是校招准备度陪伴助手。仅依据 companion_context 中 confirmed_facts 与问卷回答判断阶段。
禁止能力总分与录用概率。stage 只能是：探索期、准备期、投递期、面试期、收束期、信息不足。
输出 JSON：stage、strengths、blockers、missing_info、weekly_focus、citations、confidence、confidence_reason。
不要 Markdown 围栏。"""


def try_llm_readiness(
    db: Any,
    *,
    user_id: str,
    answers: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    from app.coach.ai.llm_invoke import invoke_companion_llm

    import json

    extra = f"问卷回答:\n{json.dumps(answers, ensure_ascii=False)}\n请输出 readiness 报告 JSON。"
    raw, engine = invoke_companion_llm(
        db,
        user_id=user_id,
        task="readiness.assess",
        schema_name="readiness_report",
        system=_READINESS_LLM_SYSTEM,
        user_extra=extra,
        validator="readiness_report",
    )
    if raw is None:
        return None, engine
    raw.pop("engine", None)
    raw.pop("_meta", None)
    if raw.get("stage") and raw.get("stage") not in READINESS_STAGES:
        return None, "rules"
    return ensure_disclaimer(raw), engine
