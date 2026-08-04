"""深度采集队列：建队、进度、中断标记（不打真实 ATS）。"""

from pathlib import Path
from queue import Queue
from unittest.mock import patch

from app.collector.deep import (
    company_index_from_progress,
    format_deep_progress,
    job_counts_from_stats,
    needs_deep_parse_supplement,
    run_deep_collect,
    supplement_company_parsing,
)
from app.collector.deep_worker import drain_deep_messages, start_deep_collect_process
from app.db.local import LocalDB


def test_format_deep_progress():
    assert format_deep_progress(40, 10931) == "深度采集进度：40/10931"
    assert format_deep_progress(40, 10931, 12, 47) == "深度采集进度：40/10931（12/47）"


def test_job_counts_from_stats():
    pending, total = job_counts_from_stats({"batch_new": 5, "skipped_known": 10, "enum_total": 47}, 50)
    assert total == 47
    assert pending == 32
    pending2, total2 = job_counts_from_stats({}, 50)
    assert total2 == 50
    assert pending2 == 50


def test_company_index_from_progress():
    assert company_index_from_progress({"processed": 39, "running": 1}) == 40
    assert company_index_from_progress({"processed": 40, "running": 0}) == 40


def test_deep_worker_drains_without_blocking():
    messages = Queue()
    messages.put(("progress", "深度采集进度：1/2"))
    messages.put(("result", {"jobs_added": 2}))
    assert drain_deep_messages(messages) == [
        ("progress", "深度采集进度：1/2"),
        ("result", {"jobs_added": 2}),
    ]


def test_deep_worker_process_finishes_empty_run(tmp_path: Path):
    db = LocalDB(tmp_path / "worker.db")
    process, messages = start_deep_collect_process(
        db.path,
        {
            "lookback_days": 90,
            "collect_months": 3,
            "list_collect_months": 6,
            "retention_days": 365,
            "batch_size": 1,
            "concurrency": 1,
            "max_retries": 0,
            "resume": False,
            "reset": True,
            "parse_supplement_limit": 0,
            "parse_supplement_timeout": 5,
        },
    )
    process.join(timeout=15)
    assert not process.is_alive()
    assert process.exitcode == 0
    events = drain_deep_messages(messages, limit=100)
    results = [payload for kind, payload in events if kind == "result"]
    assert results and results[0]["companies_total"] == 0
    messages.close()


def test_needs_deep_parse_supplement_only_for_incomplete_rows():
    assert needs_deep_parse_supplement(
        {"source_url": "https://example.com/job", "jd_text": "短", "parse_status": "ok"}
    )
    assert not needs_deep_parse_supplement(
        {
            "source_url": "https://example.com/job",
            "jd_text": "x" * 100,
            "parse_status": "ok",
        }
    )


def test_supplement_company_parsing_reuses_reidentify(tmp_path: Path):
    db = LocalDB(tmp_path / "supplement.db")
    cid = db.upsert_company({"name": "补全公司", "verify_status": "official"})
    jid = db.upsert_job(
        {
            "company_id": cid,
            "company": "补全公司",
            "title": "后端开发",
            "source_url": "https://example.com/job/1",
            "apply_url": "https://example.com/job/1",
            "jd_text": "短",
            "parse_status": "needs_browser",
            "status": "pending_review",
        }
    )
    before = db.get_job(jid)
    assert before is not None
    merged = dict(before)
    merged["jd_text"] = "完整岗位职责与任职要求" * 10
    merged["parse_status"] = "ok"
    with patch(
        "app.collector.fill_from_url.reidentify_job_fields",
        return_value=("update", merged, "ok", object()),
    ) as reidentify:
        counts = supplement_company_parsing(
            db,
            {"id": cid, "name": "补全公司"},
            since="2000-01-01T00:00:00+00:00",
            limit=1,
        )
    assert counts["checked"] == 1
    assert counts["updated"] == 1
    assert reidentify.call_args.kwargs["timeout"] == 20.0
    assert len((db.get_job(jid) or {}).get("jd_text") or "") > 80


def test_collect_queue_create_and_progress(tmp_path: Path):
    db = LocalDB(tmp_path / "deep.db")
    companies = []
    for i in range(5):
        cid = db.upsert_company(
            {
                "name": f"测试公司{i}",
                "verify_status": "official",
                "career_urls": [f"https://demo.jobs.feishu.cn/campus{i}"],
            }
        )
        companies.append({"id": cid, "name": f"测试公司{i}"})

    run_id = db.create_collect_run(companies, reset=True, resume=False)
    prog = db.collect_queue_progress(run_id)
    assert prog["total"] == 5
    assert prog["pending"] == 5

    pending = db.list_collect_queue(run_id, status="pending", limit=2)
    assert len(pending) == 2
    db.update_collect_queue_item(pending[0]["id"], status="done", jobs_published=2)
    prog2 = db.collect_queue_progress(run_id)
    assert prog2["done"] == 1
    assert prog2["jobs_published"] == 2
    assert prog2["processed"] == 1


