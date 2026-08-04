"""Strict automatic approval rules for pending job records."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.collector.filters import is_noise_title, list_date_cutoff, parse_date_loose
from app.collector.label_fields import split_jd_sections
from app.timeutil import today as cn_today


_EMPTY_COMPANY_VALUES = {
    "-",
    "--",
    "unknown",
    "未知",
    "未知公司",
    "待确认",
    "待补充",
}


@dataclass(frozen=True)
class AutoReviewAssessment:
    job_id: str
    title: str
    eligible: bool
    reasons: tuple[str, ...] = ()
    published_on: date | None = None
    duties_length: int = 0
    requirements_length: int = 0


@dataclass
class AutoReviewReport:
    assessments: list[AutoReviewAssessment] = field(default_factory=list)

    @property
    def eligible_ids(self) -> list[str]:
        return [item.job_id for item in self.assessments if item.eligible and item.job_id]

    @property
    def skipped(self) -> list[AutoReviewAssessment]:
        return [item for item in self.assessments if not item.eligible]

    def summary_zh(self) -> str:
        lines = [
            f"共检查 {len(self.assessments)} 条；符合自动审核 {len(self.eligible_ids)} 条；"
            f"保留人工审核 {len(self.skipped)} 条。"
        ]
        reason_counts = Counter(reason for item in self.skipped for reason in item.reasons)
        if reason_counts:
            labels = "；".join(f"{reason} {count} 条" for reason, count in reason_counts.items())
            lines.append(f"未通过原因：{labels}。")
        return "\n".join(lines)


def _meaningful_length(text: str | None) -> int:
    return len("".join(ch for ch in (text or "").strip() if not ch.isspace()))


def _published_date(job: dict[str, Any]) -> date | None:
    for key in ("open_at", "list_updated_at", "published_at"):
        raw = job.get(key)
        parsed = parse_date_loose(str(raw).strip() if raw is not None else None)
        if parsed is not None:
            return parsed
    return None


def assess_job_for_auto_review(
    job: dict[str, Any],
    *,
    today: date | None = None,
    months: int = 3,
    min_section_chars: int = 10,
) -> AutoReviewAssessment:
    """Return a strict, explainable automatic-review decision without mutating data."""
    day = today or cn_today()
    cutoff = list_date_cutoff(months=max(1, int(months)), today=day)
    reasons: list[str] = []

    company = str(job.get("company") or "").strip()
    if not company or company.lower() in _EMPTY_COMPANY_VALUES:
        reasons.append("公司缺失")

    title = str(job.get("title") or "").strip()
    if not title or is_noise_title(title):
        reasons.append("岗位名称无效")

    duties, requirements = split_jd_sections(str(job.get("jd_text") or ""))
    duties_length = _meaningful_length(duties)
    requirements_length = _meaningful_length(requirements)
    if duties_length < min_section_chars:
        reasons.append("岗位要求不完整")
    if requirements_length < min_section_chars:
        reasons.append("任职要求不完整")

    published_on = _published_date(job)
    if published_on is None:
        reasons.append("岗位发布时间缺失")
    elif published_on < cutoff:
        reasons.append("岗位发布时间超过3个月")
    elif published_on > day:
        reasons.append("岗位发布时间晚于今天")

    return AutoReviewAssessment(
        job_id=str(job.get("id") or "").strip(),
        title=title,
        eligible=not reasons,
        reasons=tuple(reasons),
        published_on=published_on,
        duties_length=duties_length,
        requirements_length=requirements_length,
    )


def assess_jobs_for_auto_review(
    jobs: list[dict[str, Any]],
    *,
    today: date | None = None,
    months: int = 3,
) -> AutoReviewReport:
    return AutoReviewReport(
        assessments=[
            assess_job_for_auto_review(job, today=today, months=months) for job in jobs
        ]
    )
