"""模型适配层：规则 fallback + 外接 LLM stub（不绑厂商）。"""

from __future__ import annotations

import os
from typing import Any, Protocol


class ModelAdapter(Protocol):
    model_version: str

    def complete_json(
        self,
        *,
        task: str,
        system: str,
        user: str,
        schema_name: str,
    ) -> dict[str, Any]:
        ...


class RuleBasedAdapter:
    """无 LLM 时的确定性兜底；具体逻辑由各 skill 的 rules 函数完成。"""

    model_version = "rules-v1"

    def complete_json(
        self,
        *,
        task: str,
        system: str,
        user: str,
        schema_name: str,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "fallback": True,
            "task": task,
            "schema_name": schema_name,
            "message": "RuleBasedAdapter 不直接生成；请走 skill 内规则引擎",
        }


class ExternalLLMAdapterStub:
    """有 Key 时占位：真正 HTTP 由你外接实现替换本类或子类化。"""

    model_version = "external-stub"

    def __init__(self) -> None:
        self.provider = (os.environ.get("COACH_LLM_PROVIDER") or "").strip()
        self.base_url = (os.environ.get("COACH_LLM_BASE_URL") or "").strip()
        self.model = (os.environ.get("COACH_LLM_MODEL") or "").strip()
        self.api_key = (os.environ.get("COACH_LLM_API_KEY") or "").strip()

    def complete_json(
        self,
        *,
        task: str,
        system: str,
        user: str,
        schema_name: str,
    ) -> dict[str, Any]:
        # 故意不发真实 HTTP，避免绑死厂商；接好后替换此方法即可。
        return {
            "ok": False,
            "fallback": True,
            "task": task,
            "schema_name": schema_name,
            "provider": self.provider,
            "model": self.model,
            "message": "ExternalLLMAdapterStub：请实现真实 HTTP 调用后替换",
        }


def get_model_adapter() -> ModelAdapter:
    if (os.environ.get("COACH_FORCE_RULES") or "1").strip() in {"1", "true", "TRUE"}:
        return RuleBasedAdapter()
    key = (os.environ.get("COACH_LLM_API_KEY") or "").strip()
    provider = (os.environ.get("COACH_LLM_PROVIDER") or "").strip()
    if key and provider:
        return ExternalLLMAdapterStub()
    return RuleBasedAdapter()
