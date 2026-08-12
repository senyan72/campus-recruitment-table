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
        "model": "qwen-plus",
        "label": "通义千问（DashScope OpenAI 兼容）",
        "key_env": "DASHSCOPE_API_KEY",
    },
    "dashscope": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "label": "通义千问（DashScope）",
        "key_env": "DASHSCOPE_API_KEY",
    },
    "qwen_max": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-max",
        "label": "通义千问 Max",
        "key_env": "DASHSCOPE_API_KEY",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-flash",
        "label": "DeepSeek V4 Flash",
        "key_env": "DEEPSEEK_API_KEY",
    },
    "deepseek_pro": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-pro",
        "label": "DeepSeek V4 Pro",
        "key_env": "DEEPSEEK_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "label": "OpenAI",
        "key_env": "OPENAI_API_KEY",
    },
    "openai_compatible": {
        "base_url": "",
        "model": "",
        "label": "自定义 OpenAI 兼容端点",
        "key_env": "",
    },
}

# 产品推荐与计费参考（非实时拉取；以官网为准）
PROVIDER_GUIDE: dict[str, Any] = {
    "qwen": {
        "console": "https://bailian.console.aliyun.com/",
        "docs": "https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope",
        "pricing_docs": "https://help.aliyun.com/zh/model-studio/model-pricing",
        "recommended_models": ["qwen-plus", "qwen-max", "qwen3.7-plus"],
        "default_for": "中文自然表达 + 校招陪伴主路径",
        "billing_note": "按 Token 计费（人民币）；新用户常有免费额度，以控制台为准",
        "approx_price_cny_per_1m_tokens": {
            "qwen-plus": {"input": 0.8, "output": 2.0, "unit": "元/百万tokens", "mode": "≤128K 非思考"},
            "qwen-max": {"input": 2.4, "output": 9.6, "unit": "元/百万tokens", "mode": "非思考"},
        },
    },
    "deepseek": {
        "console": "https://platform.deepseek.com/",
        "docs": "https://api-docs.deepseek.com/",
        "pricing_docs": "https://api-docs.deepseek.com/quick_start/pricing",
        "recommended_models": ["deepseek-v4-flash", "deepseek-v4-pro"],
        "default_for": "证据链推理 / 结构化 JSON / 成本敏感回退",
        "billing_note": "按 Token 计费（USD）；旧名 deepseek-chat 已退役，请用 v4-flash/pro",
        "approx_price_usd_per_1m_tokens": {
            "deepseek-v4-flash": {
                "input_cache_miss": 0.14,
                "input_cache_hit": 0.0028,
                "output": 0.28,
                "unit": "USD/百万tokens",
            },
            "deepseek-v4-pro": {
                "input_cache_miss": 0.435,
                "input_cache_hit": 0.003625,
                "output": 0.87,
                "unit": "USD/百万tokens",
            },
        },
    },
}

EVIDENCE_SYSTEM_PREFIX = """你是校招 AI 求职陪伴助手。必须遵守：
1. 只依据用户提供的「已确认事实 / 知识库片段」作判断；不得编造实习、项目、指标、证书。
2. 区分 fact（事实）、inference（推断）、assumption（假设）；不确定就写「信息不足」或 needs_proof=true。
3. 禁止输出录用概率、保证 Offer、能力人格总分。
4. 只输出合法 JSON，不要 Markdown 代码围栏，不要额外解释文字。
"""


def _resolve_api_key(*, provider: str, api_key: str | None, preset: dict[str, str]) -> str:
    if api_key is not None and str(api_key).strip():
        return str(api_key).strip()
    primary = (os.environ.get("COACH_LLM_API_KEY") or "").strip()
    if primary:
        return primary
    key_env = (preset.get("key_env") or "").strip()
    if key_env:
        return (os.environ.get(key_env) or "").strip()
    # 常见别名兜底
    if provider in {"qwen", "dashscope", "qwen_max"}:
        return (os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    if provider in {"deepseek", "deepseek_pro"}:
        return (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    return ""


def resolve_provider_config(
    *,
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> dict[str, str]:
    prov = (provider or os.environ.get("COACH_LLM_PROVIDER") or "").strip().lower()
    # 兼容别名
    aliases = {
        "通义": "qwen",
        "千问": "qwen",
        "tongyi": "qwen",
        "aliyun": "qwen",
        "bailian": "qwen",
        "ds": "deepseek",
        "deepseek-chat": "deepseek",
        "deepseek-reasoner": "deepseek_pro",
    }
    prov = aliases.get(prov, prov)
    preset = PROVIDER_PRESETS.get(prov, {})
    key = _resolve_api_key(provider=prov, api_key=api_key, preset=preset)
    url = (base_url if base_url is not None else os.environ.get("COACH_LLM_BASE_URL") or "").strip()
    mdl = (model if model is not None else os.environ.get("COACH_LLM_MODEL") or "").strip()
    if not url:
        url = (preset.get("base_url") or "").strip()
    if not mdl:
        mdl = (preset.get("model") or "").strip()
    # 旧模型名自动迁移
    if mdl in {"deepseek-chat", "deepseek-reasoner"}:
        mdl = "deepseek-v4-flash" if mdl == "deepseek-chat" else "deepseek-v4-pro"
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
        raise RuntimeError(
            "缺少 API Key：请设置 COACH_LLM_API_KEY，或 DASHSCOPE_API_KEY / DEEPSEEK_API_KEY"
        )
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
        if resp.status_code >= 400:
            detail = resp.text[:500]
            raise RuntimeError(f"LLM HTTP {resp.status_code}: {detail}")
        body = resp.json()
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f"LLM 响应结构异常: {body!r}") from e
    parsed = _extract_json_object(content if isinstance(content, str) else json.dumps(content))
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    parsed.setdefault("_meta", {})
    if isinstance(parsed["_meta"], dict):
        parsed["_meta"].update(
            {
                "provider": cfg["provider"],
                "model": cfg["model"],
                "task": task,
                "schema_name": schema_name,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
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
    fallback_key = (os.environ.get("COACH_LLM_FALLBACK_API_KEY") or "").strip() or None
    fallback = None
    if fallback_provider:
        fallback = resolve_provider_config(
            provider=fallback_provider,
            api_key=fallback_key,
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
        "guides": PROVIDER_GUIDE,
        "setup_hint": (
            "设置 COACH_FORCE_RULES=0，并配置 COACH_LLM_PROVIDER=qwen|deepseek "
            "与 COACH_LLM_API_KEY（或 DASHSCOPE_API_KEY / DEEPSEEK_API_KEY）"
        ),
    }
