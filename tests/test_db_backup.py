from __future__ import annotations

from pathlib import Path

from app.db.local import LocalDB


def _job(title: str) -> dict[str, str]:
    return {
        "company": "备份测试公司",
        "title": title,
        "source_url": f"https://example.com/{title}",
        "status": "active",
    }


def test_existing_database_gets_daily_backup(tmp_path: Path) -> None:
    path = tmp_path / "campus.db"
    first = LocalDB(path)
    first.upsert_job(_job("岗位一"))

    second = LocalDB(path)
    backups = list((tmp_path / "backups").glob("campus-*.db"))
    assert backups
    assert second.integrity_check().lower() == "ok"


def test_restore_from_backup_replaces_current_data(tmp_path: Path) -> None:
    path = tmp_path / "campus.db"
    db = LocalDB(path)
    db.upsert_job(_job("原始岗位"))
    backup = tmp_path / "manual-backup.db"
    db.backup_to(backup)

    db.upsert_job(_job("后来岗位"))
    assert db.count_jobs(status="active") == 2
    db.restore_from(backup)

    assert db.count_jobs(status="active") == 1
    assert db.list_jobs(status="active")[0]["title"] == "原始岗位"
    assert db.integrity_check().lower() == "ok"
