"""LLM 外接与知识库 API 测试。"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ["COACH_FORCE_RULES"] = "1"

from app.coach.ai.llm_client import _extract_json_object, resolve_provider_config
from app.coach.api import create_coach_app
from app.coach.knowledge import search_knowledge, upsert_hr_basic, upsert_interview_track
from app.coach.workflows import reset_registry_for_tests


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COACH_FORCE_RULES", "1")
    monkeypatch.setenv("COACH_KNOWLEDGE_DIR", str(tmp_path / "kn"))
    monkeypatch.delenv("COACH_ADMIN_TOKEN", raising=False)
    reset_registry_for_tests()
    app = create_coach_app(db_path=str(tmp_path / "coach.db"))
    return TestClient(app)


def _login(client: TestClient) -> dict[str, str]:
    r = client.post("/v1/auth/dev-login", json={"account": "llm_tester"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_resolve_qwen_deepseek_presets():
    q = resolve_provider_config(provider="qwen", api_key="k")
    assert "dashscope" in q["base_url"]
    assert q["model"] == "qwen-plus"
    d = resolve_provider_config(provider="deepseek", api_key="k")
    assert "deepseek" in d["base_url"]
    assert d["model"] == "deepseek-v4-flash"
    # 旧模型名自动迁移
    legacy = resolve_provider_config(provider="deepseek", api_key="k", model="deepseek-chat")
    assert legacy["model"] == "deepseek-v4-flash"


def test_vendor_key_env_fallback(monkeypatch):
    monkeypatch.delenv("COACH_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dash")
    q = resolve_provider_config(provider="qwen")
    assert q["api_key"] == "sk-dash"
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
    d = resolve_provider_config(provider="deepseek")
    assert d["api_key"] == "sk-ds"


def test_dual_mode_auto_when_both_keys(monkeypatch):
    from app.coach.ai.llm_client import provider_for_task, resolve_dual_routing
    from app.coach.ai import DualRoutingAdapter, get_model_adapter

    monkeypatch.setenv("COACH_FORCE_RULES", "0")
    monkeypatch.setenv("COACH_LLM_MODE", "dual")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen-test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-test")
    monkeypatch.delenv("COACH_LLM_API_KEY", raising=False)
    dual = resolve_dual_routing()
    assert dual["enabled"] is True
    assert provider_for_task("resume.suggest.bullets") == "qwen"
    assert provider_for_task("match.explain") == "deepseek"
    adapter = get_model_adapter(task="match.explain")
    assert isinstance(adapter, DualRoutingAdapter)


def test_llm_ping_endpoint_reports_missing_keys(client, monkeypatch):
    monkeypatch.setenv("COACH_FORCE_RULES", "0")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("COACH_LLM_API_KEY", raising=False)
    h = _login(client)
    r = client.post("/v1/llm/ping", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert "results" in body
    # 无 key 时 ready 为 false，results 可能为空或均失败
    assert body["all_ok"] is False or body["ready"] is False


def test_extract_json_object():
    assert _extract_json_object('{"a":1}')["a"] == 1
    assert _extract_json_object('```json\n{"a":2}\n```')["a"] == 2


def test_llm_status_endpoint(client):
    r = client.get("/v1/llm/status")
    assert r.status_code == 200
    body = r.json()
    assert "primary" in body
    assert "qwen" in body["supported_providers"]
    assert "deepseek" in body["supported_providers"]
    assert body["force_rules"] is True
    assert "guides" in body
    assert "qwen" in body["guides"]
    assert "deepseek" in body["guides"]


def test_llm_complete_json_falls_back_without_key(client):
    h = _login(client)
    r = client.post(
        "/v1/llm/complete-json",
        headers=h,
        json={"task": "ping", "user": "只返回 {\"ok\":true}", "include_knowledge": True},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["fallback"] == "rules"


def test_llm_complete_json_with_mock(client, monkeypatch):
    monkeypatch.setenv("COACH_FORCE_RULES", "0")
    monkeypatch.setenv("COACH_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("COACH_LLM_API_KEY", "test-key")
    h = _login(client)
    with patch(
        "app.coach.ai.try_complete_json_or_none",
        return_value={"ok": True, "note": "mocked"},
    ):
        # patch path used inside api module
        pass
    with patch(
        "app.coach.api.try_complete_json_or_none",
        return_value={"ok": True, "note": "mocked"},
    ):
        r = client.post(
            "/v1/llm/complete-json",
            headers=h,
            json={"task": "ping", "user": "hi", "schema_name": "x"},
        )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["result"]["note"] == "mocked"


def test_knowledge_pack_apis(client):
    assert client.get("/v1/knowledge/packs").status_code == 200
    packs = client.get("/v1/knowledge/packs").json()
    assert "builtin" in packs and "user" in packs

    put = client.put(
        "/v1/knowledge/interview/campus_general",
        json={
            "track": "campus_general",
            "merge": True,
            "questions": [
                {
                    "id": "custom_1",
                    "track": "campus_general",
                    "stage": "behavioral",
                    "question": "请讲一次你主导推进的事情",
                    "intent": "主动性",
                    "followups": [],
                    "good_signals": [],
                    "risk_flags": [],
                    "source": "user",
                }
            ],
        },
    )
    assert put.status_code == 200, put.text
    assert put.json()["count"] >= 1

    hr = client.put(
        "/v1/knowledge/hr/campus_tips",
        json={"topic": "campus_tips", "content": "# 提示\n先确认硬条件再投递。"},
    )
    assert hr.status_code == 200

    meta = client.put("/v1/knowledge/meta", json={"meta": {"owner": "pilot"}})
    assert meta.status_code == 200

    search = client.get("/v1/knowledge/search", params={"q": "主导"})
    assert search.status_code == 200
    assert search.json()["hits"]

    ctx = client.get("/v1/knowledge/context", params={"track": "campus_general", "q": "硬条件"})
    assert ctx.status_code == 200
    assert "questions" in ctx.json()


def test_knowledge_write_requires_admin_when_set(client, monkeypatch):
    monkeypatch.setenv("COACH_ADMIN_TOKEN", "secret")
    r = client.put(
        "/v1/knowledge/hr/locked",
        json={"topic": "locked", "content": "nope"},
    )
    assert r.status_code == 403
    r2 = client.put(
        "/v1/knowledge/hr/locked",
        headers={"X-Coach-Admin-Token": "secret"},
        json={"topic": "locked", "content": "ok"},
    )
    assert r2.status_code == 200


def test_direct_knowledge_helpers(tmp_path, monkeypatch):
    monkeypatch.setenv("COACH_KNOWLEDGE_DIR", str(tmp_path / "k2"))
    upsert_interview_track(
        track="product",
        questions=[{"id": "p1", "question": "如何做需求优先级？", "intent": "产品感"}],
        merge=False,
    )
    upsert_hr_basic(topic="myths", content="不要编造数据")
    hits = search_knowledge(query="优先级")
    assert hits
