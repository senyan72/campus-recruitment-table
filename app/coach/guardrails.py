"""护栏：needs_proof / fact_ids / 禁录用概率话术。"""

from __future__ import annotations

import re
from typing import Any

from app.coach.models import DISCLAIMER_AI

_HIRE_PROB_PATTERNS = re.compile(
    r"(录用概率|录取概率|一定能拿offer|保证拿到offer|通过率\s*\d+%|必过)",
    re.I,
)

_FABRICATED_METRIC = re.compile(
    r"(提升了?\s*\d+\s*%|增长了?\s*\d+\s*%|节省了?\s*\d+|带来了?\s*\d+\s*万)",
)


def ensure_disclaimer(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    out.setdefault("disclaimer", DISCLAIMER_AI)
    return out


def reject_hire_probability_language(text: str) -> None:
    if _HIRE_PROB_PATTERNS.search(text or ""):
        raise ValueError("输出含录用/Offer 承诺或概率话术，已按产品护栏拒绝")


def sanitize_suggestion(item: dict[str, Any], *, confirmed_fact_ids: set[str]) -> dict[str, Any]:
    """建议必须挂 fact_ids；无证据的量化改写标 needs_proof。"""
    out = dict(item)
    fact_ids = [str(x) for x in (out.get("fact_ids") or []) if str(x).strip()]
    unknown = [fid for fid in fact_ids if fid not in confirmed_fact_ids]
    suggestion = str(out.get("suggestion") or out.get("after_text") or "")
    needs_proof = bool(out.get("needs_proof"))
    if unknown:
        needs_proof = True
        fact_ids = [fid for fid in fact_ids if fid in confirmed_fact_ids]
    if _FABRICATED_METRIC.search(suggestion) and not fact_ids:
        needs_proof = True
    out["fact_ids"] = fact_ids
    out["needs_proof"] = needs_proof
    out.setdefault("status", "proposed")
    reject_hire_probability_language(
        " ".join(
            str(out.get(k) or "")
            for k in ("suggestion", "reason", "before_text", "after_text")
        )
    )
    return out


def assert_suggestions_safe(
    suggestions: list[dict[str, Any]],
    *,
    confirmed_fact_ids: set[str],
    allow_needs_proof: bool = True,
) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for raw in suggestions:
        item = sanitize_suggestion(raw, confirmed_fact_ids=confirmed_fact_ids)
        if item.get("needs_proof") and not allow_needs_proof:
            continue
        cleaned.append(item)
    return cleaned


def filter_approved_only(suggestions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """未批准且 needs_proof 的建议不得进入生成版本。"""
    out: list[dict[str, Any]] = []
    for s in suggestions:
        if s.get("status") != "approved":
            continue
        if s.get("needs_proof"):
            continue
        out.append(s)
    return out
