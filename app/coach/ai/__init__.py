"""模型适配层：规则 fallback + OpenAI 兼容外接（Qwen/DeepSeek 双厂商）。"""

from __future__ import annotations

import os
from typing import Any, Protocol

from app.coach.ai.llm_client import (
    chat_completions_json,
    llm_status,
    provider_for_task,
    resolve_dual_routing,
    resolve_provider_config,
)


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


class OpenAICompatibleAdapter:
    """真实 HTTP：DeepSeek / 通义 DashScope 兼容模式 / 任意 OpenAI 兼容网关。"""

    def __init__(
        self,
        *,
        provider: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        use_fallback_env: bool = False,
    ) -> None:
        if use_fallback_env:
            dual = resolve_dual_routing()
            provider = provider or dual.get("fallback_provider") or (
                os.environ.get("COACH_LLM_FALLBACK_PROVIDER") or ""
            ).strip()
            api_key = api_key or (os.environ.get("COACH_LLM_FALLBACK_API_KEY") or None)
            base_url = base_url or (os.environ.get("COACH_LLM_FALLBACK_BASE_URL") or None)
            model = model or (os.environ.get("COACH_LLM_FALLBACK_MODEL") or None)
        cfg = resolve_provider_config(
            provider=provider, api_key=api_key, base_url=base_url, model=model
        )
        self.provider = cfg["provider"]
        self.base_url = cfg["base_url"]
        self.model = cfg["model"]
        self.api_key = cfg["api_key"]
        self.model_version = f"{self.provider}:{self.model}" if self.model else "openai-compatible"

    def complete_json(
        self,
        *,
        task: str,
        system: str,
        user: str,
        schema_name: str,
    ) -> dict[str, Any]:
        return chat_completions_json(
            system=system,
            user=user,
            schema_name=schema_name,
            task=task,
            provider=self.provider,
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
        )


# 兼容旧名
ExternalLLMAdapterStub = OpenAICompatibleAdapter


class CascadingAdapter:
    """主模型失败时尝试 fallback，再失败由调用方走规则引擎。"""

    model_version = "cascade"

    def __init__(self, primary: ModelAdapter, fallback: ModelAdapter | None = None) -> None:
        self.primary = primary
        self.fallback = fallback
        self.model_version = (
            f"cascade:{getattr(primary, 'model_version', '?')}"
            + (f"+{getattr(fallback, 'model_version', '?')}" if fallback else "")
        )

    def complete_json(
        self,
        *,
        task: str,
        system: str,
        user: str,
        schema_name: str,
    ) -> dict[str, Any]:
        try:
            return self.primary.complete_json(
                task=task, system=system, user=user, schema_name=schema_name
            )
        except Exception as first:
            if not self.fallback:
                raise
            out = self.fallback.complete_json(
                task=task, system=system, user=user, schema_name=schema_name
            )
            meta = out.setdefault("_meta", {})
            if isinstance(meta, dict):
                meta["cascaded_from_error"] = str(first)[:200]
                meta["cascade"] = True
            return out


class DualRoutingAdapter:
    """双厂商：按 task 选首选，失败自动切另一家。"""

    model_version = "dual"

    def __init__(self, qwen: ModelAdapter, deepseek: ModelAdapter, *, default_primary: str = "qwen") -> None:
        self.by_provider = {
            "qwen": qwen,
            "dashscope": qwen,
            "qwen_max": qwen,
            "deepseek": deepseek,
            "deepseek_pro": deepseek,
        }
        self.default_primary = default_primary
        self.model_version = (
            f"dual:{getattr(qwen, 'model_version', 'qwen')}+{getattr(deepseek, 'model_version', 'deepseek')}"
        )

    def complete_json(
        self,
        *,
        task: str,
        system: str,
        user: str,
        schema_name: str,
    ) -> dict[str, Any]:
        preferred = provider_for_task(task) or self.default_primary
        first = self.by_provider.get(preferred) or self.by_provider["qwen"]
        second_name = "deepseek" if preferred in {"qwen", "dashscope", "qwen_max"} else "qwen"
        second = self.by_provider.get(second_name)
        try:
            out = first.complete_json(task=task, system=system, user=user, schema_name=schema_name)
            meta = out.setdefault("_meta", {})
            if isinstance(meta, dict):
                meta["route"] = preferred
                meta["dual"] = True
            return out
        except Exception as first_err:
            if not second:
                raise
            out = second.complete_json(task=task, system=system, user=user, schema_name=schema_name)
            meta = out.setdefault("_meta", {})
            if isinstance(meta, dict):
                meta["route"] = second_name
                meta["dual"] = True
                meta["cascaded_from_error"] = str(first_err)[:200]
            return out


def _build_provider_adapter(provider: str) -> OpenAICompatibleAdapter:
    dual = resolve_dual_routing()
    if dual["enabled"] and provider == dual.get("fallback_provider"):
        return OpenAICompatibleAdapter(
            provider=provider,
            api_key=(os.environ.get("COACH_LLM_FALLBACK_API_KEY") or None),
            base_url=(os.environ.get("COACH_LLM_FALLBACK_BASE_URL") or None),
            model=(os.environ.get("COACH_LLM_FALLBACK_MODEL") or None),
        )
    return OpenAICompatibleAdapter(provider=provider)


def get_model_adapter(*, task: str | None = None) -> ModelAdapter:
    status = llm_status()
    if status["force_rules"] or not status["ready"]:
        return RuleBasedAdapter()
    dual = resolve_dual_routing()
    if dual["enabled"]:
        qwen = _build_provider_adapter("qwen")
        deepseek = _build_provider_adapter("deepseek")
        # 若两边 key 都可用，走任务分流 + 互备
        if qwen.api_key and deepseek.api_key:
            return DualRoutingAdapter(qwen, deepseek, default_primary=dual["primary_provider"])
        # 仅一家可用时 cascade
        primary = _build_provider_adapter(dual["primary_provider"])
        fallback = _build_provider_adapter(dual["fallback_provider"])
        return CascadingAdapter(primary, fallback)

    primary = OpenAICompatibleAdapter()
    fb = status.get("fallback")
    if fb and fb.get("api_key_configured") and fb.get("base_url") and fb.get("model"):
        return CascadingAdapter(primary, OpenAICompatibleAdapter(use_fallback_env=True))
    return primary


def try_complete_json_or_none(
    *,
    task: str,
    system: str,
    user: str,
    schema_name: str,
) -> dict[str, Any] | None:
    """供 workflow 选用：LLM 不可用或失败时返回 None，由规则引擎接管。"""
    adapter = get_model_adapter(task=task)
    if isinstance(adapter, RuleBasedAdapter):
        return None
    try:
        return adapter.complete_json(
            task=task, system=system, user=user, schema_name=schema_name
        )
    except Exception:
        return None
