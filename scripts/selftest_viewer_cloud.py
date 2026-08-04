"""从解压目录旁路 config 做云同步自检（隔离 LOCALAPPDATA）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    bundle = Path(os.environ.get("CAMPUS_JOBS_BUNDLE_DIR") or "").expanduser()
    if not bundle.is_dir():
        print("ERROR: set CAMPUS_JOBS_BUNDLE_DIR to extracted viewer folder")
        return 2

    # 证明同学端导入路径无环状依赖
    from app.collector.adapters import parse_job_url  # noqa: F401
    from app.collector import label_fields  # noqa: F401
    from app.config import (
        bootstrap_config_from_sidecar,
        config_path,
        load_config,
        sidecar_config_path,
    )
    from app.db.local import LocalDB
    from app.sync.supabase import SupabaseSync

    print("SIDECAR", sidecar_config_path(), sidecar_config_path().exists())
    print("BOOTSTRAP", bootstrap_config_from_sidecar())
    cfg = load_config()
    print("APPDATA_CFG", config_path(), config_path().exists())
    print("URL", (cfg.get("supabase_url") or "")[:48])
    print("ANON_LEN", len(cfg.get("supabase_anon_key") or ""))
    print("SERVICE_LEN", len(cfg.get("supabase_service_role_key") or ""))
    print("MODE", cfg.get("mode"))

    if not cfg.get("supabase_url") or not cfg.get("supabase_anon_key"):
        print("SYNC_RESULT ERROR missing url/anon from packaged config")
        return 1
    if cfg.get("supabase_service_role_key"):
        print("SYNC_RESULT ERROR service_role must not be present for viewer")
        return 1

    sync = SupabaseSync(cfg["supabase_url"], cfg["supabase_anon_key"])
    print("SYNC_ENABLED", sync.enabled)
    ok, min_v = sync.check_min_version()
    print("MIN_VERSION_OK", ok, min_v)

    account = (os.environ.get("CAMPUS_JOBS_VIEWER_ACCOUNT") or "").strip()
    password = os.environ.get("CAMPUS_JOBS_VIEWER_PASSWORD") or ""
    if not account or not password:
        print("SYNC_RESULT ERROR set CAMPUS_JOBS_VIEWER_ACCOUNT/PASSWORD for authenticated selftest")
        return 2
    token = sync.create_viewer_session(account, password)
    if not token:
        print("SYNC_RESULT ERROR viewer login failed")
        return 1
    print("SESSION", "OK")

    db_file = Path(os.environ["LOCALAPPDATA"]) / "campus-jobs" / "selftest.db"
    db = LocalDB(db_file)
    try:
        n = sync.pull_jobs(db)
        with db.conn() as c:
            local_jobs = int(c.execute("select count(*) from jobs").fetchone()[0])
        print("PULL_UPDATED", n)
        print("LOCAL_JOBS", local_jobs)
        print("LAST_SYNC", db.get_meta("last_cloud_sync_at"))
        print("SYNC_RESULT", "OK")
        return 0
    except Exception as exc:  # noqa: BLE001
        print("SYNC_RESULT", "ERROR", type(exc).__name__, str(exc)[:400])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
