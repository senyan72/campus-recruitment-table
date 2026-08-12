"""OpenAI 兼容 Chat Completions 客户端（Qwen / DeepSeek / 通用）。"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

# 预置供应商（可用 COACH_LLM_BASE_URL / MODEL 覆盖）
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-max",
        "label": "通义千问（DashScope OpenAI 兼容）",
    },
    "dashscope": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-max",
        "label": "通义千问（DashScope）",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "label": "DeepSeek",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "label": "OpenAI",
    },
    "openai_compatible": {
        "base_url": "",
        "model": "",
        "label": "自定义 OpenAI 兼容端点",
    },
}

EVIDENCE_SYSTEM_PREFIX = """你是校招 AI 求职陪伴助手。必须遵守：
1. 只依据用户提供的「已确认事实 / 知识库片段」作判断；不得编造实习、项目、指标、证书。
2. 区分 fact（事实）、inference（推断）、assumption（假设）；不确定就写「信息不足」或 needs_proof=true。
3. 禁止输出录用概率、保证 Offer、能力人格总分。
4. 只输出合法 JSON，不要 Markdown 代码围栏，不要额外解释文字。
"""


def resolve_provider_config(
    *,
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> dict[str, str]:
    prov = (provider or os.environ.get("COACH_LLM_PROVIDER") or "").strip().lower()
    key = (api_key if api_key is not None else os.environ.get("COACH_LLM_API_KEY") or "").strip()
    preset = PROVIDER_PRESETS.get(prov, {})
    url = (base_url if base_url is not None else os.environ.get("COACH_LLM_BASE_URL") or "").strip()
    mdl = (model if model is not None else os.environ.get("COACH_LLM_MODEL") or "").strip()
    if not url:
        url = (preset.get("base_url") or "").strip()
    if not mdl:
        mdl = (preset.get("model") or "").strip()
    return {
        "provider": prov,
        "api_key": key,
        "base_url": url.rstrip("/"),
        "model": mdl,
        "label": preset.get("label") or prov or "unknown",
    }


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        raise ValueError("模型未返回可解析 JSON 对象")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("JSON 根节点必须是 object")
    return data


def chat_completions_json(
    *,
    system: str,
    user: str,
    schema_name: str,
    task: str,
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    timeout: float = 60.0,
    temperature: float = 0.2,
) -> dict[str, Any]:
    """调用 OpenAI 兼容 /chat/completions，要求返回 JSON 对象。"""
    cfg = resolve_provider_config(
        provider=provider, api_key=api_key, base_url=base_url, model=model
    )
    if not cfg["api_key"]:
        raise RuntimeError("缺少 COACH_LLM_API_KEY")
    if not cfg["base_url"] or not cfg["model"]:
        raise RuntimeError("缺少 COACH_LLM_BASE_URL 或 COACH_LLM_MODEL（或未识别的 PROVIDER 预置）")

    system_full = EVIDENCE_SYSTEM_PREFIX + "\n" + (system or "")
    user_full = (
        f"task={task}\nschema_name={schema_name}\n"
        f"请严格输出符合业务约定的 JSON 对象。\n\n{user}"
    )
    url = f"{cfg['base_url']}/chat/completions"
    payload: dict[str, Any] = {
        "model": cfg["model"],
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_full},
            {"role": "user", "content": user_full},
        ],
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        body = resp.json()
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f"LLM 响应结构异常: {body!r}") from e
    parsed = _extract_json_object(content if isinstance(content, str) else json.dumps(content))
    parsed.setdefault("_meta", {})
    if isinstance(parsed["_meta"], dict):
        parsed["_meta"].update(
            {
                "provider": cfg["provider"],
                "model": cfg["model"],
                "task": task,
                "schema_name": schema_name,
            }
        )
    return parsed


def llm_status() -> dict[str, Any]:
    force_rules = (os.environ.get("COACH_FORCE_RULES") or "1").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    primary = resolve_provider_config()
    fallback_provider = (os.environ.get("COACH_LLM_FALLBACK_PROVIDER") or "").strip().lower()
    fallback_key = (os.environ.get("COACH_LLM_FALLBACK_API_KEY") or primary["api_key"]).strip()
    fallback = None
    if fallback_provider:
        fallback = resolve_provider_config(
            provider=fallback_provider,
            api_key=fallback_key or None,
            base_url=(os.environ.get("COACH_LLM_FALLBACK_BASE_URL") or None),
            model=(os.environ.get("COACH_LLM_FALLBACK_MODEL") or None),
        )
    ready = bool(primary["api_key"] and primary["base_url"] and primary["model"] and not force_rules)
    return {
        "force_rules": force_rules,
        "ready": ready,
        "primary": {
            "provider": primary["provider"],
            "base_url": primary["base_url"],
            "model": primary["model"],
            "label": primary["label"],
            "api_key_configured": bool(primary["api_key"]),
        },
        "fallback": (
            {
                "provider": fallback["provider"],
                "base_url": fallback["base_url"],
                "model": fallback["model"],
                "label": fallback["label"],
                "api_key_configured": bool(fallback["api_key"]),
            }
            if fallback
            else None
        ),
        "supported_providers": sorted(PROVIDER_PRESETS.keys()),
    }
