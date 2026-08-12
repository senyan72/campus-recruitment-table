"""产品范围：仅 AI 陪伴。人工 coach 相关能力一律禁用。"""

from __future__ import annotations

# 本阶段明确 out-of-scope（不做入口、不做 API、不做 UI）
HUMAN_COACH_OUT_OF_SCOPE: tuple[str, ...] = (
    "human_coach_request",
    "coach_dispatch",
    "coach_marketplace",
    "human_audio_review",
    "human_grading_workbench",
    "live_human_session",
)

# 暂缓基建（计划明确 defer，仅保留接口注释/骨架位）
DEFERRED_INFRA: tuple[str, ...] = (
    "postgresql_primary",
    "object_storage_signed_url",
    "cloud_stt_production",
    "cloud_ocr_production",
    "job_portal_scraper",
    "gmail_notion_sync",
    "auto_apply",
    "salary_negotiation",
    "offer_comparison",
)

AI_COMPANION_CAPABILITIES: tuple[str, ...] = (
    "profile_evidence",
    "readiness",
    "job_match_explain",
    "resume_scenarios",
    "today_tasks",
    "mock_interview",
    "interview_storybank_feedback",
)


def assert_not_human_coach(feature: str) -> None:
    if feature in HUMAN_COACH_OUT_OF_SCOPE:
        raise PermissionError(
            f"功能「{feature}」属于人工 coach，当前产品范围仅 AI 陪伴，已禁用。"
        )


def is_deferred(feature: str) -> bool:
    return feature in DEFERRED_INFRA
