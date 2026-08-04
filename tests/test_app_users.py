"""Viewer 登录账号 CRUD、哈希校验与同步映射。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.auth.password import hash_password, verify_password
from app.auth.license import is_expired, normalize_expiry
from app.db.local import LocalDB
from app.ui.account_login import verify_viewer_credentials
from app.sync.supabase import (
    SupabaseSync,
    _cloud_to_local_user,
    _local_to_cloud_user,
)


def test_hash_and_verify_password():
    h = hash_password("secret123")
    assert h.startswith("pbkdf2_sha256$")
    assert verify_password("secret123", h)
    assert not verify_password("wrong", h)


def test_account_pk_unique(tmp_path: Path):
    db = LocalDB(tmp_path / "users.db")
    db.create_app_user("alice", "pass-a")
    with pytest.raises(ValueError, match="已存在"):
        db.create_app_user("alice", "pass-b")
    assert db.count_app_users() == 1


def test_duplicate_password_allowed(tmp_path: Path):
    db = LocalDB(tmp_path / "dup_pwd.db")
    db.create_app_user("u1", "same-password")
    db.create_app_user("u2", "same-password")
    assert db.verify_app_user_login("u1", "same-password")
    assert db.verify_app_user_login("u2", "same-password")
    u1 = db.get_app_user("u1")
    u2 = db.get_app_user("u2")
    assert u1 and u2
    assert u1["password_hash"] != u2["password_hash"]


def test_update_password_and_disable(tmp_path: Path):
    db = LocalDB(tmp_path / "update.db")
    db.create_app_user("bob", "old-pwd", notes="test")
    assert db.verify_app_user_login("bob", "old-pwd")

    db.update_app_user("bob", password="new-pwd", notes="updated", enabled=False)
    assert not db.verify_app_user_login("bob", "old-pwd")
    assert not db.verify_app_user_login("bob", "new-pwd")

    db.update_app_user("bob", enabled=True)
    assert db.verify_app_user_login("bob", "new-pwd")
    row = db.get_app_user("bob")
    assert row is not None
    assert row["notes"] == "updated"


def test_account_expiry_blocks_login_and_roundtrips(tmp_path: Path):
    db = LocalDB(tmp_path / "expiry.db")
    db.create_app_user("trial", "pw", expires_at="2026-08-02")
    assert db.get_app_user("trial")["expires_at"] == "2026-08-02"
    assert not db.verify_app_user_login("trial", "pw")
    db.update_app_user("trial", expires_at="2099-01-01")
    assert db.verify_app_user_login("trial", "pw")
    assert normalize_expiry("2026-08-02T12:30:00+00:00") == "2026-08-02"
    assert is_expired("2026-08-02", today=date(2026, 8, 3))


def test_delete_account(tmp_path: Path):
    db = LocalDB(tmp_path / "del.db")
    db.create_app_user("x", "p")
    assert db.delete_app_user("x")
    assert db.get_app_user("x") is None
    assert not db.delete_app_user("missing")


def test_sync_mappers_no_plaintext_password():
    user = {
        "account": "alice",
        "password_hash": hash_password("pw"),
        "notes": "n",
        "enabled": 1,
        "expires_at": "2026-12-31",
        "updated_at": "2026-08-01T00:00:00+00:00",
    }
    cloud = _local_to_cloud_user(user)
    dumped = json.dumps(cloud)
    assert "password_hash" in cloud
    assert "pw" not in dumped
    assert "password" not in cloud

    local = _cloud_to_local_user(
        {
            "account": "alice",
            "password_hash": user["password_hash"],
            "notes": "n",
            "enabled": True,
            "expires_at": "2026-12-31",
            "updated_at": "2026-08-01T00:00:00+00:00",
        }
    )
    assert "password" not in local
    assert local["password_hash"] == user["password_hash"]
    assert local["expires_at"] == "2026-12-31"


def test_replace_app_users_from_cloud(tmp_path: Path):
    db = LocalDB(tmp_path / "cloud.db")
    db.create_app_user("keep", "p1")
    db.create_app_user("remove", "p2")

    h_new = hash_password("new")
    db.replace_app_users_from_cloud(
        [
            {
                "account": "keep",
                "password_hash": h_new,
                "notes": "from cloud",
                "enabled": True,
                "updated_at": "2026-08-02T00:00:00+00:00",
            },
            {
                "account": "added",
                "password_hash": hash_password("a"),
                "notes": None,
                "enabled": True,
                "updated_at": "2026-08-02T00:00:00+00:00",
            },
        ]
    )
    assert db.get_app_user("remove") is None
    assert db.verify_app_user_login("keep", "new")
    assert db.get_app_user("added") is not None
    assert db.count_app_users() == 2


def test_replace_app_users_does_not_touch_my_pick(tmp_path: Path):
    db = LocalDB(tmp_path / "pick.db")
    jid = db.upsert_job(
        {
            "company": "Co",
            "title": "Dev",
            "source_url": "https://example.com/j/1",
            "status": "active",
        }
    )
    db.add_my_pick([jid])
    db.create_app_user("u", "p")

    db.replace_app_users_from_cloud(
        [
            {
                "account": "cloud-u",
                "password_hash": hash_password("cp"),
                "enabled": True,
                "updated_at": "2026-08-02T00:00:00+00:00",
            }
        ]
    )
    assert db.count_my_pick() == 1
    assert db.get_app_user("u") is None


def test_push_app_users_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = LocalDB(tmp_path / "push.db")
    db.create_app_user("sync-u", "pw", notes="n")

    captured: dict = {}

    class FakeResp:
        status_code = 200
        text = ""

    class FakeClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):  # noqa: ANN002
            return False

        def delete(self, url, **kwargs):  # noqa: ANN001
            captured["delete"] = url
            return FakeResp()

        def post(self, url, **kwargs):  # noqa: ANN001
            captured["post_url"] = url
            captured["post_body"] = json.loads(kwargs.get("content") or b"[]")
            return FakeResp()

    monkeypatch.setattr("app.sync.supabase.httpx.Client", FakeClient)
    sync = SupabaseSync("https://x.supabase.co", "anon", "service")
    n = sync.push_app_users(db)
    assert n == 1
    body = captured["post_body"]
    assert len(body) == 1
    assert body[0]["account"] == "sync-u"
    assert "password_hash" in body[0]
    assert "password" not in body[0]
    dumped = json.dumps(body)
    assert "pw" not in dumped


def test_verify_viewer_credentials_local(tmp_path: Path):
    db = LocalDB(tmp_path / "login_local.db")
    db.create_app_user("alice", "secret")
    assert verify_viewer_credentials(db, None, "alice", "secret")
    assert not verify_viewer_credentials(db, None, "alice", "wrong")
    assert not verify_viewer_credentials(db, None, "missing", "secret")


def test_verify_viewer_credentials_rpc_fallback(tmp_path: Path):
    db = LocalDB(tmp_path / "login_rpc.db")
    db.create_app_user("bob", "local-only")

    class FakeSync:
        enabled = True

        def verify_viewer_login(self, account: str, password: str) -> bool:
            return account == "cloud" and password == "ok"

        def viewer_has_accounts(self) -> bool:
            return True

    sync = FakeSync()
    assert verify_viewer_credentials(db, sync, "cloud", "ok")
    assert verify_viewer_credentials(db, sync, "bob", "local-only")
    assert not verify_viewer_credentials(db, sync, "cloud", "bad")


def test_viewer_has_accounts_rpc(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    class FakeResp:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):  # noqa: ANN002
            return False

        def post(self, url, **kwargs):  # noqa: ANN001
            calls.append(url)
            return FakeResp(True)

    monkeypatch.setattr("app.sync.supabase.httpx.Client", FakeClient)
    sync = SupabaseSync("https://x.supabase.co", "anon")
    assert sync.viewer_has_accounts()
    assert any("viewer_has_accounts" in u for u in calls)


def test_verify_viewer_login_rpc(monkeypatch: pytest.MonkeyPatch):
    class FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return [{"session_token": "session-token", "session_account": "u1"}]

    class FakeClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):  # noqa: ANN002
            return False

        def post(self, url, **kwargs):  # noqa: ANN001
            body = json.loads(kwargs.get("content") or "{}")
            assert body["p_account"] == "u1"
            assert body["p_password"] == "pw"
            assert "viewer_login_session" in url
            return FakeResp()

    monkeypatch.setattr("app.sync.supabase.httpx.Client", FakeClient)
    sync = SupabaseSync("https://x.supabase.co", "anon")
    assert sync.verify_viewer_login("u1", "pw")
    assert sync.session_token == "session-token"
