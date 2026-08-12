"""AI 陪伴：范围、workflow、护栏、知识包。"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ["COACH_FORCE_RULES"] = "1"

from app.coach.api import create_coach_app
from app.coach.guardrails import assert_suggestions_safe, filter_approved_only
from app.coach.knowledge import load_interview_questions, load_hr_basics
from app.coach.scope import HUMAN_COACH_OUT_OF_SCOPE, assert_not_human_coach
from app.coach.workflows import get_registry, reset_registry_for_tests


@pytest.fixture()
def client(tmp_path):
    reset_registry_for_tests()
    app = create_coach_app(db_path=str(tmp_path / "coach.db"))
    return TestClient(app)


def _login(client: TestClient) -> str:
    r = client.post("/v1/auth/dev-login", json={"account": "tester"})
    assert r.status_code == 200
    return r.json()["token"]


def test_scope_blocks_human_coach():
    with pytest.raises(PermissionError):
        assert_not_human_coach("human_coach_request")
    assert "coach_marketplace" in HUMAN_COACH_OUT_OF_SCOPE


def test_health_scope(client):
    r = client.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["scope"] == "ai_companion_only"
    assert "human_coach_request" in body["human_coach_out_of_scope"]
    assert "postgresql_primary" in body["deferred_infra"]


def test_workflows_registered(client):
    names = {w["name"] for w in client.get("/v1/workflows").json()["workflows"]}
    for n in (
        "evidence.extract",
        "strengths.mine",
        "resume.suggest.bullets",
        "resume.suggest.quantify",
        "resume.suggest.keywords",
        "match.explain",
        "interview.storybank",
        "interview.feedback",
        "interview.mock.start",
        "interview.mock.answer",
    ):
        assert n in names


def test_knowledge_builtin_pack():
    qs = load_interview_questions(track="campus_general", limit=3)
    assert len(qs) >= 1
    assert qs[0]["question"]
    hr = load_hr_basics()
    assert hr


def test_companion_path_evidence_resume_match_interview(client):
    token = _login(client)
    h = {"Authorization": f"Bearer {token}"}

    up = client.post(
        "/v1/resumes/upload-text",
        headers=h,
        json={"text": "- 负责校园数据分析项目并完成报告交付\n- 组织团队协作完成用户调研"},
    )
    assert up.status_code == 200, up.text
    version_id = up.json()["version_id"]

    facts = client.get("/v1/facts", headers=h).json()["facts"]
    pending = [f["id"] for f in facts if f["status"] == "pending"]
    conf = client.post("/v1/facts/confirm", headers=h, json={"fact_ids": pending})
    assert conf.status_code == 200
    assert conf.json()["facts"]

    st = client.post("/v1/workflows/strengths.mine/run", headers=h, json={"payload": {}})
    assert st.status_code == 200
    assert st.json()["result"]["strengths"]

    sug = client.post(
        "/v1/workflows/resume.suggest.bullets/run",
        headers=h,
        json={"payload": {"version_id": version_id, "job": {"title": "数据分析实习"}}},
    )
    assert sug.status_code == 200
    suggestions = sug.json()["result"]["suggestions"]
    assert suggestions
    assert "needs_proof" in suggestions[0]

    match = client.post(
        "/v1/workflows/match.explain/run",
        headers=h,
        json={
            "payload": {
                "job": {
                    "title": "数据分析实习生",
                    "city": "上海",
                    "description": "数据分析 沟通协作 项目交付",
                }
            }
        },
    )
    assert match.status_code == 200
    mr = match.json()["result"]
    assert mr["tier"] in {"优先投", "可以投", "补充后投", "暂不建议"}
    assert "requirement_evidence_table" in mr

    sb = client.post("/v1/workflows/interview.storybank/run", headers=h, json={"payload": {}})
    assert sb.status_code == 200
    assert sb.json()["result"]["stories"]

    start = client.post("/v1/workflows/interview.mock.start/run", headers=h, json={"payload": {}})
    assert start.status_code == 200
    sid = start.json()["result"]["session_id"]
    ans = client.post(
        "/v1/workflows/interview.mock.answer/run",
        headers=h,
        json={
            "payload": {
                "session_id": sid,
                "answer": "我在实习中负责数据分析项目，完成了报告交付，并与团队协作推进用户调研。",
            }
        },
    )
    assert ans.status_code == 200
    fb = ans.json()["result"]["feedback"]
    assert "what_heard" in fb and "gaps" in fb
    assert "录用概率" not in str(fb)


def test_guardrails_needs_proof_and_filter():
    cleaned = assert_suggestions_safe(
        [{"id": "1", "suggestion": "提升了50%效率", "fact_ids": [], "reason": "x"}],
        confirmed_fact_ids=set(),
    )
    assert cleaned[0]["needs_proof"] is True
    approved = filter_approved_only(
        [
            {"id": "1", "status": "approved", "needs_proof": True, "suggestion": "x"},
            {"id": "2", "status": "approved", "needs_proof": False, "suggestion": "y"},
            {"id": "3", "status": "proposed", "needs_proof": False, "suggestion": "z"},
        ]
    )
    assert [a["id"] for a in approved] == ["2"]


def test_human_coach_workflow_blocked(client):
    token = _login(client)
    r = client.post(
        "/v1/workflows/human_coach_request/run",
        headers={"Authorization": f"Bearer {token}"},
        json={"payload": {}},
    )
    assert r.status_code in {403, 404}
