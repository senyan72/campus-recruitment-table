"""匹配规则 + 证据表解释。"""

from __future__ import annotations

from typing import Any

from app.coach.guardrails import ensure_disclaimer
from app.coach.models import MATCH_TIERS


def match_job(
    *,
    job: dict[str, Any],
    facts: list[dict[str, Any]],
    strengths: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    strengths = strengths or []
    title = str(job.get("title") or job.get("job_title") or "")
    city = str(job.get("city") or job.get("location") or "")
    edu = str(job.get("education") or job.get("degree") or "")
    jd = str(job.get("description") or job.get("jd") or title)

    hard_failures: list[str] = []
    checks: list[dict[str, Any]] = []

    profile_cities = " ".join(str(f.get("text") or "") for f in facts)
    if city and city not in ("不限", "远程") and city not in profile_cities and facts:
        # soft unless user explicitly constrained — keep as gap not hard fail for campus
        checks.append({"name": "城市", "ok": city in profile_cities, "detail": city})
    if edu and "博士" in edu:
        hard_failures.append("学历要求可能不满足（博士）")
        checks.append({"name": "学历", "ok": False, "detail": edu})

    table: list[dict[str, Any]] = []
    req_keywords = [w for w in ("实习", "项目", "沟通", "数据", "开发", "协作", "分析") if w in jd or w in title]
    if not req_keywords:
        req_keywords = ["综合素质", "学习能力"]
    fact_texts = [str(f.get("text") or "") for f in facts]
    for req in req_keywords:
        hit = next((t for t in fact_texts if req in t), None)
        str_hit = next((s for s in strengths if req in str(s.get("evidence") or "")), None)
        table.append(
            {
                "requirement": req,
                "evidence": hit or (str_hit or {}).get("evidence") or "",
                "status": "matched" if (hit or str_hit) else "gap",
            }
        )

    gaps = [r["requirement"] for r in table if r["status"] == "gap"]
    matched = [r["requirement"] for r in table if r["status"] == "matched"]

    if hard_failures:
        tier = "暂不建议"
        cost = "高"
        next_action = "先确认硬条件是否可满足，再决定是否投递"
    elif len(matched) >= 2 and len(gaps) <= 1:
        tier = "优先投"
        cost = "低"
        next_action = "使用已确认证据定制简历并投递"
    elif matched:
        tier = "可以投"
        cost = "中"
        next_action = "补齐 1 个缺口证据后再投，或先投同时准备故事"
    elif facts:
        tier = "补充后投"
        cost = "中"
        next_action = "先补项目/实习证据，再针对 JD 改简历"
    else:
        tier = "暂不建议"
        cost = "高"
        next_action = "先确认简历事实与目标方向"

    assert tier in MATCH_TIERS
    why_apply = [f"已有证据支撑：{m}" for m in matched[:3]] or ["信息有限，仅作探索性了解"]
    why_not = [f"缺口：{g}" for g in gaps[:3]] + hard_failures
    mitigations = [
        {"gap": g, "plan": f"本周补充与「{g}」相关的可讲述经历或作品", "hours": 3} for g in gaps[:3]
    ]

    result = {
        "tier": tier,
        "reasons": why_apply[:4],
        "gaps": gaps[:4],
        "hard_constraints": {
            "failed": bool(hard_failures),
            "failures": hard_failures,
            "checks": checks,
        },
        "cost": cost,
        "next_action": next_action,
        "requirement_evidence_table": table,
        "why_apply": why_apply,
        "why_not": why_not,
        "gap_mitigations": mitigations,
        "version": "v1-rules",
    }
    return ensure_disclaimer(result)
