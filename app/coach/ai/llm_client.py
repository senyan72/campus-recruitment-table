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


def _normalize_provider(name: str) -> str:
    prov = (name or "").strip().lower()
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
    return aliases.get(prov, prov)


def _vendor_key_for(provider: str) -> str:
    if provider in {"qwen", "dashscope", "qwen_max"}:
        return (os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    if provider in {"deepseek", "deepseek_pro"}:
        return (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if provider == "openai":
        return (os.environ.get("OPENAI_API_KEY") or "").strip()
    return ""


def _resolve_api_key(*, provider: str, api_key: str | None, preset: dict[str, str]) -> str:
    """解析 Key：显式参数 > 厂商专用变量 > COACH_LLM_API_KEY（仅当 provider 匹配主 provider）。"""
    if api_key is not None and str(api_key).strip():
        return str(api_key).strip()
    vendor = _vendor_key_for(provider)
    if vendor:
        return vendor
    key_env = (preset.get("key_env") or "").strip()
    if key_env:
        v = (os.environ.get(key_env) or "").strip()
        if v:
            return v
    # 通用 Key：仅当未设厂商专用、且当前就是主 provider（或未指定主 provider）时使用
    shared = (os.environ.get("COACH_LLM_API_KEY") or "").strip()
    if not shared:
        return ""
    main_prov = _normalize_provider(os.environ.get("COACH_LLM_PROVIDER") or "")
    if not main_prov or main_prov == provider:
        return shared
    return ""


def resolve_provider_config(
    *,
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> dict[str, str]:
    dual = resolve_dual_routing()
    default_prov = dual["primary_provider"] if dual["enabled"] else (os.environ.get("COACH_LLM_PROVIDER") or "")
    prov = _normalize_provider(provider or default_prov)
    preset = PROVIDER_PRESETS.get(prov, {})
    key = _resolve_api_key(provider=prov, api_key=api_key, preset=preset)
    url = (base_url if base_url is not None else os.environ.get("COACH_LLM_BASE_URL") or "").strip()
    mdl = (model if model is not None else os.environ.get("COACH_LLM_MODEL") or "").strip()
    # dual 模式下，备用链路不要吃主链路的 MODEL/BASE_URL
    if dual["enabled"] and provider and _normalize_provider(provider) == dual["fallback_provider"]:
        url = (base_url or os.environ.get("COACH_LLM_FALLBACK_BASE_URL") or "").strip()
        mdl = (model or os.environ.get("COACH_LLM_FALLBACK_MODEL") or "").strip()
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


# 默认任务分流：中文表达类 → Qwen；结构化推理类 → DeepSeek
DEFAULT_TASK_ROUTES: dict[str, str] = {
    "resume.suggest": "qwen",
    "resume.suggest.general": "qwen",
    "resume.suggest.bullets": "qwen",
    "resume.suggest.quantify": "qwen",
    "resume.suggest.keywords": "qwen",
    "interview.storybank": "qwen",
    "interview.feedback": "qwen",
    "interview.mock": "qwen",
    "match.explain": "deepseek",
    "readiness.assess": "deepseek",
    "readiness.revise": "deepseek",
}


def _parse_task_routes() -> dict[str, str]:
    raw = (os.environ.get("COACH_LLM_TASK_ROUTES") or "").strip()
    routes = dict(DEFAULT_TASK_ROUTES)
    if not raw:
        return routes
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(k, str) and isinstance(v, str) and v.strip():
                    routes[k] = _normalize_provider(v)
    except json.JSONDecodeError:
        pass
    return routes


def resolve_dual_routing() -> dict[str, Any]:
    """双厂商一起调用：COACH_LLM_MODE=dual，或同时存在两家 Key 时自动启用。"""
    mode = (os.environ.get("COACH_LLM_MODE") or "").strip().lower()
    dash = (os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    ds = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    shared = (os.environ.get("COACH_LLM_API_KEY") or "").strip()
    fb_key = (os.environ.get("COACH_LLM_FALLBACK_API_KEY") or "").strip()
    explicit_fb = _normalize_provider(os.environ.get("COACH_LLM_FALLBACK_PROVIDER") or "")
    env_primary = _normalize_provider(os.environ.get("COACH_LLM_PROVIDER") or "")

    has_qwen = bool(dash or (shared and env_primary in {"", "qwen", "dashscope", "qwen_max"}))
    has_deepseek = bool(ds or (explicit_fb in {"deepseek", "deepseek_pro"} and (fb_key or ds)))
    if fb_key and explicit_fb in {"deepseek", "deepseek_pro"}:
        has_deepseek = True
    if shared and env_primary in {"deepseek", "deepseek_pro"}:
        has_deepseek = True
        if not dash:
            has_qwen = bool(dash)

    auto_dual = bool(dash and ds)
    want_dual = mode in {"dual", "both", "cascade", "qwen+deepseek", "deepseek+qwen"} or (
        mode in {"", "auto"} and auto_dual
    )
    if mode == "single":
        want_dual = False
    if explicit_fb and (has_qwen or has_deepseek) and mode != "single":
        # 显式配置了 fallback 也视为 dual
        other = explicit_fb
        if other and (dash or ds or shared or fb_key):
            want_dual = want_dual or (bool(dash) and (bool(ds) or bool(fb_key) or other.startswith("deepseek")))

    if mode == "deepseek+qwen":
        primary, fallback = "deepseek", "qwen"
    else:
        primary = env_primary or "qwen"
        fallback = explicit_fb or ("deepseek" if primary in {"qwen", "dashscope", "qwen_max"} else "qwen")

    if primary == fallback:
        fallback = "deepseek" if primary in {"qwen", "dashscope", "qwen_max"} else "qwen"

    primary_key = _resolve_api_key(provider=primary, api_key=None, preset=PROVIDER_PRESETS.get(primary, {}))
    fallback_key = fb_key or _resolve_api_key(
        provider=fallback, api_key=None, preset=PROVIDER_PRESETS.get(fallback, {})
    )
    enabled = bool(want_dual and primary_key and fallback_key and primary != fallback)

    return {
        "enabled": enabled,
        "mode": "dual" if enabled else (mode or "single"),
        "primary_provider": primary,
        "fallback_provider": fallback if enabled else "",
        "task_routes": _parse_task_routes() if enabled else {},
        "has_qwen_key": bool(dash or (shared and primary in {"qwen", "dashscope", "qwen_max"})),
        "has_deepseek_key": bool(ds or (fallback_key and fallback.startswith("deepseek"))),
    }


def provider_for_task(task: str) -> str | None:
    """dual 模式下按任务选首选厂商；未启用则 None。"""
    dual = resolve_dual_routing()
    if not dual["enabled"]:
        return None
    routes: dict[str, str] = dual["task_routes"]
    t = (task or "").strip()
    if t in routes:
        return routes[t]
    # 前缀匹配
    for prefix, prov in routes.items():
        if t.startswith(prefix.rstrip(".*")):
            return prov
    if t.startswith("resume.") or t.startswith("interview."):
        return dual["primary_provider"]
    if t.startswith("match.") or t.startswith("readiness."):
        return dual["fallback_provider"]
    return dual["primary_provider"]


def ping_provider(provider: str, *, timeout: float = 20.0) -> dict[str, Any]:
    """对单个厂商做最小连通性探测（返回 ok/error，不含密钥）。"""
    cfg = resolve_provider_config(provider=provider)
    if not cfg["api_key"]:
        return {"provider": provider, "ok": False, "error": "missing_api_key", "model": cfg.get("model")}
    try:
        out = chat_completions_json(
            system="只返回 JSON。",
            user='返回 {"ok":true,"provider":"' + provider + '"}',
            schema_name="ping",
            task="ping",
            provider=cfg["provider"],
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            model=cfg["model"],
            timeout=timeout,
            temperature=0,
        )
        return {
            "provider": cfg["provider"],
            "ok": True,
            "model": cfg["model"],
            "label": cfg["label"],
            "sample": {k: out.get(k) for k in ("ok", "provider") if k in out},
            "usage": {
                "prompt_tokens": (out.get("_meta") or {}).get("prompt_tokens"),
                "completion_tokens": (out.get("_meta") or {}).get("completion_tokens"),
            },
        }
    except Exception as e:
        return {
            "provider": cfg["provider"],
            "ok": False,
            "model": cfg["model"],
            "label": cfg["label"],
            "error": str(e)[:300],
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
    dual = resolve_dual_routing()
    primary = resolve_provider_config(provider=dual["primary_provider"] if dual["enabled"] else None)
    fallback = None
    if dual["enabled"] and dual["fallback_provider"]:
        fb_key = (os.environ.get("COACH_LLM_FALLBACK_API_KEY") or "").strip() or None
        fallback = resolve_provider_config(
            provider=dual["fallback_provider"],
            api_key=fb_key,
            base_url=(os.environ.get("COACH_LLM_FALLBACK_BASE_URL") or None),
            model=(os.environ.get("COACH_LLM_FALLBACK_MODEL") or None),
        )
    elif (os.environ.get("COACH_LLM_FALLBACK_PROVIDER") or "").strip():
        fb_key = (os.environ.get("COACH_LLM_FALLBACK_API_KEY") or "").strip() or None
        fallback = resolve_provider_config(
            provider=os.environ.get("COACH_LLM_FALLBACK_PROVIDER"),
            api_key=fb_key,
            base_url=(os.environ.get("COACH_LLM_FALLBACK_BASE_URL") or None),
            model=(os.environ.get("COACH_LLM_FALLBACK_MODEL") or None),
        )
    ready = bool(primary["api_key"] and primary["base_url"] and primary["model"] and not force_rules)
    return {
        "force_rules": force_rules,
        "ready": ready,
        "dual": {
            "enabled": dual["enabled"],
            "mode": dual["mode"],
            "task_routes": dual.get("task_routes") or {},
            "has_qwen_key": dual.get("has_qwen_key"),
            "has_deepseek_key": dual.get("has_deepseek_key"),
        },
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
            if fallback and fallback.get("provider")
            else None
        ),
        "supported_providers": sorted(PROVIDER_PRESETS.keys()),
        "guides": PROVIDER_GUIDE,
        "setup_hint": (
            "双厂商：在 .env 填写 DASHSCOPE_API_KEY 与 DEEPSEEK_API_KEY，"
            "并设 COACH_FORCE_RULES=0、COACH_LLM_MODE=dual；然后 GET /v1/llm/ping 检测"
        ),
    }
