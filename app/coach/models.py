"""常量与枚举。"""

from __future__ import annotations

READINESS_STAGES = ("探索期", "准备期", "投递期", "面试期", "收束期", "信息不足")

MATCH_TIERS = ("优先投", "可以投", "补充后投", "暂不建议")

TASK_STATUSES = ("open", "done", "deferred", "abandoned", "split")

RESUME_SUGGEST_SCENARIOS = ("general", "bullets", "quantify", "keywords")

DISCLAIMER_AI = (
    "本输出由 AI 陪伴生成，基于你已确认的事实；不构成录用承诺、能力测评或人事决策依据。"
)
