"""文档知识库、护栏校验、岗位 API、LLM 调用日志测试。"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ["COACH_FORCE_RULES"] = "1"

from app.coach.api import create_coach_app
from app.coach.guardrails import validate_llm_payload
from app.coach.knowledge.documents import chunk_text_by_headings, ingest_document, search_document_chunks
from app.coach.database import CoachDB
from app.coach.workflows import reset_registry_for_tests


@pytest.fixture()
def coach_db(tmp_path):
    return CoachDB(tmp_path / "coach.db")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COACH_FORCE_RULES", "1")
    monkeypatch.setenv("COACH_KNOWLEDGE_DIR", str(tmp_path / "kn"))
    monkeypatch.delenv("COACH_ADMIN_TOKEN", raising=False)
    reset_registry_for_tests()
    app = create_coach_app(db_path=str(tmp_path / "coach.db"))
    return TestClient(app)


def _login(client: TestClient) -> dict[str, str]:
    r = client.post("/v1/auth/dev-login", json={"account": "doc_tester"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_chunk_text_by_headings():
    text = "# 标题一\n内容A\n\n## 子标题\n内容B"
    chunks = chunk_text_by_headings(text)
    assert len(chunks) >= 2
    assert chunks[0]["heading"] == "标题一"


def test_ingest_and_search_document(coach_db):
    content = "# 校招流程\n先确认硬条件。\n\n## 笔试\n准备行测与专业题。".encode("utf-8")
    result = ingest_document(
        coach_db,
        filename="tips.md",
        content=content,
        owner_id="usr_test",
    )
    assert result["chunk_count"] >= 1
    hits = search_document_chunks(coach_db, query="硬条件", owner_id="usr_test")
    assert hits
    assert "硬条件" in hits[0]["text"]


def test_document_upload_api(client):
    h = _login(client)
    files = {"file": ("guide.md", "# 面试\n使用 STAR 结构回答行为题。".encode("utf-8"), "text/markdown")}
    r = client.post("/v1/knowledge/documents/upload", headers=h, files=files)
    assert r.status_code == 200, r.text
    assert r.json()["chunk_count"] >= 1

    listed = client.get("/v1/knowledge/documents", headers=h)
    assert listed.status_code == 200
    assert listed.json()["documents"]

    search = client.get("/v1/knowledge/documents/search", headers=h, params={"q": "STAR"})
    assert search.status_code == 200
    assert search.json()["hits"]


def test_knowledge_context_includes_documents(client):
    h = _login(client)
    files = {"file": ("hr.md", "# 人事\n不要编造实习经历。".encode("utf-8"), "text/markdown")}
    client.post("/v1/knowledge/documents/upload", headers=h, files=files)
    ctx = client.get("/v1/knowledge/context", headers=h, params={"q": "编造"})
    assert ctx.status_code == 200
    body = ctx.json()
    assert "document_hits" in body
    assert body["document_hits"]


def test_validate_llm_payload_resume_rejects_hire_prob():
    with pytest.raises(ValueError, match="录用"):
        validate_llm_payload(
            {"suggestions": [{"suggestion": "录用概率80%", "fact_ids": []}]},
            validator="resume_suggestions",
            confirmed_fact_ids=set(),
        )


def test_validate_llm_payload_resume_needs_proof_on_metrics():
    validate_llm_payload(
        {
            "suggestions": [
                {
                    "suggestion": "提升了50%效率",
                    "fact_ids": [],
                    "reason": "量化",
                }
            ]
        },
        validator="resume_suggestions",
        confirmed_fact_ids=set(),
    )


def test_validate_llm_payload_match_invalid_tier():
    with pytest.raises(ValueError, match="tier"):
        validate_llm_payload(
            {"tier": "必过"},
            validator="match_explain",
            confirmed_fact_ids=set(),
        )


def test_workflow_engine_field_rules(client):
    h = _login(client)
    up = client.post(
        "/v1/resumes/upload-text",
        headers=h,
        json={"text": "- 负责数据分析项目并完成报告\n- 组织团队协作"},
    )
    version_id = up.json()["version_id"]
    facts = client.get("/v1/facts", headers=h).json()["facts"]
    pending = [f["id"] for f in facts if f["status"] == "pending"]
    client.post("/v1/facts/confirm", headers=h, json={"fact_ids": pending})
    sug = client.post(
        "/v1/workflows/resume.suggest/run",
        headers=h,
        json={"payload": {"version_id": version_id, "job": {"title": "数据分析"}}},
    )
    assert sug.json()["result"]["engine"] == "rules"


def test_workflow_engine_llm_when_mocked(client, monkeypatch):
    monkeypatch.setenv("COACH_FORCE_RULES", "0")
    monkeypatch.setenv("COACH_LLM_API_KEY", "test-key")
    h = _login(client)
    up = client.post(
        "/v1/resumes/upload-text",
        headers=h,
        json={"text": "- 负责校园数据分析项目"},
    )
    version_id = up.json()["version_id"]
    mock_payload = {
        "suggestions": [
            {
                "id": "s1",
                "section": "经历",
                "suggestion": "突出数据分析职责",
                "reason": "对齐岗位",
                "fact_ids": [],
                "needs_proof": True,
                "scenario": "general",
            }
        ]
    }
    with patch("app.coach.ai.llm_invoke.try_complete_json_or_none", return_value=mock_payload):
        sug = client.post(
            "/v1/workflows/resume.suggest/run",
            headers=h,
            json={"payload": {"version_id": version_id, "job": {"title": "数据分析"}}},
        )
    assert sug.status_code == 200
    assert sug.json()["result"]["engine"] == "llm"


def test_llm_call_log_written(client, monkeypatch, tmp_path):
    monkeypatch.setenv("COACH_FORCE_RULES", "0")
    monkeypatch.setenv("COACH_LLM_API_KEY", "test-key")
    db = CoachDB(tmp_path / "log.db")
    reset_registry_for_tests()
    app = create_coach_app(db_path=str(tmp_path / "log.db"))
    c = TestClient(app)
    h = _login(c)
    up = c.post(
        "/v1/resumes/upload-text",
        headers=h,
        json={"text": "- 负责校园数据分析项目并完成报告交付\n- 组织团队协作完成用户调研"},
    )
    assert up.status_code == 200, up.text
    version_id = up.json()["version_id"]
    with patch(
        "app.coach.ai.llm_invoke.try_complete_json_or_none",
        return_value={
            "suggestions": [
                {
                    "id": "s1",
                    "suggestion": "建议",
                    "fact_ids": [],
                    "needs_proof": True,
                }
            ]
        },
    ):
        c.post(
            "/v1/workflows/resume.suggest/run",
            headers=h,
            json={"payload": {"version_id": version_id}},
        )
    logs = db.fetchall("SELECT * FROM llm_call_logs")
    assert logs
    assert logs[0]["task"] == "resume.suggest.general"
