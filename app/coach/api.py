"""AI 陪伴 FastAPI：/v1 + /coach H5。不含人工 coach。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.coach.ai import get_model_adapter, try_complete_json_or_none
from app.coach.ai.llm_client import llm_status, ping_provider, resolve_dual_routing
from app.coach.database import CoachDB
from app.coach.knowledge import (
    build_llm_knowledge_context,
    ingest_document,
    knowledge_context_for_interview,
    list_documents,
    list_packs,
    load_hr_basics,
    search_document_chunks,
    search_knowledge,
    upsert_hr_basic,
    upsert_interview_track,
    upsert_user_meta,
)
from app.coach.services.jobs_bridge import get_job_for_match, list_published_jobs
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


class LlmCompleteBody(BaseModel):
    task: str = Field(min_length=1, max_length=64)
    schema_name: str = Field(default="generic", max_length=64)
    system: str = ""
    user: str = Field(min_length=1)
    include_knowledge: bool = False
    knowledge_track: str = "campus_general"
    knowledge_query: str | None = None


class InterviewTrackBody(BaseModel):
    track: str = Field(min_length=1, max_length=64)
    questions: list[dict[str, Any]]
    merge: bool = True


class HrBasicBody(BaseModel):
    topic: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1)


class KnowledgeMetaBody(BaseModel):
    meta: dict[str, Any] = Field(default_factory=dict)


def _knowledge_write_allowed(admin_token: str | None) -> None:
    expected = (os.environ.get("COACH_ADMIN_TOKEN") or "").strip()
    if not expected:
        # 未配置时允许本地开发写入；生产请设置 COACH_ADMIN_TOKEN
        return
    if not admin_token or admin_token != expected:
        raise HTTPException(403, "需要有效的 X-Coach-Admin-Token 才能写入知识库")


def create_coach_app(*, db_path: str | None = None, campus_db_path: str | None = None) -> FastAPI:
    app = FastAPI(title="AI Job Companion", version="1.0.0")
    db = CoachDB(db_path)
    campus_path = campus_db_path

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
            "llm": llm_status(),
        }

    @app.get("/v1/llm/status")
    def get_llm_status() -> dict[str, Any]:
        """查看 LLM 外接是否就绪（不返回密钥）。"""
        status = llm_status()
        status["adapter"] = getattr(get_model_adapter(), "model_version", "unknown")
        return status

    @app.post("/v1/llm/ping")
    def llm_ping(
        user: dict[str, Any] = Depends(current_user),
    ) -> dict[str, Any]:
        """探测已配置的 Qwen / DeepSeek 是否可调用（会各发一次极小请求，产生微量费用）。"""
        del user
        dual = resolve_dual_routing()
        status = llm_status()
        targets: list[str] = []
        if status["primary"].get("api_key_configured") and status["primary"].get("provider"):
            targets.append(status["primary"]["provider"])
        if status.get("fallback") and status["fallback"].get("api_key_configured"):
            targets.append(status["fallback"]["provider"])
        # dual 下确保两家都测
        if dual.get("has_qwen_key") and "qwen" not in targets and "dashscope" not in targets:
            targets.append("qwen")
        if dual.get("has_deepseek_key") and "deepseek" not in targets:
            targets.append("deepseek")
        # 去重保序
        seen: set[str] = set()
        ordered: list[str] = []
        for t in targets:
            if t and t not in seen:
                seen.add(t)
                ordered.append(t)
        results = [ping_provider(p) for p in ordered]
        return {
            "dual": dual,
            "force_rules": status["force_rules"],
            "ready": status["ready"],
            "results": results,
            "all_ok": bool(results) and all(r.get("ok") for r in results),
        }

    @app.post("/v1/llm/complete-json")
    def llm_complete_json(
        body: LlmCompleteBody,
        user: dict[str, Any] = Depends(current_user),
        db: CoachDB = Depends(get_db),
    ) -> dict[str, Any]:
        """预留：直接调用外接 LLM 生成 JSON（需配置 Key；失败可返回 rules 提示）。"""
        system = body.system
        user_prompt = body.user
        if body.include_knowledge:
            kn = build_llm_knowledge_context(
                track=body.knowledge_track,
                query=body.knowledge_query,
                include_documents=True,
                db=db,
                owner_id=user["id"],
            )
            user_prompt = (
                f"{user_prompt}\n\n--- knowledge_context ---\n"
                f"{kn}\n--- end knowledge_context ---\n"
                f"仅可引用上述知识与用户已确认事实；缺失请标记 needs_proof/信息不足。"
            )
        out = try_complete_json_or_none(
            task=body.task,
            system=system,
            user=user_prompt,
            schema_name=body.schema_name,
        )
        if out is None:
            return {
                "ok": False,
                "fallback": "rules",
                "message": "LLM 未配置或调用失败；请设置 COACH_FORCE_RULES=0 与 COACH_LLM_*，或走 /v1/workflows 规则引擎",
                "llm": llm_status(),
                "user_id": user["id"],
            }
        return {"ok": True, "result": out, "user_id": user["id"]}

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
            result = get_registry().run(
                name,
                payload,
                context={"db": db, "user": user, "campus_db_path": campus_path},
            )
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

    @app.get("/v1/knowledge/packs")
    def knowledge_packs() -> dict[str, Any]:
        """内部知识库：内置包 + 用户可写包概览。"""
        return list_packs()

    @app.get("/v1/knowledge/interview")
    def knowledge_interview(track: str = "campus_general", stage: str | None = None) -> dict[str, Any]:
        return knowledge_context_for_interview(track=track, stage=stage)

    @app.get("/v1/knowledge/hr")
    def knowledge_hr(topic: str | None = None) -> dict[str, Any]:
        return {"items": load_hr_basics(topic=topic)}

    @app.get("/v1/knowledge/search")
    def knowledge_search(q: str, limit: int = 10) -> dict[str, Any]:
        return {"query": q, "hits": search_knowledge(query=q, limit=max(1, min(limit, 50)))}

    @app.get("/v1/knowledge/context")
    def knowledge_llm_context(
        track: str = "campus_general",
        stage: str | None = None,
        q: str | None = None,
        authorization: str | None = Header(default=None),
        db: CoachDB = Depends(get_db),
    ) -> dict[str, Any]:
        """给 LLM/workflow 组装用的知识上下文。"""
        owner_id: str | None = None
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization.split(" ", 1)[1].strip()
            row = db.fetchone("SELECT user_id FROM sessions WHERE token=?", (token,))
            if row:
                owner_id = row["user_id"]
        return build_llm_knowledge_context(
            track=track,
            stage=stage,
            query=q,
            include_documents=owner_id is not None,
            db=db if owner_id else None,
            owner_id=owner_id,
        )

    @app.get("/v1/knowledge/documents")
    def knowledge_documents_list(
        user: dict[str, Any] = Depends(current_user),
        db: CoachDB = Depends(get_db),
    ) -> dict[str, Any]:
        return {"documents": list_documents(db, owner_id=user["id"])}

    @app.post("/v1/knowledge/documents/upload")
    async def knowledge_documents_upload(
        file: UploadFile = File(...),
        user: dict[str, Any] = Depends(current_user),
        db: CoachDB = Depends(get_db),
        x_coach_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _knowledge_write_allowed(x_coach_admin_token)
        content = await file.read()
        if len(content) < 10:
            raise HTTPException(400, "文件过小或为空")
        if len(content) > 8 * 1024 * 1024:
            raise HTTPException(400, "单文件不超过 8MB")
        try:
            result = ingest_document(
                db,
                filename=file.filename or "upload.txt",
                content=content,
                owner_id=user["id"],
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except RuntimeError as e:
            raise HTTPException(500, str(e)) from e
        return {"ok": True, **result}

    @app.get("/v1/knowledge/documents/search")
    def knowledge_documents_search(
        q: str,
        limit: int = 8,
        user: dict[str, Any] = Depends(current_user),
        db: CoachDB = Depends(get_db),
    ) -> dict[str, Any]:
        return {
            "query": q,
            "hits": search_document_chunks(
                db, query=q, limit=max(1, min(limit, 30)), owner_id=user["id"]
            ),
        }

    @app.get("/v1/jobs")
    def list_jobs(
        keyword: str | None = None,
        location: str | None = None,
        bucket: str | None = None,
        limit: int = 30,
        user: dict[str, Any] = Depends(current_user),
    ) -> dict[str, Any]:
        del user  # 登录即可访问岗位库
        try:
            jobs = list_published_jobs(
                campus_db_path=campus_path,
                keyword=keyword,
                location=location,
                bucket=bucket,
                limit=limit,
            )
        except Exception as e:
            raise HTTPException(503, f"岗位库暂不可用: {e}") from e
        return {"jobs": jobs, "count": len(jobs)}

    @app.get("/v1/jobs/{job_id}")
    def get_job(
        job_id: str,
        user: dict[str, Any] = Depends(current_user),
    ) -> dict[str, Any]:
        del user
        try:
            job = get_job_for_match(job_id=job_id, campus_db_path=campus_path)
        except Exception as e:
            raise HTTPException(503, f"岗位库暂不可用: {e}") from e
        if not job:
            raise HTTPException(404, "岗位不存在")
        return {"job": job}

    @app.put("/v1/knowledge/interview/{track}")
    def knowledge_upsert_interview(
        track: str,
        body: InterviewTrackBody,
        x_coach_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _knowledge_write_allowed(x_coach_admin_token)
        try:
            return upsert_interview_track(
                track=body.track or track,
                questions=body.questions,
                merge=body.merge,
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.put("/v1/knowledge/hr/{topic}")
    def knowledge_upsert_hr(
        topic: str,
        body: HrBasicBody,
        x_coach_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _knowledge_write_allowed(x_coach_admin_token)
        try:
            return upsert_hr_basic(topic=body.topic or topic, content=body.content)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.put("/v1/knowledge/meta")
    def knowledge_upsert_meta(
        body: KnowledgeMetaBody,
        x_coach_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _knowledge_write_allowed(x_coach_admin_token)
        try:
            return upsert_user_meta(body.meta)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

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
