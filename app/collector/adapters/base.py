"""适配器公共类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParseResult:
    title: str | None = None
    jd_text: str | None = None
    recruit_project: str | None = None
    recruit_bucket: str | None = None
    work_location: str | None = None
    deadline: str | None = None
    apply_url: str | None = None
    job_tags: list[str] = field(default_factory=list)
    raw_category: str | None = None
    education: str | None = None
    salary_range: str | None = None
    headcount: str | None = None
    parse_status: str = "ok"
    confidence: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)

    def to_job_fields(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "jd_text": self.jd_text,
            "recruit_project": self.recruit_project,
            "recruit_bucket": self.recruit_bucket,
            "work_location": self.work_location,
            "deadline": self.deadline,
            "apply_url": self.apply_url,
            "job_tags": self.job_tags,
            "raw_category": self.raw_category,
            "education": self.education,
            "salary_range": self.salary_range,
            "headcount": self.headcount,
            "parse_status": self.parse_status,
            "confidence": self.confidence,
        }
