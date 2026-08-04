"""夜间复检调度辅助与全量复检参数。"""

from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from app.collector.nightly import NightlyScheduler, run_nightly_refresh, seconds_until_next_local
from app.db.local import LocalDB
from app.timeutil import CN_TZ


def test_seconds_until_next_local_future_today():
    fixed = datetime(2026, 8, 1, 1, 0, 0)

    with patch("app.timeutil.now", return_value=fixed.replace(tzinfo=CN_TZ)):
        secs = seconds_until_next_local(2, 0)
    assert 3500 <= secs <= 3700


def test_seconds_until_next_local_rolls_to_tomorrow():
    fixed = datetime(2026, 8, 1, 22, 0, 0)

    with patch("app.timeutil.now", return_value=fixed.replace(tzinfo=CN_TZ)):
        secs = seconds_until_next_local(2, 0)
    # 22:00 → 次日 02:00 = 4 小时
    assert abs(secs - 4 * 3600) < 5


def test_seconds_until_next_local_supports_midnight():
    fixed = datetime(2026, 8, 1, 23, 0, 0)
    assert seconds_until_next_local(0, 0, now=fixed) == 3600


def test_scheduler_detects_missed_run_after_startup(tmp_path: Path):
    db = LocalDB(tmp_path / "catch-up.db")
    scheduler = NightlyScheduler(db, hour=2, minute=0, enabled=True)
    assert scheduler._pending_catch_up_slot(datetime(2026, 8, 1, 3, 0, 0)) == datetime(
        2026, 8, 1, 2, 0, 0
    )


def test_scheduler_does_not_catch_up_before_first_schedule(tmp_path: Path):
    db = LocalDB(tmp_path / "no-catch-up.db")
    scheduler = NightlyScheduler(db, hour=2, minute=0, enabled=True)
    assert scheduler._pending_catch_up_slot(datetime(2026, 8, 1, 1, 0, 0)) is None


def test_scheduler_does_not_repeat_an_attempted_schedule(tmp_path: Path):
    db = LocalDB(tmp_path / "attempted.db")
    db.set_meta("last_nightly_attempt_at", "2026-07-31T19:00:00+00:00")
    scheduler = NightlyScheduler(db, hour=2, minute=0, enabled=True)
    assert scheduler._pending_catch_up_slot(datetime(2026, 8, 1, 4, 0, 0)) is None


def test_update_schedule_wakes_running_loop_and_keeps_midnight(tmp_path: Path):
    db = LocalDB(tmp_path / "schedule.db")
    scheduler = NightlyScheduler(db, enabled=True)
    scheduler._thread = Mock()
    scheduler._thread.is_alive.return_value = True
    scheduler.update_schedule(
        enabled=True,
        hour=0,
        minute=15,
        lookback_days=120,
        limit_companies=0,
    )
    assert (scheduler.hour, scheduler.minute) == (0, 15)
    assert scheduler.lookback_days == 120
    assert scheduler._wake.is_set()


def test_execute_once_prevents_overlapping_runs(tmp_path: Path):
    db = LocalDB(tmp_path / "lock.db")
    scheduler = NightlyScheduler(db, enabled=True)
    scheduler._job_lock.acquire()
    try:
        with patch("app.collector.nightly.run_nightly_refresh") as refresh:
            assert scheduler._execute_once() is False
            refresh.assert_not_called()
    finally:
        scheduler._job_lock.release()


def test_execute_once_records_failure_for_admin_status(tmp_path: Path):
    db = LocalDB(tmp_path / "failure.db")
    scheduler = NightlyScheduler(db, enabled=True)
    with patch("app.collector.nightly.run_nightly_refresh", side_effect=RuntimeError("network down")):
        assert scheduler._execute_once() is False
    assert db.get_meta("last_nightly_status") == "failed"
    assert db.get_meta("last_nightly_error") == "network down"
    assert db.get_meta("last_nightly_attempt_at")


def test_run_nightly_refresh_calls_full_campus_intern_recheck(tmp_path: Path):
    db = LocalDB(tmp_path / "nightly.db")
    db.upsert_company(
        {
            "name": "复检公司",
            "career_urls": ["https://careers.example.com/campus"],
            "verify_status": "official",
        }
    )
    captured: dict = {}

    def fake_collect(db_arg, **kwargs):
        captured.update(kwargs)
        return {
            "parsed": 1,
            "published": 0,
            "updated": 0,
            "unchanged": 1,
            "queued": 0,
            "errors": 0,
            "skipped": 0,
            "stale": 0,
        }

    with patch("app.collector.nightly.run_website_collect", side_effect=fake_collect):
        with patch("app.collector.nightly.cleanup_expired_jobs", return_value={"deleted": 0}):
            result = run_nightly_refresh(db, lookback_days=90, limit_companies=0)

    assert captured.get("limit_companies") == 0
    assert captured.get("max_career_urls") == 0
    assert captured.get("stop_when_published") is False
    assert "无变化" in (result.get("text") or "")
    assert db.get_meta("last_scan_mode") == "nightly"


def test_upsert_job_skips_identical_and_updates_changed(tmp_path: Path):
    db = LocalDB(tmp_path / "upsert.db")
    payload = {
        "company": "甲公司",
        "title": "后端开发",
        "source_url": "https://example.com/job/1",
        "apply_url": "https://example.com/job/1",
        "deadline": "2026-12-31",
        "jd_text": "职责 A",
        "status": "active",
        "recruit_bucket": "校招",
        "confidence": 0.8,
    }
    jid1, action1 = db.upsert_job_with_action(payload)
    assert action1 == "inserted"
    row1 = db.get_job(jid1)
    assert row1 is not None
    updated_at_1 = row1["updated_at"]

    jid2, action2 = db.upsert_job_with_action(dict(payload))
    assert jid2 == jid1
    assert action2 == "unchanged"
    assert db.get_job(jid1)["updated_at"] == updated_at_1
    assert db.count_jobs() == 1

    changed = dict(payload)
    changed["deadline"] = "2027-01-15"
    changed["jd_text"] = "职责 B 更新"
    jid3, action3 = db.upsert_job_with_action(changed)
    assert jid3 == jid1
    assert action3 == "updated"
    assert db.count_jobs() == 1
    assert db.get_job(jid1)["deadline"] == "2027-01-15"
