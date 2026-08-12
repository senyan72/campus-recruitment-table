"""AI 陪伴 FastAPI：/v1 + /coach H5。不含人工 coach。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.coach.database import CoachDB
from app.coach.knowledge import knowledge_context_for_interview, load_hr_basics
from app.coach.scope import (
    AI_COMPANION_CAPABILITIES,
    DEFERRED_INFRA,
    HUMAN_COACH_OUT_OF_SCOPE,
    assert_not_human_coach,
)
from app.coach.workflows import get_registry
from app.timeutil import utc_now_iso

STATIC_DIR = Path(__file__).resolve().parent / "static"


class LoginBody(BaseModel):
    account: str = Field(min_length=1, max_length=64)


class ConfirmFactsBody(BaseModel):
    fact_ids: list[str]


class WorkflowRunBody(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


def create_coach_app(*, db_path: str | None = None) -> FastAPI:
    app = FastAPI(title="AI Job Companion", version="1.0.0")
    db = CoachDB(db_path)

    def get_db() -> CoachDB:
        return db

    def current_user(
        authorization: str | None = Header(default=None),
        db: CoachDB = Depends(get_db),
    ) -> dict[str, Any]:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "需要登录")
        token = authorization.split(" ", 1)[1].strip()
        row = db.fetchone("SELECT * FROM sessions WHERE token=?", (token,))
        if not row:
            raise HTTPException(401, "登录已失效")
        user = db.fetchone("SELECT * FROM coach_users WHERE id=?", (row["user_id"],))
        if not user:
            raise HTTPException(401, "用户不存在")
        return user

    @app.get("/v1/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "scope": "ai_companion_only",
            "capabilities": list(AI_COMPANION_CAPABILITIES),
            "human_coach_out_of_scope": list(HUMAN_COACH_OUT_OF_SCOPE),
            "deferred_infra": list(DEFERRED_INFRA),
        }

    @app.post("/v1/auth/dev-login")
    def dev_login(body: LoginBody, db: CoachDB = Depends(get_db)) -> dict[str, Any]:
        user = db.ensure_user(body.account.strip())
        token = db.new_id("tok_")
        db.execute(
            "INSERT INTO sessions(id, user_id, token, created_at) VALUES(?,?,?,?)",
            (db.new_id("sess_"), user["id"], token, utc_now_iso()),
        )
        return {"token": token, "user_id": user["id"], "account": user["account"], "auth_mode": "dev"}

    @app.get("/v1/workflows")
    def list_workflows() -> dict[str, Any]:
        return {"workflows": get_registry().list()}

    @app.post("/v1/workflows/{name}/run")
    def run_workflow(
        name: str,
        body: WorkflowRunBody,
        user: dict[str, Any] = Depends(current_user),
        db: CoachDB = Depends(get_db),
    ) -> dict[str, Any]:
        payload = dict(body.payload)
        payload.setdefault("user_id", user["id"])
        try:
            if name.startswith("human.") or name in HUMAN_COACH_OUT_OF_SCOPE:
                assert_not_human_coach(
                    name if name in HUMAN_COACH_OUT_OF_SCOPE else "human_coach_request"
                )
            result = get_registry().run(name, payload, context={"db": db, "user": user})
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"name": name, "result": result}

    @app.post("/v1/facts/confirm")
    def confirm_facts(
        body: ConfirmFactsBody,
        user: dict[str, Any] = Depends(current_user),
        db: CoachDB = Depends(get_db),
    ) -> dict[str, Any]:
        for fid in body.fact_ids:
            db.execute(
                "UPDATE facts SET status='confirmed' WHERE id=? AND user_id=?",
                (fid, user["id"]),
            )
        return {"confirmed": body.fact_ids, "facts": db.list_confirmed_facts(user["id"])}

    @app.get("/v1/facts")
    def list_facts(
        user: dict[str, Any] = Depends(current_user),
        db: CoachDB = Depends(get_db),
    ) -> dict[str, Any]:
        rows = db.fetchall("SELECT * FROM facts WHERE user_id=? ORDER BY created_at", (user["id"],))
        for r in rows:
            r["meta"] = db.loads(r.pop("meta_json"), {})
        return {"facts": rows}

    @app.post("/v1/resumes/upload-text")
    def upload_resume_text(
        body: dict[str, Any],
        user: dict[str, Any] = Depends(current_user),
        db: CoachDB = Depends(get_db),
    ) -> dict[str, Any]:
        text = str(body.get("text") or "")
        if len(text.strip()) < 10:
            raise HTTPException(400, "简历文本过短")
        now = utc_now_iso()
        doc_id = db.new_id("doc_")
        ver_id = db.new_id("rv_")
        db.execute(
            "INSERT INTO resume_documents(id, user_id, filename, parse_status, text_content, created_at) VALUES(?,?,?,?,?,?)",
            (doc_id, user["id"], body.get("filename") or "paste.txt", "parsed", text, now),
        )
        sections = {"全文": text}
        db.execute(
            "INSERT INTO resume_versions(id, document_id, user_id, parent_id, label, sections_json, created_at) VALUES(?,?,?,?,?,?,?)",
            (ver_id, doc_id, user["id"], None, "原始版", db.dumps(sections), now),
        )
        extracted = get_registry().run(
            "evidence.extract",
            {"user_id": user["id"], "text": text, "source": "resume"},
            context={"db": db},
        )
        return {"document_id": doc_id, "version_id": ver_id, "extract": extracted}

    @app.get("/v1/knowledge/interview")
    def knowledge_interview(track: str = "campus_general", stage: str | None = None) -> dict[str, Any]:
        return knowledge_context_for_interview(track=track, stage=stage)

    @app.get("/v1/knowledge/hr")
    def knowledge_hr() -> dict[str, Any]:
        return {"items": load_hr_basics()}

    @app.get("/v1/home/today")
    def today(
        user: dict[str, Any] = Depends(current_user),
        db: CoachDB = Depends(get_db),
    ) -> dict[str, Any]:
        tasks = db.fetchall(
            "SELECT * FROM action_tasks WHERE user_id=? AND status='open' ORDER BY created_at LIMIT 3",
            (user["id"],),
        )
        primary = tasks[0] if tasks else {
            "title": "上传简历并确认事实",
            "reason": "AI 陪伴需基于已确认证据，才能给简历/面试建议",
        }
        return {
            "primary": primary,
            "tasks": tasks,
            "note": "人工 coach 求助未启用",
        }

    if STATIC_DIR.exists():
        app.mount("/coach/assets", StaticFiles(directory=str(STATIC_DIR)), name="coach_assets")

        @app.get("/coach")
        def coach_index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app


def create_app() -> FastAPI:
    return create_coach_app()
