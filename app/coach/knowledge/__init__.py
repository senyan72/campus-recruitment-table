"""知识包加载、检索与写入（内置 + 用户包）。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from app.config import app_data_dir, project_root

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


def builtin_pack_dir() -> Path:
    return Path(__file__).resolve().parent / "builtin_campus_v1"


def user_pack_dir() -> Path:
    override = (os.environ.get("COACH_KNOWLEDGE_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    proj = project_root() / "knowledge"
    if proj.exists():
        return proj
    path = app_data_dir() / "coach_knowledge"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_yaml(path: Path) -> Any:
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _safe_id(name: str) -> str:
    name = (name or "").strip()
    if not _SAFE_NAME.match(name):
        raise ValueError("名称仅允许字母数字下划线与短横线，最长 64")
    return name


def load_meta(pack_root: Path) -> dict[str, Any]:
    meta = _read_yaml(pack_root / "meta.yaml") or {}
    return meta if isinstance(meta, dict) else {}


def list_interview_tracks(pack_root: Path) -> list[str]:
    folder = pack_root / "interview_questions"
    if not folder.exists():
        return []
    return sorted(p.stem for p in folder.glob("*.yaml"))


def list_packs() -> dict[str, Any]:
    user = user_pack_dir()
    builtin = builtin_pack_dir()
    return {
        "builtin": {
            "path": str(builtin),
            "meta": load_meta(builtin),
            "interview_tracks": list_interview_tracks(builtin),
            "hr_topics": sorted(p.stem for p in (builtin / "hr_basics").glob("*.md"))
            if (builtin / "hr_basics").exists()
            else [],
        },
        "user": {
            "path": str(user),
            "meta": load_meta(user),
            "interview_tracks": list_interview_tracks(user),
            "hr_topics": sorted(p.stem for p in (user / "hr_basics").glob("*.md"))
            if (user / "hr_basics").exists()
            else [],
            "writable": True,
        },
    }


def load_interview_questions(
    *,
    track: str = "campus_general",
    stage: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for root in (user_pack_dir(), builtin_pack_dir()):
        path = root / "interview_questions" / f"{track}.yaml"
        data = _read_yaml(path)
        if not data:
            continue
        items = data.get("questions") if isinstance(data, dict) else data
        if not isinstance(items, list):
            continue
        for q in items:
            if not isinstance(q, dict):
                continue
            if stage and q.get("stage") and q.get("stage") != stage:
                continue
            questions.append(dict(q))
        if questions:
            break
    return questions[:limit]


def load_hr_basics(*, topic: str | None = None) -> list[dict[str, Any]]:
    snippets: list[dict[str, Any]] = []
    for root in (user_pack_dir(), builtin_pack_dir()):
        folder = root / "hr_basics"
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.md")):
            if topic and topic not in path.stem:
                continue
            text = _read_text(path).strip()
            if text:
                snippets.append({"id": path.stem, "topic": path.stem, "summary": text[:4000]})
        if snippets:
            break
    return snippets


def knowledge_context_for_interview(
    *,
    track: str = "campus_general",
    stage: str | None = None,
) -> dict[str, Any]:
    return {
        "questions": load_interview_questions(track=track, stage=stage, limit=6),
        "hr_basics": load_hr_basics()[:3],
        "pack_meta": load_meta(user_pack_dir()) or load_meta(builtin_pack_dir()),
        "source": "user"
        if (user_pack_dir() / "interview_questions" / f"{track}.yaml").exists()
        else "builtin",
    }


def search_knowledge(*, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """简易全文检索（题库 + 人事短文），供 LLM context 组装。"""
    q = (query or "").strip().lower()
    if not q:
        return []
    hits: list[dict[str, Any]] = []
    for track in set(list_interview_tracks(user_pack_dir()) + list_interview_tracks(builtin_pack_dir())):
        for item in load_interview_questions(track=track, limit=50):
            blob = " ".join(
                str(item.get(k) or "")
                for k in ("question", "intent", "id", "track", "stage")
            ).lower()
            if q in blob:
                hits.append({"kind": "interview_question", "track": track, "item": item})
            if len(hits) >= limit:
                return hits
    for hr in load_hr_basics():
        if q in (hr.get("summary") or "").lower() or q in (hr.get("topic") or "").lower():
            hits.append({"kind": "hr_basic", "item": hr})
        if len(hits) >= limit:
            break
    return hits[:limit]


def upsert_interview_track(*, track: str, questions: list[dict[str, Any]], merge: bool = True) -> dict[str, Any]:
    """写入用户包面试题（不改内置包）。"""
    track = _safe_id(track)
    if not isinstance(questions, list):
        raise ValueError("questions 必须是数组")
    path = user_pack_dir() / "interview_questions" / f"{track}.yaml"
    existing: list[dict[str, Any]] = []
    if merge and path.exists():
        data = _read_yaml(path) or {}
        raw = data.get("questions") if isinstance(data, dict) else data
        if isinstance(raw, list):
            existing = [x for x in raw if isinstance(x, dict)]
    by_id: dict[str, dict[str, Any]] = {}
    for q in existing + list(questions):
        if not isinstance(q, dict):
            continue
        qid = str(q.get("id") or "").strip() or f"q_{len(by_id)+1}"
        q = dict(q)
        q["id"] = qid
        by_id[qid] = q
    out = {"questions": list(by_id.values())}
    _write_yaml(path, out)
    return {"track": track, "path": str(path), "count": len(out["questions"])}


def upsert_hr_basic(*, topic: str, content: str) -> dict[str, Any]:
    topic = _safe_id(topic)
    path = user_pack_dir() / "hr_basics" / f"{topic}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((content or "").strip() + "\n", encoding="utf-8")
    return {"topic": topic, "path": str(path), "bytes": path.stat().st_size}


def upsert_user_meta(meta: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(meta, dict):
        raise ValueError("meta 必须是对象")
    path = user_pack_dir() / "meta.yaml"
    current = load_meta(user_pack_dir())
    current.update(meta)
    _write_yaml(path, current)
    return {"path": str(path), "meta": current}


def build_llm_knowledge_context(
    *,
    track: str = "campus_general",
    stage: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    """给 LLM 用的知识库上下文块。"""
    ctx = knowledge_context_for_interview(track=track, stage=stage)
    if query:
        ctx["search_hits"] = search_knowledge(query=query, limit=8)
    return ctx
