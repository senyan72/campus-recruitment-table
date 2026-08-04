"""导出中文 CSV。"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


COLUMNS = [
    ("open_at", "岗位发布时间"),
    ("group_name", "集团"),
    ("company", "招聘单位"),
    ("title", "岗位名称"),
    ("salary_range", "薪资范围"),
    ("headcount", "招聘人数"),
    ("recruit_project", "岗位类型"),
    ("education", "学历要求"),
    ("work_location", "base地"),
    ("jd_text", "岗位介绍"),
    ("company_nature", "企业性质"),
    ("source_url", "招聘原文"),
    ("deadline", "截止时间"),
    ("apply_url", "网申地址"),
    ("industry", "行业"),
    ("my_apply_status", "投递状态"),
]


def _export_cell(job: dict[str, Any], key: str) -> str:
    if key == "open_at":
        from app.ui.job_review import display_page_updated_at

        shown = display_page_updated_at(job)
        return "" if shown == "-" else shown
    return str(job.get(key, "") or "")


def export_jobs_csv(jobs: list[dict[str, Any]], path: Path | str) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([zh for _, zh in COLUMNS])
        for job in jobs:
            writer.writerow([_export_cell(job, key) for key, _ in COLUMNS])
    return out
