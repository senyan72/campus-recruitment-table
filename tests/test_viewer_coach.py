"""Viewer 桌面 AI 陪伴：岗位转换与 workflow 主链（无 GUI）。"""

from __future__ import annotations

import os

import pytest

os.environ["COACH_FORCE_RULES"] = "1"

from app.coach.database import CoachDB
from app.coach.workflows import get_registry, reset_registry_for_tests
from app.ui.coach_viewer import job_payload_from_campus


@pytest.fixture()
def coach_db(tmp_path):
    reset_registry_for_tests()
    return CoachDB(tmp_path / "viewer_coach.db")


def test_job_payload_from_campus():
    payload = job_payload_from_campus(
        {
            "id": "j1",
            "title": "数据分析实习",
            "company": "示例公司",
            "work_location": "上海",
            "education": "本科",
            "jd_text": "负责数据分析与沟通协作",
            "apply_url": "https://example.com",
        }
    )
    assert payload["id"] == "j1"
    assert payload["city"] == "上海"
    assert "数据分析" in payload["description"]
    assert job_payload_from_campus(None) == {}


def test_viewer_companion_workflow_chain(coach_db):
    user = coach_db.ensure_user("viewer_demo")
    uid = user["id"]
    reg = get_registry()
    ctx = {"db": coach_db, "user": user}

    extracted = reg.run(
        "evidence.extract",
        {"user_id": uid, "text": "- 负责校园数据分析项目\n- 组织团队协作完成调研", "source": "resume"},
        context=ctx,
    )
    assert extracted["assets"]
    facts = coach_db.fetchall("SELECT id FROM facts WHERE user_id=? AND status='pending'", (uid,))
    for f in facts:
        coach_db.execute("UPDATE facts SET status='confirmed' WHERE id=?", (f["id"],))

    # fake resume version for suggest
    now = "2026-08-12T00:00:00Z"
    doc_id = coach_db.new_id("doc_")
    ver_id = coach_db.new_id("rv_")
    coach_db.execute(
        "INSERT INTO resume_documents(id, user_id, filename, parse_status, text_content, created_at) VALUES(?,?,?,?,?,?)",
        (doc_id, uid, "t.txt", "parsed", "数据分析", now),
    )
    coach_db.execute(
        "INSERT INTO resume_versions(id, document_id, user_id, parent_id, label, sections_json, created_at) VALUES(?,?,?,?,?,?,?)",
        (ver_id, doc_id, uid, None, "v1", coach_db.dumps({"全文": "数据分析项目"}), now),
    )

    sug = reg.run(
        "resume.suggest.bullets",
        {"user_id": uid, "version_id": ver_id, "job": {"title": "数据分析实习"}},
        context=ctx,
    )
    assert sug["suggestions"]

    match = reg.run(
        "match.explain",
        {
            "user_id": uid,
            "job": {
                "title": "数据分析实习生",
                "city": "上海",
                "description": "数据分析 沟通协作",
            },
        },
        context=ctx,
    )
    assert match["tier"] in {"优先投", "可以投", "补充后投", "暂不建议"}

    stories = reg.run("interview.storybank", {"user_id": uid}, context=ctx)
    assert stories.get("stories") is not None
