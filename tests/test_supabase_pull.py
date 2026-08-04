from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.db.local import LocalDB
from app.sync.supabase import SupabaseSync


class _Response:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.status_code = 200
        self.text = ""
        self._rows = rows

    def raise_for_status(self) -> None:
        return None

    def json(self) -> list[dict[str, Any]]:
        return self._rows


def _cloud_job(job_id: str, *, updated_at: str, status: str = "active") -> dict[str, Any]:
    return {
        "id": job_id,
        "company": "测试公司",
        "title": f"岗位-{job_id[-2:]}",
        "source_url": f"https://example.com/jobs/{job_id}",
        "status": status,
        "updated_at": updated_at,
    }


def test_pull_jobs_pages_with_composite_cursor_and_applies_tombstone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = LocalDB(tmp_path / "pull.db")
    ts = "2026-08-03T08:00:00+00:00"
    ids = [
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        "00000000-0000-0000-0000-000000000003",
    ]
    pages = [
        [_cloud_job(ids[0], updated_at=ts), _cloud_job(ids[1], updated_at=ts)],
        [_cloud_job(ids[2], updated_at=ts, status="deleted")],
    ]
    calls: list[dict[str, Any]] = []

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, _url: str, *, content: str, headers: dict[str, str]) -> _Response:
            assert headers["apikey"] == "anon"
            import json

            calls.append(json.loads(content))
            return _Response(pages[len(calls) - 1])

    monkeypatch.setattr("app.sync.supabase.httpx.Client", FakeClient)

    sync = SupabaseSync("https://example.supabase.co", "anon", session_token="token")
    assert sync.pull_jobs(db, since="1970-01-01T00:00:00+00:00", page_size=2) == 3

    assert calls[0]["p_updated_at"] == "1970-01-01T00:00:00+00:00"
    assert calls[0]["p_after_id"] is None
    assert calls[0]["p_limit"] == 2
    assert calls[1]["p_updated_at"] == ts
    assert calls[1]["p_after_id"] == ids[1]
    assert db.get_meta("last_cloud_sync_at") == ts
    assert db.get_meta("last_cloud_sync_id") == ids[2]
    assert db.count_jobs(status="active") == 2
    assert db.get_job(ids[2])["status"] == "deleted"


def test_pull_jobs_empty_page_does_not_advance_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = LocalDB(tmp_path / "empty.db")
    db.set_meta("last_cloud_sync_at", "2026-08-03T08:00:00+00:00")
    db.set_meta("last_cloud_sync_id", "00000000-0000-0000-0000-000000000009")

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, _url: str, *, content: str, headers: dict[str, str]) -> _Response:
            import json

            body = json.loads(content)
            assert body["p_after_id"] == "00000000-0000-0000-0000-000000000009"
            return _Response([])

    monkeypatch.setattr("app.sync.supabase.httpx.Client", FakeClient)

    sync = SupabaseSync("https://example.supabase.co", "anon", session_token="token")
    assert sync.pull_jobs(db) == 0
    assert db.get_meta("last_cloud_sync_at") == "2026-08-03T08:00:00+00:00"
    assert db.get_meta("last_cloud_sync_id") == "00000000-0000-0000-0000-000000000009"
