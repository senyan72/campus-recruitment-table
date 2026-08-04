from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.db.local import LocalDB
from app.sync.supabase import SupabaseSync, ViewerSessionError


ROOT = Path(__file__).resolve().parents[1]


def test_viewer_session_rpc_returns_token_and_stores_it(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return [{"session_token": "opaque-token", "session_account": "alice"}]

    class Client:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):  # noqa: ANN002
            return False

        def post(self, url, **kwargs):  # noqa: ANN001
            assert url.endswith("/rpc/viewer_login_session")
            body = json.loads(kwargs["content"])
            assert body == {"p_account": "alice", "p_password": "pw"}
            return Response()

    monkeypatch.setattr("app.sync.supabase.httpx.Client", Client)
    sync = SupabaseSync("https://example.supabase.co", "anon")
    assert sync.create_viewer_session("alice", "pw") == "opaque-token"
    assert sync.session_token == "opaque-token"


def test_viewer_sync_clears_expired_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Response:
        status_code = 401
        text = "viewer session expired"

        @staticmethod
        def json():
            return []

    class Client:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):  # noqa: ANN002
            return False

        def post(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return Response()

    monkeypatch.setattr("app.sync.supabase.httpx.Client", Client)
    sync = SupabaseSync("https://example.supabase.co", "anon", session_token="expired")
    with pytest.raises(ViewerSessionError):
        sync.pull_jobs(LocalDB(tmp_path / "x.db"))
    assert sync.session_token == ""


def test_viewer_session_migration_removes_direct_anon_reads() -> None:
    sql = (ROOT / "supabase" / "migrations" / "20260803_viewer_sessions.sql").read_text(
        encoding="utf-8"
    )
    assert "create table if not exists public.viewer_sessions" in sql
    assert "create table if not exists public.viewer_login_attempts" in sql
    assert "create or replace function public.viewer_sync_jobs" in sql
    assert "drop policy if exists \"jobs_select_active\"" in sql
    assert "grant execute on function public.viewer_sync_jobs" in sql
    assert "revoke all on function public.viewer_login(text, text)" in sql
