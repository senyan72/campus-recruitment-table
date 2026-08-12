"""CoachDB：本地 SQLite，专用于 AI 陪伴。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from app.coach.schema import SCHEMA_SQL
from app.config import app_data_dir
from app.timeutil import utc_now_iso


def default_coach_db_path() -> Path:
    return app_data_dir() / "coach_companion.db"


class CoachDB:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_coach_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def migrate(self) -> None:
        self._conn.executescript(SCHEMA_SQL)
        # 兼容旧库补列
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(llm_call_logs)").fetchall()}
        for col, typ in (
            ("prompt_tokens", "INTEGER"),
            ("completion_tokens", "INTEGER"),
            ("total_tokens", "INTEGER"),
        ):
            if cols and col not in cols:
                self._conn.execute(f"ALTER TABLE llm_call_logs ADD COLUMN {col} {typ}")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def new_id(prefix: str) -> str:
        return f"{prefix}{uuid.uuid4().hex[:12]}"

    @staticmethod
    def dumps(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False)

    @staticmethod
    def loads(text: str | None, default: Any = None) -> Any:
        if not text:
            return default
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return default

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def ensure_user(self, account: str) -> dict[str, Any]:
        row = self.fetchone("SELECT * FROM coach_users WHERE account=?", (account,))
        if row:
            return row
        uid = self.new_id("usr_")
        now = utc_now_iso()
        self.execute(
            "INSERT INTO coach_users(id, account, status, created_at) VALUES(?,?,?,?)",
            (uid, account, "active", now),
        )
        self.execute(
            "INSERT INTO profiles(user_id, payload_json, updated_at) VALUES(?,?,?)",
            (uid, self.dumps({}), now),
        )
        return self.fetchone("SELECT * FROM coach_users WHERE id=?", (uid,)) or {
            "id": uid,
            "account": account,
        }

    def get_profile(self, user_id: str) -> dict[str, Any] | None:
        row = self.fetchone("SELECT * FROM profiles WHERE user_id=?", (user_id,))
        if not row:
            return None
        row["payload"] = self.loads(row.pop("payload_json"), {})
        return row

    def list_confirmed_facts(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.fetchall(
            "SELECT * FROM facts WHERE user_id=? AND status='confirmed' ORDER BY created_at",
            (user_id,),
        )
        for r in rows:
            r["meta"] = self.loads(r.pop("meta_json"), {})
        return rows
