"""岗位 URL 适配器。

勿在此模块顶层导入 router/各适配器：`label_fields` 等只需 `adapters.base`，
若此处再拉起 router→hotjob→label_fields 会形成环状导入。
"""

from __future__ import annotations

from typing import Any

__all__ = ["parse_job_url"]


def __getattr__(name: str) -> Any:
    if name == "parse_job_url":
        from app.collector.adapters.router import parse_job_url

        return parse_job_url
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
