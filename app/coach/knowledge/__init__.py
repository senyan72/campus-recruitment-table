"""知识包加载与检索（可选用户包 + 内置校招默认包）。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from app.config import app_data_dir, project_root


def builtin_pack_dir() -> Path:
    return Path(__file__).resolve().parent / "builtin_campus_v1"


def user_pack_dir() -> Path:
    override = (os.environ.get("COACH_KNOWLEDGE_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    # 优先项目内 knowledge/，其次本机数据目录
    proj = project_root() / "knowledge"
    if proj.exists():
        return proj
    return app_data_dir() / "coach_knowledge"


def _read_yaml(path: Path) -> Any:
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def load_meta(pack_root: Path) -> dict[str, Any]:
    meta = _read_yaml(pack_root / "meta.yaml") or {}
    return meta if isinstance(meta, dict) else {}


def list_interview_tracks(pack_root: Path) -> list[str]:
    folder = pack_root / "interview_questions"
    if not folder.exists():
        return []
    return sorted(p.stem for p in folder.glob("*.yaml"))


def load_interview_questions(
    *,
    track: str = "campus_general",
    stage: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """合并用户包覆盖同名 track，否则回退内置包。"""
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
                snippets.append({"id": path.stem, "topic": path.stem, "summary": text[:2000]})
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
        "source": "user" if (user_pack_dir() / "interview_questions" / f"{track}.yaml").exists() else "builtin",
    }