def test_deep_collect_resumable_with_mock(tmp_path: Path):
    db = LocalDB(tmp_path / "deep2.db")
    for i in range(3):
        db.upsert_company(
            {
                "name": f"MockCorp{i}",
                "verify_status": "official",
                "career_urls": [f"https://example.com/career/{i}"],
            }
        )

    def fake_collect(db, company, **kwargs):
        return {"parsed": 1, "published": 1, "queued": 0, "errors": 0, "skipped": 0, "stale": 0}

    with patch("app.collector.deep.collect_company_careers", side_effect=fake_collect):
        with patch("app.collector.deep.promote_trusted_seeds", return_value=0):
            result = run_deep_collect(
                db,
                lookback_days=90,
                batch_size=2,
                concurrency=1,
                resume=False,
                reset=True,
            )
    assert result["companies_total"] == 3
    assert result["companies_processed"] == 3
    assert result["web"]["published"] == 3
    assert not result["cancelled"]

    # 再跑应因无 pending 立刻结束（resume 新 run 但公司已无 pending 于旧队）
    run_id = db.get_meta("deep_collect_run_id")
    assert run_id
    prog = db.collect_queue_progress(run_id)
    assert prog["pending"] == 0


def test_deep_collect_auto_continues_50_plus_46_for_one_company(tmp_path: Path):
    db = LocalDB(tmp_path / "deep-auto-batches.db")
    db.upsert_company(
        {
            "name": "九十六岗公司",
            "verify_status": "official",
            "career_urls": ["https://example.com/campus"],
        }
    )
    responses = [
        {
            "parsed": 50,
            "published": 50,
            "queued": 0,
            "errors": 0,
            "skipped": 0,
            "stale": 0,
            "batch_new": 50,
            "skipped_known": 0,
            "enum_total": 96,
        },
        {
            "parsed": 96,
            "published": 46,
            "queued": 0,
            "errors": 0,
            "skipped": 0,
            "stale": 0,
            "batch_new": 46,
            "skipped_known": 50,
            "enum_total": 96,
        },
    ]
    calls = {"n": 0}

    def fake_collect(db, company, **kwargs):
        calls["n"] += 1
        return responses.pop(0)

    with patch("app.collector.deep.collect_company_careers", side_effect=fake_collect):
        with patch("app.collector.deep.promote_trusted_seeds", return_value=0):
            result = run_deep_collect(
                db,
                resume=False,
                reset=True,
                max_company_batches=10,
            )
    assert calls["n"] == 2
    assert result["web"]["published"] == 96
    assert result["web"]["company_batches"] == 2
    assert result["web"]["continuation_limited"] == 0


def test_deep_collect_cancel_keeps_current_company_pending_between_batches(
    tmp_path: Path,
):
    db = LocalDB(tmp_path / "deep-batch-cancel.db")
    db.upsert_company(
        {
            "name": "可续采公司",
            "verify_status": "official",
            "career_urls": ["https://example.com/campus"],
        }
    )

    def fake_collect(db, company, **kwargs):
        db.request_deep_collect_cancel()
        return {
            "parsed": 50,
            "published": 50,
            "queued": 0,
            "errors": 0,
            "skipped": 0,
            "stale": 0,
            "batch_new": 50,
            "skipped_known": 0,
            "enum_total": 96,
        }

    with patch("app.collector.deep.collect_company_careers", side_effect=fake_collect):
        with patch("app.collector.deep.promote_trusted_seeds", return_value=0):
            result = run_deep_collect(db, resume=False, reset=True)
    assert result["cancelled"]
    run_id = db.get_meta("deep_collect_run_id")
    assert run_id
    assert db.collect_queue_progress(run_id)["pending"] == 1


def test_deep_collect_cancel(tmp_path: Path):
    db = LocalDB(tmp_path / "deep3.db")
    for i in range(4):
        db.upsert_company(
            {
                "name": f"CancelCorp{i}",
                "verify_status": "official",
                "hint_apply_urls": [f"https://app.mokahr.com/campus/{i}"],
            }
        )

    calls = {"n": 0}

    def fake_collect(db, company, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            db.request_deep_collect_cancel()
        return {"parsed": 1, "published": 0, "queued": 1, "errors": 0, "skipped": 0, "stale": 0}

    with patch("app.collector.deep.collect_company_careers", side_effect=fake_collect):
        with patch("app.collector.deep.promote_trusted_seeds", return_value=0):
            result = run_deep_collect(
                db,
                batch_size=1,
                concurrency=1,
                resume=False,
                reset=True,
            )
    assert result["cancelled"]
    assert result["companies_processed"] < result["companies_total"]
