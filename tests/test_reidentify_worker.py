"""Local re-identification worker commits rows before notifying the UI."""

from __future__ import annotations

from pathlib import Path

from app.collector.fill_from_url import FillCandidate
from app.collector.reidentify_worker import (
    drain_reidentify_messages,
    run_reidentify_task,
    serialize_candidate,
    start_reidentify_process,
)
from app.db.local import LocalDB


def _candidate(title: str, url: str) -> FillCandidate:
    return FillCandidate(
        fields={
            "company": "增量公司",
            "title": title,
            "source_url": url,
            "apply_url": url,
            "recruit_bucket": "校招",
            "jd_text": (title + " 岗位职责与任职要求。") * 12,
        },
        label=title,
        summary=title,
        detail_url=url,
    )


def _seed_db(path: Path) -> tuple[LocalDB, str, dict]:
    db = LocalDB(path)
    item_id = db.upsert_job(
        {
            "company": "增量公司",
            "title": "原入口",
            "source_url": "https://jobs.example.com/list",
            "apply_url": "https://jobs.example.com/list",
            "status": "pending_review",
        }
    )
    before = db.get_job(item_id)
    assert before is not None
    return db, item_id, before


def test_selected_task_commits_each_item_before_emit(tmp_path: Path):
    db, item_id, before = _seed_db(tmp_path / "incremental.db")
    candidates = [
        _candidate("岗位一", "https://jobs.example.com/1"),
        _candidate("岗位二", "https://jobs.example.com/2"),
    ]
    committed_ids: list[str] = []

    def emit(kind: str, payload):  # noqa: ANN001
        if kind != "item" or payload.get("action") not in ("inserted", "updated"):
            return
        job_id = str(payload["job_id"])
        stored = db.get_job(job_id)
        assert stored is not None
        assert stored["status"] == "pending_review"
        committed_ids.append(job_id)

    result = run_reidentify_task(
        db.path,
        {
            "mode": "selected",
            "item_id": item_id,
            "before": before,
            "page_url": before["source_url"],
            "candidates": [serialize_candidate(c) for c in candidates],
            "found_n": 2,
            "selected_n": 2,
            "timeout": 5,
        },
        emit,
    )
    assert result["ok_n"] == 2
    assert result["fail_n"] == 0
    assert len(committed_ids) == 2
    assert db.count_jobs(status="pending_review") == 3


def test_selected_process_reports_incremental_item(tmp_path: Path):
    db, item_id, before = _seed_db(tmp_path / "process.db")
    process, messages = start_reidentify_process(
        db.path,
        {
            "mode": "selected",
            "item_id": item_id,
            "before": before,
            "page_url": before["source_url"],
            "candidates": [
                serialize_candidate(_candidate("进程岗位", "https://jobs.example.com/p"))
            ],
            "found_n": 1,
            "selected_n": 1,
            "timeout": 5,
        },
    )
    process.join(timeout=20)
    assert not process.is_alive()
    assert process.exitcode == 0
    events = drain_reidentify_messages(messages, limit=100)
    assert any(kind == "item" and payload.get("job_id") for kind, payload in events)
    assert any(kind == "result" and payload.get("ok_n") == 1 for kind, payload in events)
    assert db.count_jobs(status="pending_review") == 2
    messages.close()
