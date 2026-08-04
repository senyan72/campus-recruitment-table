"""打包旁路 config.json → AppData 引导与云端凭证补全。"""

from __future__ import annotations

import json
from pathlib import Path

import app.config as config_mod


def test_bootstrap_sidecar_writes_viewer_safe_appdata(tmp_path: Path, monkeypatch):
    appdata = tmp_path / "AppData"
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    appdata.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))
    monkeypatch.setenv("CAMPUS_JOBS_BUNDLE_DIR", str(bundle))

    side = {
        "mode": "admin",
        "supabase_url": "https://example.supabase.co",
        "supabase_anon_key": "anon-test-key-xxxxxxxxxxxx",
        "supabase_service_role_key": "service-secret-MUST-NOT-COPY",
        "sync_interval_minutes": 15,
    }
    (bundle / "config.json").write_text(json.dumps(side), encoding="utf-8")

    assert config_mod.bootstrap_config_from_sidecar() is True
    saved = json.loads((appdata / "campus-jobs" / "config.json").read_text(encoding="utf-8"))
    assert saved["mode"] == "viewer"
    assert saved["supabase_url"] == "https://example.supabase.co"
    assert saved["supabase_anon_key"] == "anon-test-key-xxxxxxxxxxxx"
    assert saved["supabase_service_role_key"] == ""

    cfg = config_mod.load_config()
    assert cfg["supabase_anon_key"] == "anon-test-key-xxxxxxxxxxxx"
    assert cfg["supabase_service_role_key"] == ""


def test_read_json_dict_accepts_utf8_bom(tmp_path: Path):
    path = tmp_path / "bom.json"
    path.write_bytes(b'\xef\xbb\xbf{"supabase_url":"https://bom.example","supabase_anon_key":"k"}')
    data = config_mod._read_json_dict(path)
    assert data["supabase_url"] == "https://bom.example"
    assert data["supabase_anon_key"] == "k"


def test_load_config_fills_empty_cloud_keys_from_sidecar(tmp_path: Path, monkeypatch):
    appdata = tmp_path / "AppData"
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (appdata / "campus-jobs").mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))
    monkeypatch.setenv("CAMPUS_JOBS_BUNDLE_DIR", str(bundle))

    (appdata / "campus-jobs" / "config.json").write_text(
        json.dumps({"mode": "viewer", "supabase_url": "", "supabase_anon_key": ""}),
        encoding="utf-8",
    )
    (bundle / "config.json").write_text(
        json.dumps(
            {
                "supabase_url": "https://from-sidecar.supabase.co",
                "supabase_anon_key": "sidecar-anon",
                "supabase_service_role_key": "should-not-fill",
            }
        ),
        encoding="utf-8",
    )

    cfg = config_mod.load_config()
    assert cfg["supabase_url"] == "https://from-sidecar.supabase.co"
    assert cfg["supabase_anon_key"] == "sidecar-anon"
    assert cfg["supabase_service_role_key"] == ""
