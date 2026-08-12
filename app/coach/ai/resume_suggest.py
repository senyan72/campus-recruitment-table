"""简历建议多场景规则引擎。"""

from __future__ import annotations

from typing import Any

from app.coach.guardrails import assert_suggestions_safe, ensure_disclaimer


def generate_suggestions(
    *,
    sections: dict[str, Any] | str,
    job: dict[str, Any] | None = None,
    facts: list[dict[str, Any]] | None = None,
    scenario: str = "general",
) -> list[dict[str, Any]]:
    facts = facts or []
    confirmed_ids = {str(f.get("id")) for f in facts if f.get("status") == "confirmed"}
    if isinstance(sections, dict):
        blob = "\n".join(str(v) for v in sections.values())
    else:
        blob = str(sections or "")
    lines = [ln.strip() for ln in blob.splitlines() if ln.strip()][:12]
    job_title = str((job or {}).get("title") or (job or {}).get("job_title") or "目标岗位")
    suggestions: list[dict[str, Any]] = []

    if scenario == "bullets":
        for i, line in enumerate(lines[:5]):
            suggestions.append(
                {
                    "id": f"sug_b_{i+1}",
                    "section": "经历",
                    "before_text": line,
                    "suggestion": f"将「{line[:40]}」改写为：动作 + 对象 + 结果（仅使用已确认事实）",
                    "reason": "成就化表达，避免『参与/协助』空泛措辞",
                    "fact_ids": list(confirmed_ids)[:1],
                    "needs_proof": not confirmed_ids,
                    "scenario": scenario,
                }
            )
    elif scenario == "quantify":
        for i, line in enumerate(lines[:5]):
            has_digit = any(ch.isdigit() for ch in line)
            suggestions.append(
                {
                    "id": f"sug_q_{i+1}",
                    "section": "经历",
                    "before_text": line,
                    "suggestion": (
                        f"若有可核验数据，可为「{line[:40]}」补充结果指标"
                        if not has_digit
                        else f"核验「{line[:40]}」中数字来源，确保可答辩"
                    ),
                    "reason": "量化必须可追溯；未知数字标记 needs_proof，禁止编造",
                    "fact_ids": list(confirmed_ids)[:1],
                    "needs_proof": not has_digit,
                    "scenario": scenario,
                }
            )
    elif scenario == "keywords":
        jd = str((job or {}).get("description") or (job or {}).get("jd") or job_title)
        keys = [k for k in ("数据分析", "项目管理", "沟通协作", "Python", "实习", "用户研究") if k in jd]
        if not keys:
            keys = ["岗位关键词"]
        suggestions.append(
            {
                "id": "sug_k_1",
                "section": "总结/技能",
                "before_text": lines[0] if lines else "",
                "suggestion": f"在技能或摘要中自然对齐 JD 关键词：{', '.join(keys[:4])}（仅写已具备证据的）",
                "reason": f"针对「{job_title}」提高岗位可读性，而非虚构技能",
                "fact_ids": list(confirmed_ids)[:2],
                "needs_proof": not confirmed_ids,
                "scenario": scenario,
            }
        )
    else:  # general
        suggestions.append(
            {
                "id": "sug_g_1",
                "section": "总体",
                "before_text": lines[0] if lines else "",
                "suggestion": f"针对「{job_title}」突出与岗位最相关的 2 条已确认经历，弱化无关描述",
                "reason": "岗位定制应基于已确认事实",
                "fact_ids": list(confirmed_ids)[:2],
                "needs_proof": not confirmed_ids,
                "scenario": "general",
            }
        )

    return assert_suggestions_safe(suggestions, confirmed_fact_ids=confirmed_ids)


def apply_approved_suggestions(
    sections: dict[str, Any],
    suggestions: list[dict[str, Any]],
) -> dict[str, Any]:
    out = dict(sections)
    notes: list[str] = []
    for s in suggestions:
        if s.get("status") != "approved" or s.get("needs_proof"):
            continue
        sec = str(s.get("section") or "经历")
        prev = str(out.get(sec) or "")
        out[sec] = (prev + "\n" + str(s.get("suggestion") or "")).strip()
        notes.append(str(s.get("id")))
    return {"sections": out, "applied_suggestions": notes}


def diff_versions(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    keys = sorted(set(before) | set(after))
    diffs: list[dict[str, Any]] = []
    for k in keys:
        b = before.get(k)
        a = after.get(k)
        if b == a:
            continue
        if b is None:
            change = "added"
        elif a is None:
            change = "removed"
        else:
            change = "modified"
        diffs.append(
            {
                "section": k,
                "change": change,
                "before": b,
                "after": a,
                "reason": "用户批准建议后的版本差异",
            }
        )
    return diffs


def wrap_suggest_result(suggestions: list[dict[str, Any]], scenario: str) -> dict[str, Any]:
    return ensure_disclaimer({"scenario": scenario, "suggestions": suggestions})
