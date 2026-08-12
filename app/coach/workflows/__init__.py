"""默认注册所有 AI 陪伴 workflows。"""

from __future__ import annotations

from app.coach.workflows.base import WorkflowRegistry
from app.coach.workflows.evidence_skills import register_evidence_workflows
from app.coach.workflows.interview_skills import register_interview_workflows
from app.coach.workflows.match_skills import register_match_workflows
from app.coach.workflows.readiness_skills import register_readiness_workflows
from app.coach.workflows.resume_skills import register_resume_workflows

_REGISTRY: WorkflowRegistry | None = None


def register_defaults(registry: WorkflowRegistry) -> None:
    register_evidence_workflows(registry)
    register_readiness_workflows(registry)
    register_resume_workflows(registry)
    register_match_workflows(registry)
    register_interview_workflows(registry)


def get_registry() -> WorkflowRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = WorkflowRegistry()
        register_defaults(_REGISTRY)
    return _REGISTRY


def reset_registry_for_tests() -> WorkflowRegistry:
    global _REGISTRY
    _REGISTRY = WorkflowRegistry()
    register_defaults(_REGISTRY)
    return _REGISTRY
