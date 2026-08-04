"""本机 SQLite 读写。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.auth.license import is_expired, normalize_expiry
from app.auth.password import hash_password, verify_password
from app.config import db_path
from app.db.schema import SCHEMA_SQL
from app.timeutil import utc_now_iso

MY_PICK_CAMPUS = "个人校招投递"


def utc_now() -> str:
    """UTC ISO；默认跟系统钟，可用 CAMPUS_JOBS_NOW 覆盖。"""
    return utc_now_iso()


def new_id() -> str:
    return str(uuid.uuid4())


class LocalDB:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.last_backup_error: str | None = None
        self._backup_existing_database()
        self._init_schema()

    @property
    def backup_dir(self) -> Path:
        """本地 SQLite 快照目录。"""
        return self.path.parent / "backups"

    def backup_to(self, target: Path) -> Path:
        """使用 SQLite 在线 backup API 创建快照。"""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(str(self.path), timeout=30.0)
        destination = sqlite3.connect(str(target), timeout=30.0)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
            source.close()
        return target

    def integrity_check(self) -> str:
        """返回 SQLite integrity_check 的结果，正常值为 ``ok``。"""
        with sqlite3.connect(str(self.path), timeout=30.0) as c:
            row = c.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"

    def restore_from(self, source: Path) -> Path:
        """从快照恢复当前数据库；调用方应先停止正在运行的采集任务。"""
        source = Path(source)
        if not source.exists() or not source.is_file():
            raise FileNotFoundError(f"备份不存在：{source}")
        if source.resolve() == self.path.resolve():
            raise ValueError("备份文件不能与当前数据库相同")
        with sqlite3.connect(str(source), timeout=30.0) as source_conn:
            check = source_conn.execute("PRAGMA integrity_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                raise sqlite3.DatabaseError(f"备份完整性检查失败：{check[0] if check else 'unknown'}")
            with sqlite3.connect(str(self.path), timeout=30.0) as destination:
                source_conn.backup(destination)
                destination.commit()
        self._init_schema()
        return self.path

    def _backup_existing_database(self) -> None:
        """首次打开当天已有数据库时创建一份快照，避免迁移/清理不可逆。"""
        try:
            if not self.path.exists() or self.path.stat().st_size <= 0:
                return
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
            target = self.backup_dir / f"{self.path.stem}-{stamp}.db"
            if not target.exists():
                self.backup_to(target)
            backups = sorted(self.backup_dir.glob(f"{self.path.stem}-*.db"), reverse=True)
            for old in backups[14:]:
                try:
                    old.unlink()
                except OSError:
                    pass
        except (OSError, sqlite3.Error) as exc:
            self.last_backup_error = str(exc)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = self._connect()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def _init_schema(self) -> None:
        with self.conn() as c:
            try:
                # WAL persists in the database file; setting it on every short-lived
                # connection needlessly competes with the deep-collection process.
                c.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
            c.executescript(SCHEMA_SQL)
            self._migrate_job_columns(c)
            self._ensure_my_pick_schema(c)
            self._ensure_app_users_schema(c)
            self._seed_blocklist(c)

    def _migrate_job_columns(self, c: sqlite3.Connection) -> None:
        """旧库补列（CREATE IF NOT EXISTS 不会加新字段）。"""
        cols = {str(r[1]) for r in c.execute("PRAGMA table_info(jobs)").fetchall()}
        if "group_name" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN group_name TEXT")
        if "education" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN education TEXT")
        if "salary_range" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN salary_range TEXT")
        if "headcount" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN headcount TEXT")
        # 季节性实习本身不等于应届生实习；仅明确应届/校园信号归应届生实习。
        c.execute(
            "UPDATE jobs SET recruit_bucket='日常实习' "
            "WHERE recruit_bucket IN ('暑期实习', '暑假实习', '寒假实习')"
        )
        c.execute(
            "UPDATE jobs SET recruit_bucket='应届生实习' "
            "WHERE recruit_bucket IN ('日常实习', '应届实习') AND ("
            "recruit_project LIKE '%应届%实习%' OR recruit_project LIKE '%校招%实习%' "
            "OR recruit_project LIKE '%校园%实习%' OR recruit_project LIKE '%毕业%实习%' "
            "OR title LIKE '%届%实习%' OR title LIKE '%实习%届%'"
            ")"
        )

    def _ensure_my_pick_schema(self, c: sqlite3.Connection) -> None:
        """旧库补建 Viewer 本机个人选取表（不同步云端）。"""
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS my_pick (
                job_id TEXT NOT NULL,
                collection TEXT NOT NULL DEFAULT '个人校招投递',
                added_at TEXT NOT NULL,
                PRIMARY KEY (job_id, collection),
                FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_my_pick_collection ON my_pick(collection);
            """
        )

    def _seed_blocklist(self, c: sqlite3.Connection) -> None:
        defaults = [
            ("汇总", "二手汇总特征"),
            ("合集", "二手汇总特征"),
            ("信息差", "二手汇总特征"),
            ("海投", "二手汇总特征"),
            ("每日岗", "二手汇总特征"),
            ("整理了", "二手汇总特征"),
            ("offer研习社", "二手汇总来源"),
            ("校招汇总", "二手汇总特征"),
        ]
        for pattern, note in defaults:
            c.execute(
                "INSERT OR IGNORE INTO aggregator_blocklist(pattern, note) VALUES (?, ?)",
                (pattern, note),
            )

    # ---- meta ----
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.conn() as c:
            row = c.execute("SELECT value FROM sync_meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO sync_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ---- companies ----
    def upsert_company(self, data: dict[str, Any]) -> str:
        now = utc_now()
        name = (data.get("name") or "").strip()
        name_norm = data.get("name_norm") or normalize_company_name(name)
        with self.conn() as c:
            existing = c.execute(
                "SELECT id, hint_apply_urls, career_urls, source_sheets, verify_status "
                "FROM companies WHERE name_norm=?",
                (name_norm,),
            ).fetchone()
            if existing:
                cid = existing["id"]
                hints = _merge_json_list(existing["hint_apply_urls"], data.get("hint_apply_urls"))
                careers = _merge_json_list(existing["career_urls"], data.get("career_urls"))
                sheets = _merge_json_list(existing["source_sheets"], data.get("source_sheets"))
                # 不降级：official/rejected 不被 re-import 的 unverified 覆盖
                new_status = (data.get("verify_status") or "").strip()
                old_status = (existing["verify_status"] or "").strip()
                if old_status in ("official", "rejected") and new_status == "unverified":
                    new_status = ""
                c.execute(
                    """
                    UPDATE companies SET
                        name=COALESCE(NULLIF(?, ''), name),
                        company_nature=COALESCE(NULLIF(?, ''), company_nature),
                        industry=COALESCE(NULLIF(?, ''), industry),
                        hint_apply_urls=?,
                        career_urls=?,
                        wechat_name=COALESCE(NULLIF(?, ''), wechat_name),
                        verify_status=COALESCE(NULLIF(?, ''), verify_status),
                        source_sheets=?,
                        notes=COALESCE(NULLIF(?, ''), notes),
                        updated_at=?
                    WHERE id=?
                    """,
                    (
                        name,
                        data.get("company_nature") or "",
                        data.get("industry") or "",
                        json.dumps(hints, ensure_ascii=False),
                        json.dumps(careers, ensure_ascii=False),
                        data.get("wechat_name") or "",
                        new_status,
                        json.dumps(sheets, ensure_ascii=False),
                        data.get("notes") or "",
                        now,
                        cid,
                    ),
                )
                return cid

            cid = data.get("id") or new_id()
            c.execute(
                """
                INSERT INTO companies(
                    id, name, name_norm, company_nature, industry,
                    hint_apply_urls, career_urls, wechat_name, verify_status,
                    source_sheets, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cid,
                    name,
                    name_norm,
                    data.get("company_nature"),
                    data.get("industry"),
                    json.dumps(data.get("hint_apply_urls") or [], ensure_ascii=False),
                    json.dumps(data.get("career_urls") or [], ensure_ascii=False),
                    data.get("wechat_name"),
                    data.get("verify_status") or "unverified",
                    json.dumps(data.get("source_sheets") or [], ensure_ascii=False),
                    data.get("notes"),
                    now,
                    now,
                ),
            )
            return cid

    def count_companies(self) -> int:
        with self.conn() as c:
            return int(c.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"])

    def get_company(self, company_id: str) -> dict[str, Any] | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
            return dict(row) if row else None

    def list_companies(
        self, verify_status: str | None = None, limit: int = 500, offset: int = 0
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM companies"
        params: list[Any] = []
        if verify_status:
            sql += " WHERE verify_status=?"
            params.append(verify_status)
        sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def list_companies_with_urls(
        self,
        *,
        limit: int = 100,
        prefer_ats: bool = True,
        verify_status: str | None = None,
        include_rejected: bool = False,
    ) -> list[dict[str, Any]]:
        """优先返回带 hint/career URL 的公司（ATS 优先），供探测与官网采集。

        limit<=0 表示不限制条数（夜间全量复检用）。
        """
        where = [
            "("
            "(career_urls IS NOT NULL AND career_urls NOT IN ('', '[]'))"
            " OR (hint_apply_urls IS NOT NULL AND hint_apply_urls NOT IN ('', '[]'))"
            ")"
        ]
        params: list[Any] = []
        if verify_status:
            where.append("verify_status=?")
            params.append(verify_status)
        elif not include_rejected:
            where.append("verify_status != 'rejected'")
        order = "ORDER BY "
        if prefer_ats:
            order += (
                "CASE WHEN ("
                "hint_apply_urls LIKE '%mokahr%' OR hint_apply_urls LIKE '%zhiye%' OR "
                "hint_apply_urls LIKE '%feishu%' OR hint_apply_urls LIKE '%hotjob%' OR "
                "hint_apply_urls LIKE '%wecruit%' OR hint_apply_urls LIKE '%italent%' OR "
                "career_urls LIKE '%mokahr%' OR career_urls LIKE '%zhiye%' OR "
                "career_urls LIKE '%feishu%' OR career_urls LIKE '%hotjob%'"
                ") THEN 0 ELSE 1 END, "
            )
        order += (
            "CASE WHEN career_urls IS NOT NULL AND career_urls NOT IN ('', '[]') THEN 0 ELSE 1 END, "
            "CASE WHEN verify_status='official' THEN 0 ELSE 1 END, "
            "updated_at DESC "
        )
        sql = f"SELECT * FROM companies WHERE {' AND '.join(where)} {order}"
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def update_company_verify(self, company_id: str, status: str, career_urls: list[str] | None = None) -> None:
        with self.conn() as c:
            if career_urls is not None:
                c.execute(
                    "UPDATE companies SET verify_status=?, career_urls=?, updated_at=? WHERE id=?",
                    (status, json.dumps(career_urls, ensure_ascii=False), utc_now(), company_id),
                )
            else:
                c.execute(
                    "UPDATE companies SET verify_status=?, updated_at=? WHERE id=?",
                    (status, utc_now(), company_id),
                )

    def set_company_nature(self, company_id: str, nature: str) -> int:
        """写入企业性质，并同步到该企业已有岗位。"""
        from app.collector.filters import clean_company_nature

        cleaned = clean_company_nature(nature)
        if not cleaned:
            return 0
        now = utc_now()
        with self.conn() as c:
            c.execute(
                "UPDATE companies SET company_nature=?, updated_at=? WHERE id=?",
                (cleaned, now, company_id),
            )
            cur = c.execute(
                "UPDATE jobs SET company_nature=?, updated_at=? "
                "WHERE company_id=? AND COALESCE(company_nature, '')=''",
                (cleaned, now, company_id),
            )
            return int(cur.rowcount or 0)

    def find_company_id(
        self, *, company_id: str | None = None, company_name: str | None = None
    ) -> str | None:
        """按 id 或归一化公司名解析 companies.id。"""
        cid = (company_id or "").strip()
        if cid:
            row = self.get_company(cid)
            if row:
                return cid
        name = (company_name or "").strip()
        if not name:
            return None
        name_norm = normalize_company_name(name)
        if not name_norm:
            return None
        with self.conn() as c:
            row = c.execute(
                "SELECT id FROM companies WHERE name_norm=? LIMIT 1",
                (name_norm,),
            ).fetchone()
            return row["id"] if row else None

    def sync_company_seed_urls(
        self,
        company_id: str,
        *,
        replacements: list[tuple[str | None, str | None]] | None = None,
        add_urls: list[str] | None = None,
        set_official: bool = False,
    ) -> dict[str, Any]:
        """用人工确认/修正的 URL 覆盖公司种子链接（career_urls / hint_apply_urls）。

        - replacements: (旧 URL, 新 URL)；旧链在种子列表中则替换，避免下次再采坏链
        - add_urls: 直接并入种子（去重）
        - ATS/校招类新链优先写入 career_urls，其余写入 hint_apply_urls
        不新建公司行；company_id 无效时返回 synced=False。
        """
        from app.collector.filters import (
            is_campus_apply_url,
            is_trusted_ats_url,
            looks_like_url,
            normalize_url_for_dedupe,
            parse_url_list,
        )

        cid = (company_id or "").strip()
        company = self.get_company(cid) if cid else None
        if not company:
            return {"synced": False, "reason": "no_company", "company_id": cid or None}

        reps: list[tuple[str, str]] = []
        for old, new in replacements or []:
            o = (old or "").strip()
            n = (new or "").strip()
            if not o and not n:
                continue
            if o and n and (normalize_url_for_dedupe(o) or o.lower()) == (
                normalize_url_for_dedupe(n) or n.lower()
            ):
                continue
            reps.append((o, n))

        extras = [(u or "").strip() for u in (add_urls or []) if looks_like_url(u)]

        if not reps and not extras and not set_official:
            return {
                "synced": False,
                "reason": "no_url_change",
                "company_id": cid,
            }

        old_careers = parse_url_list(company.get("career_urls"))
        old_hints = parse_url_list(company.get("hint_apply_urls"))
        careers = _apply_seed_url_replacements(old_careers, reps)
        hints = _apply_seed_url_replacements(old_hints, reps)

        # 所有有效新链写入 hint；ATS/校招链同时提升到 career_urls
        promote: list[str] = []
        for _, new in reps:
            if looks_like_url(new):
                promote.append(new)
        promote.extend(extras)
        for url in promote:
            hints = _dedupe_seed_urls(hints + [url])
            if is_trusted_ats_url(url) or is_campus_apply_url(url):
                careers = _dedupe_seed_urls(careers + [url])

        careers = _dedupe_seed_urls(careers)
        hints = _dedupe_seed_urls(hints)

        status = (company.get("verify_status") or "unverified").strip()
        if set_official:
            status = "official"

        changed = (
            careers != old_careers
            or hints != old_hints
            or (set_official and status != (company.get("verify_status") or "").strip())
        )
        if not changed:
            return {
                "synced": False,
                "reason": "unchanged",
                "company_id": cid,
                "career_urls": careers,
                "hint_apply_urls": hints,
            }

        now = utc_now()
        with self.conn() as c:
            c.execute(
                """
                UPDATE companies SET
                    career_urls=?,
                    hint_apply_urls=?,
                    verify_status=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    json.dumps(careers, ensure_ascii=False),
                    json.dumps(hints, ensure_ascii=False),
                    status,
                    now,
                    cid,
                ),
            )
        return {
            "synced": True,
            "company_id": cid,
            "career_urls": careers,
            "hint_apply_urls": hints,
            "verify_status": status,
            "replacements": [(o, n) for o, n in reps],
            "added": extras,
        }

    def sync_seed_urls_from_job_fields(
        self,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        *,
        set_official: bool = False,
    ) -> dict[str, Any]:
        """岗位/异常队列保存后：若 source_url / apply_url 有变，同步到关联公司种子。"""
        from app.collector.filters import looks_like_url, normalize_url_for_dedupe

        prev = before or {}
        nxt = after or {}
        company_id = self.find_company_id(
            company_id=nxt.get("company_id") or prev.get("company_id"),
            company_name=nxt.get("company") or prev.get("company"),
        )
        if not company_id:
            return {"synced": False, "reason": "no_company"}

        replacements: list[tuple[str | None, str | None]] = []
        for key in ("source_url", "apply_url"):
            old_u = (prev.get(key) or "").strip()
            new_u = (nxt.get(key) or "").strip()
            old_key = normalize_url_for_dedupe(old_u) if old_u else ""
            new_key = normalize_url_for_dedupe(new_u) if new_u else ""
            if old_key == new_key:
                continue
            if not old_u and not looks_like_url(new_u):
                continue
            if new_u and not looks_like_url(new_u):
                continue
            replacements.append((old_u or None, new_u or None))

        if not replacements and not set_official:
            return {"synced": False, "reason": "no_url_change", "company_id": company_id}

        return self.sync_company_seed_urls(
            company_id,
            replacements=replacements,
            set_official=set_official,
        )

    # ---- jobs ----
    def _find_job_for_upsert(self, c: sqlite3.Connection, data: dict[str, Any]) -> str | None:
        """按公司、岗位 URL 和标题查找已有岗位。

        同标题但不同详情 URL 必须保留为多行；无独立 URL 时才按标题回退。
        """
        from app.collector.filters import (
            is_noise_title,
            is_portal_shell_record,
            normalize_job_identity_url,
            normalize_job_title,
        )

        source_url = (data.get("source_url") or "").strip()
        title = (data.get("title") or "").strip()
        company_id = data.get("company_id")
        company = (data.get("company") or "").strip()
        apply_url = (data.get("apply_url") or source_url or "").strip()
        norm_url = normalize_job_identity_url(apply_url) or normalize_job_identity_url(source_url)
        norm_title = normalize_job_title(title)
        if not company_id and not company:
            existing = c.execute(
                "SELECT id FROM jobs WHERE source_url=? AND title=?",
                (source_url, title),
            ).fetchone()
            return existing["id"] if existing else None

        if company_id:
            rows = c.execute(
                "SELECT id, source_url, apply_url, title, jd_text FROM jobs "
                "WHERE company_id=?",
                (company_id,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id, source_url, apply_url, title, jd_text FROM jobs "
                "WHERE company=?",
                (company,),
            ).fetchall()

        for r in rows:
            r_title_norm = normalize_job_title(r["title"])
            r_url = normalize_job_identity_url(r["apply_url"] or "") or normalize_job_identity_url(
                r["source_url"] or ""
            )
            # 同 URL：仅当标题相同，或已有/新标题为壳（用真实岗覆盖壳）
            if norm_url and r_url and norm_url == r_url:
                if norm_title and r_title_norm and norm_title == r_title_norm:
                    return r["id"]
                old_shell = is_noise_title(r["title"]) or is_portal_shell_record(
                    title=r["title"], jd_text=r["jd_text"], source_url=r["source_url"]
                )
                new_shell = is_noise_title(title) or is_portal_shell_record(
                    title=title, jd_text=data.get("jd_text"), source_url=source_url
                )
                if old_shell or new_shell:
                    return r["id"]
                # 同链但不同真实岗位名：保留为多行（不合并）
                continue
            # 一侧缺少独立 URL 时才按标题回退；不同详情 URL 的同名岗必须并存。
            if (
                norm_title
                and r_title_norm
                and norm_title == r_title_norm
                and (not norm_url or not r_url)
            ):
                return r["id"]
        return None

    @staticmethod
    def _norm_job_cmp(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, float):
            return f"{value:.6f}"
        return str(value).strip()

    def upsert_job(self, data: dict[str, Any]) -> str:
        jid, _action = self.upsert_job_with_action(data)
        return jid

    def upsert_job_with_action(self, data: dict[str, Any]) -> tuple[str, str]:
        """写入岗位。返回 (id, action)，action 为 inserted|updated|unchanged。

        稳定身份：同公司+岗位 URL；缺少独立 URL 时回退到归一化标题。
        字段完全一致时不 UPDATE，避免复检无意义刷写。
        """
        now = utc_now()
        source_url = (data.get("source_url") or "").strip()
        title = (data.get("title") or "").strip()
        with self.conn() as c:
            existing_id = data.get("id")
            if existing_id:
                row = c.execute("SELECT id FROM jobs WHERE id=?", (existing_id,)).fetchone()
                if not row:
                    existing_id = None
            if not existing_id:
                existing_id = self._find_job_for_upsert(c, data)
            if not existing_id and source_url and title and data.get("apply_url"):
                collision = c.execute(
                    "SELECT apply_url FROM jobs WHERE source_url=? AND title=? LIMIT 1",
                    (source_url, title),
                ).fetchone()
                if collision:
                    from app.collector.filters import normalize_job_identity_url

                    new_apply_key = normalize_job_identity_url(data.get("apply_url"))
                    old_apply_key = normalize_job_identity_url(collision["apply_url"])
                    if new_apply_key and new_apply_key != old_apply_key:
                        # Preserve same-title jobs from one listing page without changing
                        # the existing local/cloud UNIQUE(source_url, title) contract.
                        source_url = str(data.get("apply_url") or source_url).strip()
            tags = data.get("job_tags") or []
            if isinstance(tags, str):
                tags_json = tags
            else:
                tags_json = json.dumps(tags, ensure_ascii=False)
            new_group = (data.get("group_name") or "").strip() or None
            new_company = data.get("company") or ""
            new_project = data.get("recruit_project")
            from app.collector.filters import normalize_recruit_bucket

            new_bucket = normalize_recruit_bucket(
                data.get("recruit_bucket"),
                recruit_project=data.get("recruit_project"),
                title=title,
            )
            new_nature = data.get("company_nature")
            new_apply = data.get("apply_url")
            new_deadline = data.get("deadline")
            new_loc = data.get("work_location")
            new_industry = data.get("industry")
            new_edu = data.get("education")
            new_salary = data.get("salary_range")
            new_headcount = data.get("headcount")
            new_open = data.get("open_at")
            new_batch = data.get("graduation_batch")
            new_jd = data.get("jd_text")
            new_raw = data.get("raw_category")
            new_parse = data.get("parse_status") or "ok"
            new_conf = float(data.get("confidence") or 0.0)
            # 采集默认进「岗位审核」；显式传入 status 时尊重调用方
            new_status = data.get("status") or "pending_review"
            new_cloud = data.get("cloud_updated_at")
            if existing_id:
                # 已审核进「岗位显示」的 active 不被复检降回 pending_review
                row_pre = c.execute(
                    "SELECT status FROM jobs WHERE id=?", (existing_id,)
                ).fetchone()
                if (
                    row_pre
                    and (row_pre["status"] or "") == "active"
                    and new_status == "pending_review"
                ):
                    new_status = "active"
            fields = (
                data.get("company_id"),
                new_group,
                new_company,
                new_project,
                new_bucket,
                new_nature,
                title,
                source_url,
                new_apply,
                new_deadline,
                new_loc,
                new_industry,
                new_edu,
                new_salary,
                new_headcount,
                new_open,
                new_batch,
                new_jd,
                tags_json,
                new_raw,
                new_parse,
                new_conf,
                new_status,
                new_cloud,
                now,
            )
            if existing_id:
                jid = existing_id
                row = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
                if row:
                    # jd_text 与 UPDATE 一致：新值为空时保留旧值
                    effective_jd = new_jd if new_jd not in (None, "") else row["jd_text"]
                    row_group = ""
                    try:
                        row_group = row["group_name"] or ""
                    except (IndexError, KeyError):
                        row_group = ""
                    same = (
                        self._norm_job_cmp(row_group) == self._norm_job_cmp(new_group)
                        and self._norm_job_cmp(row["company"]) == self._norm_job_cmp(new_company)
                        and self._norm_job_cmp(row["recruit_project"])
                        == self._norm_job_cmp(new_project)
                        and self._norm_job_cmp(row["recruit_bucket"])
                        == self._norm_job_cmp(new_bucket)
                        and self._norm_job_cmp(row["company_nature"])
                        == self._norm_job_cmp(new_nature)
                        and self._norm_job_cmp(row["title"]) == self._norm_job_cmp(title)
                        and self._norm_job_cmp(row["source_url"])
                        == self._norm_job_cmp(source_url)
                        and self._norm_job_cmp(row["apply_url"])
                        == self._norm_job_cmp(new_apply)
                        and self._norm_job_cmp(row["deadline"])
                        == self._norm_job_cmp(new_deadline)
                        and self._norm_job_cmp(row["work_location"])
                        == self._norm_job_cmp(new_loc)
                        and self._norm_job_cmp(row["industry"])
                        == self._norm_job_cmp(new_industry)
                        and self._norm_job_cmp(row["education"])
                        == self._norm_job_cmp(new_edu)
                        and self._norm_job_cmp(row["salary_range"])
                        == self._norm_job_cmp(new_salary)
                        and self._norm_job_cmp(row["headcount"])
                        == self._norm_job_cmp(new_headcount)
                        and self._norm_job_cmp(row["open_at"]) == self._norm_job_cmp(new_open)
                        and self._norm_job_cmp(row["graduation_batch"])
                        == self._norm_job_cmp(new_batch)
                        and self._norm_job_cmp(row["jd_text"]) == self._norm_job_cmp(effective_jd)
                        and self._norm_job_cmp(row["job_tags"]) == self._norm_job_cmp(tags_json)
                        and self._norm_job_cmp(row["raw_category"])
                        == self._norm_job_cmp(new_raw)
                        and self._norm_job_cmp(row["parse_status"])
                        == self._norm_job_cmp(new_parse)
                        and abs(float(row["confidence"] or 0) - new_conf) < 1e-9
                        and self._norm_job_cmp(row["status"]) == self._norm_job_cmp(new_status)
                    )
                    if same:
                        return jid, "unchanged"
                c.execute(
                    """
                    UPDATE jobs SET
                        company_id=COALESCE(?, company_id),
                        group_name=?,
                        company=?, recruit_project=?, recruit_bucket=?, company_nature=?,
                        title=?, source_url=?, apply_url=?, deadline=?, work_location=?,
                        industry=?, education=?, salary_range=?, headcount=?,
                        open_at=?, graduation_batch=?,
                        jd_text=COALESCE(?, jd_text), job_tags=?, raw_category=?,
                        parse_status=?, confidence=?, status=?,
                        cloud_updated_at=COALESCE(?, cloud_updated_at),
                        updated_at=?
                    WHERE id=?
                    """,
                    fields + (jid,),
                )
                return jid, "updated"
            jid = data.get("id") or new_id()
            c.execute(
                """
                INSERT INTO jobs(
                    id, company_id, group_name, company, recruit_project, recruit_bucket,
                    company_nature, title, source_url, apply_url, deadline, work_location,
                    industry, education, salary_range, headcount, open_at, graduation_batch,
                    jd_text, job_tags, raw_category, parse_status, confidence, status,
                    cloud_updated_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (jid,) + fields[:-1] + (now, now),
            )
            return jid, "inserted"

    def soft_delete_jobs(self, job_ids: list[str]) -> int:
        """将岗位标记为 deleted（软删），供清理噪声/重复。"""
        if not job_ids:
            return 0
        now = utc_now()
        n = 0
        with self.conn() as c:
            for jid in job_ids:
                cur = c.execute(
                    "UPDATE jobs SET status='deleted', updated_at=? WHERE id=? AND status!='deleted'",
                    (now, jid),
                )
                n += cur.rowcount
        return n

    def _cloud_revoke_ids(self) -> list[str]:
        raw = self.get_meta("cloud_revoke_ids") or "[]"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        return [str(x) for x in data if x]

    def _set_cloud_revoke_ids(self, ids: list[str]) -> None:
        uniq = list(dict.fromkeys(str(i) for i in ids if i))
        self.set_meta("cloud_revoke_ids", json.dumps(uniq, ensure_ascii=False))

    def add_cloud_revoke_ids(self, job_ids: list[str]) -> int:
        """登记下次推送时以 deleted 覆盖云端（反审核已推送岗）。"""
        if not job_ids:
            return 0
        cur = self._cloud_revoke_ids()
        before = len(cur)
        for jid in job_ids:
            if jid and jid not in cur:
                cur.append(str(jid))
        self._set_cloud_revoke_ids(cur)
        return len(cur) - before

    def remove_cloud_revoke_ids(self, job_ids: list[str]) -> None:
        if not job_ids:
            return
        drop = {str(i) for i in job_ids if i}
        self._set_cloud_revoke_ids([i for i in self._cloud_revoke_ids() if i not in drop])

    def list_cloud_revoke_jobs(self, limit: int = 2000) -> list[dict[str, Any]]:
        """待撤下云端的岗位快照（推送时 status 改为 deleted，不改本地）。"""
        ids = self._cloud_revoke_ids()[: max(0, int(limit or 0))]
        if not ids:
            return []
        out: list[dict[str, Any]] = []
        for job in self.get_jobs_by_ids(ids):
            # 仅本地仍非 active 的才撤（已再次审核的会从名单移除）
            if (job.get("status") or "") == "active":
                continue
            payload = dict(job)
            payload["status"] = "deleted"
            out.append(payload)
        return out

    def revert_jobs_to_pending_review(self, job_ids: list[str]) -> dict[str, Any]:
        """反审核：active → pending_review，清空 cloud_updated_at；曾推送过的记入撤云名单。"""
        if not job_ids:
            return {"reverted": 0, "revoke_queued": 0}
        now = utc_now()
        reverted = 0
        revoke: list[str] = []
        with self.conn() as c:
            for jid in job_ids:
                row = c.execute(
                    "SELECT id, status, cloud_updated_at FROM jobs WHERE id=?",
                    (jid,),
                ).fetchone()
                if not row or (row["status"] or "") != "active":
                    continue
                had_cloud = bool((row["cloud_updated_at"] or "").strip())
                cur = c.execute(
                    """
                    UPDATE jobs
                    SET status='pending_review', cloud_updated_at=NULL, updated_at=?
                    WHERE id=? AND status='active'
                    """,
                    (now, jid),
                )
                if cur.rowcount:
                    reverted += 1
                    if had_cloud:
                        revoke.append(str(jid))
        queued = self.add_cloud_revoke_ids(revoke) if revoke else 0
        return {"reverted": reverted, "revoke_queued": queued, "revoke_ids": revoke}

    def get_jobs_by_ids(self, job_ids: list[str]) -> list[dict[str, Any]]:
        if not job_ids:
            return []
        out: list[dict[str, Any]] = []
        with self.conn() as c:
            for jid in job_ids:
                row = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
                if row:
                    out.append(dict(row))
        return out

    def job_identity_keys_for_company(
        self, company_name: str, company_id: str | None = None
    ) -> tuple[set[str], set[str]]:
        """Return durable identities from jobs and pending review rows for one company."""
        from app.collector.filters import normalize_job_identity_url

        name = (company_name or "").strip()
        if not name:
            return set(), set()
        urls: set[str] = set()
        titles: set[str] = set()

        def add_identity(item: dict[str, Any]) -> None:
            for key in ("apply_url", "source_url"):
                uk = normalize_job_identity_url(item.get(key))
                if uk:
                    urls.add(uk)
            title = str(item.get("title") or "").strip().lower()
            if title:
                titles.add(title)

        with self.conn() as c:
            rows = c.execute(
                """
                SELECT apply_url, source_url, title FROM jobs
                WHERE company = ?
                  AND status IN ('pending_review', 'active', 'abnormal')
                """,
                (name,),
            ).fetchall()
            review_params: list[Any] = [name]
            review_company = "json_extract(payload, '$.company') = ?"
            if company_id:
                review_company = (
                    f"({review_company} OR json_extract(payload, '$.company_id') = ?)"
                )
                review_params.append(str(company_id))
            review_rows = c.execute(
                "SELECT payload FROM review_queue "
                "WHERE status='pending' AND json_valid(payload) "
                f"AND {review_company}",
                review_params,
            ).fetchall()
        for row in rows:
            add_identity(dict(row))
        for row in review_rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict):
                add_identity(payload)
        return urls, titles

    def list_jobs(
        self,
        *,
        bucket: str | None = None,
        industry: str | None = None,
        company_nature: str | None = None,
        location: str | None = None,
        keyword: str | None = None,
        graduation_batch: str | None = None,
        target_or_intern: bool = False,
        my_pick_only: bool = False,
        my_pick_collection: str = MY_PICK_CAMPUS,
        status: str | list[str] = "active",
        limit: int = 2000,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if isinstance(status, (list, tuple, set)):
            statuses = [str(s) for s in status if s]
            if not statuses:
                statuses = ["active"]
            placeholders = ",".join("?" * len(statuses))
            clauses = [f"status IN ({placeholders})"]
            params: list[Any] = list(statuses)
        else:
            clauses = ["status=?"]
            params = [status]
        if bucket and bucket != "全部":
            clauses.append("recruit_bucket=?")
            params.append(bucket)
        if industry:
            clauses.append("industry LIKE ?")
            params.append(f"%{industry}%")
        if company_nature:
            clauses.append("company_nature=?")
            params.append(company_nature)
        if location:
            clauses.append("work_location LIKE ?")
            params.append(f"%{location}%")
        if target_or_intern:
            from app.collector.filters import current_grad_batch

            target = current_grad_batch()
            clauses.append(
                "(recruit_bucket IN (?, ?) OR graduation_batch=? OR title LIKE ? OR title LIKE ?)"
            )
            year = target.replace("届", "")
            params.extend(
                ["应届生实习", "日常实习", target, f"%{target}%", f"%{year}应届%"]
            )
        elif graduation_batch:
            clauses.append("graduation_batch LIKE ?")
            params.append(f"%{graduation_batch}%")
        if keyword:
            clauses.append(
                "(company LIKE ? OR title LIKE ? OR recruit_project LIKE ? OR graduation_batch LIKE ?)"
            )
            kw = f"%{keyword}%"
            params.extend([kw, kw, kw, kw])
        if my_pick_only:
            clauses.append(
                "id IN (SELECT job_id FROM my_pick WHERE collection=?)"
            )
            params.append(my_pick_collection or MY_PICK_CAMPUS)
        sql = f"SELECT * FROM jobs WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self.conn() as c:
            rows = [dict(r) for r in c.execute(sql, params).fetchall()]
        # 附加本机投递状态
        if not rows:
            return rows
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        with self.conn() as c:
            statuses = {
                r["job_id"]: r
                for r in c.execute(
                    f"SELECT * FROM my_status WHERE job_id IN ({placeholders})", ids
                ).fetchall()
            }
        for r in rows:
            st = statuses.get(r["id"])
            r["my_apply_status"] = st["apply_status"] if st else "未投递"
            r["my_note"] = st["note"] if st else ""
        return rows

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            st = c.execute("SELECT * FROM my_status WHERE job_id=?", (job_id,)).fetchone()
            d["my_apply_status"] = st["apply_status"] if st else "未投递"
            d["my_note"] = st["note"] if st else ""
            return d

    def list_company_jobs_updated_since(
        self,
        company_id: str,
        since: str,
        *,
        status: str = "pending_review",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return jobs touched during one company collection slice."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM jobs WHERE company_id=? AND status=? AND updated_at>=? "
                "ORDER BY updated_at DESC LIMIT ?",
                (company_id, status, since, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_jobs(self, status: str | list[str] = "active") -> int:
        with self.conn() as c:
            if isinstance(status, (list, tuple, set)):
                statuses = [str(s) for s in status if s]
                if not statuses:
                    return 0
                placeholders = ",".join("?" * len(statuses))
                return int(
                    c.execute(
                        f"SELECT COUNT(*) AS n FROM jobs WHERE status IN ({placeholders})",
                        list(statuses),
                    ).fetchone()["n"]
                )
            return int(
                c.execute("SELECT COUNT(*) AS n FROM jobs WHERE status=?", (status,)).fetchone()["n"]
            )

    def mark_jobs_cloud_synced(self, job_ids: list[str], *, synced_at: str | None = None) -> int:
        """推送成功后标记本地 cloud_updated_at，供「云端上传进度」展示。"""
        if not job_ids:
            return 0
        when = synced_at or utc_now()
        n = 0
        with self.conn() as c:
            for jid in job_ids:
                cur = c.execute(
                    "UPDATE jobs SET cloud_updated_at=? WHERE id=?",
                    (when, jid),
                )
                n += cur.rowcount
        return n

    def set_my_status(self, job_id: str, apply_status: str, note: str | None = None) -> None:
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO my_status(job_id, apply_status, note, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    apply_status=excluded.apply_status,
                    note=COALESCE(excluded.note, my_status.note),
                    updated_at=excluded.updated_at
                """,
                (job_id, apply_status, note, utc_now()),
            )

    def add_my_pick(
        self,
        job_ids: list[str],
        *,
        collection: str = MY_PICK_CAMPUS,
    ) -> int:
        """加入 Viewer 本机个人选取；仅写本地 SQLite，不同步云端。"""
        if not job_ids:
            return 0
        when = utc_now()
        coll = collection or MY_PICK_CAMPUS
        added = 0
        with self.conn() as c:
            for jid in job_ids:
                if not jid:
                    continue
                cur = c.execute(
                    """
                    INSERT INTO my_pick(job_id, collection, added_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(job_id, collection) DO NOTHING
                    """,
                    (jid, coll, when),
                )
                added += cur.rowcount
        return added

    def remove_my_pick(
        self,
        job_ids: list[str],
        *,
        collection: str = MY_PICK_CAMPUS,
    ) -> int:
        if not job_ids:
            return 0
        coll = collection or MY_PICK_CAMPUS
        placeholders = ",".join("?" * len(job_ids))
        with self.conn() as c:
            cur = c.execute(
                f"DELETE FROM my_pick WHERE collection=? AND job_id IN ({placeholders})",
                [coll, *job_ids],
            )
            return int(cur.rowcount or 0)

    def list_my_pick_job_ids(self, *, collection: str = MY_PICK_CAMPUS) -> list[str]:
        coll = collection or MY_PICK_CAMPUS
        with self.conn() as c:
            rows = c.execute(
                "SELECT job_id FROM my_pick WHERE collection=? ORDER BY added_at DESC",
                (coll,),
            ).fetchall()
        return [str(r["job_id"]) for r in rows]

    def count_my_pick(self, *, collection: str = MY_PICK_CAMPUS) -> int:
        coll = collection or MY_PICK_CAMPUS
        with self.conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM my_pick WHERE collection=?",
                (coll,),
            ).fetchone()
        return int(row["n"] if row else 0)

    # ---- queues ----
    def enqueue_review(self, kind: str, payload: dict[str, Any], reason: str) -> str:
        rid = new_id()
        with self.conn() as c:
            c.execute(
                "INSERT INTO review_queue(id, kind, payload, reason, status, created_at) VALUES (?,?,?,?,?,?)",
                (rid, kind, json.dumps(payload, ensure_ascii=False), reason, "pending", utc_now()),
            )
        return rid

    def list_review_queue(self, status: str = "pending", limit: int = 200) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM review_queue WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d["payload"])
            except json.JSONDecodeError:
                pass
            result.append(d)
        return result

    def list_company_reviews_since(
        self,
        company_id: str,
        since: str,
        *,
        kinds: set[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return recent review payloads for one company without scanning the full queue."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM review_queue WHERE status='pending' AND created_at>=? "
                "ORDER BY created_at DESC LIMIT ?",
                (since, max(1, int(limit) * 4)),
            ).fetchall()
        result: list[dict[str, Any]] = []
        allowed = set(kinds or ())
        for row in rows:
            item = dict(row)
            if allowed and str(item.get("kind") or "") not in allowed:
                continue
            try:
                payload = json.loads(item.get("payload") or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if str(payload.get("company_id") or "") != str(company_id):
                continue
            item["payload"] = payload
            result.append(item)
            if len(result) >= limit:
                break
        return result

    def resolve_review(self, review_id: str, status: str = "resolved") -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE review_queue SET status=?, resolved_at=? WHERE id=?",
                (status, utc_now(), review_id),
            )

    def clear_review_queue(self, status: str = "pending") -> int:
        """清空指定状态的异常队列（默认 pending），返回删除条数。"""
        with self.conn() as c:
            cur = c.execute("DELETE FROM review_queue WHERE status=?", (status,))
            return int(cur.rowcount or 0)

    def update_review_payload(
        self, review_id: str, payload: dict[str, Any], reason: str | None = None
    ) -> bool:
        """更新异常队列条目的 payload（人工编辑后保存）。"""
        with self.conn() as c:
            row = c.execute(
                "SELECT id FROM review_queue WHERE id=? AND status='pending'",
                (review_id,),
            ).fetchone()
            if not row:
                return False
            if reason is None:
                c.execute(
                    "UPDATE review_queue SET payload=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), review_id),
                )
            else:
                c.execute(
                    "UPDATE review_queue SET payload=?, reason=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), reason, review_id),
                )
            return True

    def get_review_item(self, review_id: str) -> dict[str, Any] | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM review_queue WHERE id=?", (review_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["payload"] = json.loads(d["payload"])
        except (json.JSONDecodeError, TypeError):
            pass
        return d

    def enqueue_source_verify(
        self, company_id: str | None, source_type: str, source_value: str, reason: str
    ) -> str:
        """写入源验证队列；同公司+同源值的 pending 不重复插入。"""
        with self.conn() as c:
            if company_id and source_value:
                existing = c.execute(
                    "SELECT id FROM source_verify_queue "
                    "WHERE status='pending' AND company_id=? AND source_value=? LIMIT 1",
                    (company_id, source_value),
                ).fetchone()
                if existing:
                    return str(existing["id"])
            sid = new_id()
            c.execute(
                """
                INSERT INTO source_verify_queue(id, company_id, source_type, source_value, reason, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (sid, company_id, source_type, source_value, reason, utc_now()),
            )
        return sid

    def list_source_verify(self, status: str = "pending", limit: int = 200) -> list[dict[str, Any]]:
        with self.conn() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM source_verify_queue WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            ]

    def resolve_source_verify(self, item_id: str, status: str = "resolved") -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE source_verify_queue SET status=?, resolved_at=? WHERE id=?",
                (status, utc_now(), item_id),
            )

    def update_source_verify(
        self,
        item_id: str,
        *,
        source_value: str | None = None,
        source_type: str | None = None,
        reason: str | None = None,
        company_id: str | None = None,
        update_company_id: bool = False,
    ) -> bool:
        """更新源验证队列字段。返回是否实际更新到一行。

        company_id 仅在 update_company_id=True 时写入（允许置空）。
        """
        sets: list[str] = []
        params: list[Any] = []
        if source_value is not None:
            sets.append("source_value=?")
            params.append(source_value)
        if source_type is not None:
            sets.append("source_type=?")
            params.append(source_type)
        if reason is not None:
            sets.append("reason=?")
            params.append(reason)
        if update_company_id:
            sets.append("company_id=?")
            params.append(company_id or None)
        if not sets:
            return False
        params.append(item_id)
        with self.conn() as c:
            cur = c.execute(
                f"UPDATE source_verify_queue SET {', '.join(sets)} WHERE id=?",
                params,
            )
            return cur.rowcount > 0

    def update_company_notes(self, company_id: str, notes: str) -> bool:
        """更新公司展示用备注（companies.notes）。"""
        cid = (company_id or "").strip()
        if not cid:
            return False
        with self.conn() as c:
            cur = c.execute(
                "UPDATE companies SET notes=?, updated_at=? WHERE id=?",
                (notes, utc_now(), cid),
            )
            return cur.rowcount > 0

    def list_blocklist(self) -> list[str]:
        with self.conn() as c:
            return [r["pattern"] for r in c.execute("SELECT pattern FROM aggregator_blocklist").fetchall()]

    def add_digest(self, summary: str) -> str:
        did = new_id()
        with self.conn() as c:
            c.execute(
                "INSERT INTO digest_log(id, summary, created_at) VALUES (?, ?, ?)",
                (did, summary, utc_now()),
            )
        return did

    def latest_digests(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.conn() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM digest_log ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            ]

    def replace_jobs_from_cloud(self, jobs: list[dict[str, Any]]) -> int:
        """用云端增量数据 upsert 本地 jobs（不覆盖 my_status / my_pick）。"""
        n = 0
        for j in jobs:
            self.upsert_job(j)
            n += 1
        return n

    def _ensure_app_users_schema(self, c: sqlite3.Connection) -> None:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_users (
                account TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                notes TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        cols = {str(r[1]) for r in c.execute("PRAGMA table_info(app_users)").fetchall()}
        if "expires_at" not in cols:
            c.execute("ALTER TABLE app_users ADD COLUMN expires_at TEXT")

    def create_app_user(
        self,
        account: str,
        password: str,
        *,
        notes: str = "",
        enabled: bool = True,
        expires_at: str | None = None,
    ) -> None:
        """新建 Viewer 账号；账号为主键，新建时密码必填（由 UI 校验）。"""
        acct = (account or "").strip()
        if not acct:
            raise ValueError("账号不能为空")
        if self.get_app_user(acct):
            raise ValueError(f"账号「{acct}」已存在")
        expiry = normalize_expiry(expires_at)
        now = utc_now()
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO app_users(account, password_hash, notes, enabled, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (acct, hash_password(password), notes or "", 1 if enabled else 0, expiry, now, now),
            )

    def update_app_user(
        self,
        account: str,
        *,
        password: str | None = None,
        notes: str | None = None,
        enabled: bool | None = None,
        expires_at: str | None = None,
    ) -> None:
        acct = (account or "").strip()
        row = self.get_app_user(acct)
        if not row:
            raise ValueError(f"账号「{acct}」不存在")
        sets = ["updated_at=?"]
        params: list[Any] = [utc_now()]
        if password is not None and password.strip():
            sets.append("password_hash=?")
            params.append(hash_password(password))
        if notes is not None:
            sets.append("notes=?")
            params.append(notes)
        if enabled is not None:
            sets.append("enabled=?")
            params.append(1 if enabled else 0)
        if expires_at is not None:
            sets.append("expires_at=?")
            params.append(normalize_expiry(expires_at))
        params.append(acct)
        with self.conn() as c:
            c.execute(f"UPDATE app_users SET {', '.join(sets)} WHERE account=?", params)

    def delete_app_user(self, account: str) -> bool:
        acct = (account or "").strip()
        with self.conn() as c:
            cur = c.execute("DELETE FROM app_users WHERE account=?", (acct,))
            return cur.rowcount > 0

    def get_app_user(self, account: str) -> dict[str, Any] | None:
        acct = (account or "").strip()
        with self.conn() as c:
            row = c.execute("SELECT * FROM app_users WHERE account=?", (acct,)).fetchone()
            return dict(row) if row else None

    def list_app_users(self) -> list[dict[str, Any]]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM app_users ORDER BY account").fetchall()]

    def count_app_users(self) -> int:
        with self.conn() as c:
            row = c.execute("SELECT COUNT(*) AS n FROM app_users").fetchone()
            return int(row["n"]) if row else 0

    def verify_app_user_login(self, account: str, password: str) -> bool:
        row = self.get_app_user((account or "").strip())
        if not row or not int(row.get("enabled") or 0):
            return False
        if is_expired(row.get("expires_at")):
            return False
        return verify_password(password, str(row.get("password_hash") or ""))

    def replace_app_users_from_cloud(self, users: list[dict[str, Any]]) -> None:
        """用云端账号列表全量替换本地 app_users（不触碰 my_pick / jobs）。"""
        now = utc_now()
        with self.conn() as c:
            c.execute("DELETE FROM app_users")
            for u in users:
                acct = str(u.get("account") or "").strip()
                if not acct:
                    continue
                ts = str(u.get("updated_at") or now)
                c.execute(
                    """
                    INSERT INTO app_users(account, password_hash, notes, enabled, expires_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        acct,
                        u.get("password_hash") or "",
                        u.get("notes") or "",
                        1 if u.get("enabled") else 0,
                        normalize_expiry(u.get("expires_at")),
                        ts,
                        ts,
                    ),
                )

    # ---- deep collect queue ----
    def ensure_collect_queue_schema(self) -> None:
        """旧库升级：补建 collect_queue。"""
        with self.conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS collect_queue (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    company_id TEXT NOT NULL,
                    company_name TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    jobs_published INTEGER NOT NULL DEFAULT 0,
                    jobs_queued INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, company_id)
                );
                CREATE INDEX IF NOT EXISTS idx_collect_queue_run ON collect_queue(run_id, status);
                """
            )

    def create_collect_run(
        self,
        companies: list[dict[str, Any]],
        *,
        run_id: str | None = None,
        reset: bool = False,
        resume: bool = True,
    ) -> str:
        """
        为深度采集创建队列。
        resume=True 且存在未完成 run 时复用；reset=True 强制新开并取消旧 pending。
        """
        self.ensure_collect_queue_schema()
        if resume and not reset and not run_id:
            existing = self.get_meta("deep_collect_run_id")
            if existing:
                prog = self.collect_queue_progress(existing)
                if prog.get("pending", 0) > 0 or prog.get("running", 0) > 0:
                    self.set_meta("deep_collect_cancel", "0")
                    return existing

        rid = run_id or new_id()
        now = utc_now()
        with self.conn() as c:
            if reset:
                c.execute(
                    "UPDATE collect_queue SET status='cancelled', updated_at=? "
                    "WHERE status IN ('pending','running')",
                    (now,),
                )
            for co in companies:
                cid = co.get("id")
                if not cid:
                    continue
                c.execute(
                    """
                    INSERT INTO collect_queue(
                        id, run_id, company_id, company_name, status, attempts,
                        last_error, jobs_published, jobs_queued, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', 0, NULL, 0, 0, ?, ?)
                    ON CONFLICT(run_id, company_id) DO NOTHING
                    """,
                    (new_id(), rid, cid, co.get("name"), now, now),
                )
        self.set_meta("deep_collect_run_id", rid)
        self.set_meta("deep_collect_cancel", "0")
        return rid

    def reclaim_stuck_collect_queue(self, run_id: str) -> int:
        """将中断残留的 running 项改回 pending，便于续跑。"""
        self.ensure_collect_queue_schema()
        now = utc_now()
        with self.conn() as c:
            cur = c.execute(
                "UPDATE collect_queue SET status='pending', updated_at=? "
                "WHERE run_id=? AND status='running'",
                (now, run_id),
            )
        return int(cur.rowcount or 0)

    def list_collect_queue(
        self,
        run_id: str,
        *,
        status: str | None = "pending",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.ensure_collect_queue_schema()
        sql = "SELECT * FROM collect_queue WHERE run_id=?"
        params: list[Any] = [run_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at ASC LIMIT ?"
        params.append(limit)
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def update_collect_queue_item(
        self,
        item_id: str,
        *,
        status: str | None = None,
        last_error: str | None = None,
        jobs_published: int | None = None,
        jobs_queued: int | None = None,
        bump_attempts: bool = False,
    ) -> None:
        self.ensure_collect_queue_schema()
        sets = ["updated_at=?"]
        params: list[Any] = [utc_now()]
        if status is not None:
            sets.append("status=?")
            params.append(status)
        if last_error is not None:
            sets.append("last_error=?")
            params.append(last_error)
        if jobs_published is not None:
            sets.append("jobs_published=?")
            params.append(jobs_published)
        if jobs_queued is not None:
            sets.append("jobs_queued=?")
            params.append(jobs_queued)
        if bump_attempts:
            sets.append("attempts=attempts+1")
        params.append(item_id)
        with self.conn() as c:
            c.execute(f"UPDATE collect_queue SET {', '.join(sets)} WHERE id=?", params)

    def collect_queue_progress(self, run_id: str) -> dict[str, Any]:
        self.ensure_collect_queue_schema()
        with self.conn() as c:
            rows = c.execute(
                "SELECT status, COUNT(*) AS n, "
                "COALESCE(SUM(jobs_published),0) AS pub, "
                "COALESCE(SUM(jobs_queued),0) AS queued "
                "FROM collect_queue WHERE run_id=? GROUP BY status",
                (run_id,),
            ).fetchall()
        by_status = {r["status"]: int(r["n"]) for r in rows}
        pub = sum(int(r["pub"]) for r in rows)
        queued = sum(int(r["queued"]) for r in rows)
        total = sum(by_status.values())
        done = by_status.get("done", 0) + by_status.get("error", 0) + by_status.get("skipped", 0)
        return {
            "run_id": run_id,
            "total": total,
            "pending": by_status.get("pending", 0),
            "running": by_status.get("running", 0),
            "done": by_status.get("done", 0),
            "error": by_status.get("error", 0),
            "skipped": by_status.get("skipped", 0),
            "cancelled": by_status.get("cancelled", 0),
            "processed": done,
            "jobs_published": pub,
            "jobs_queued": queued,
            "by_status": by_status,
        }

    def request_deep_collect_cancel(self) -> None:
        self.set_meta("deep_collect_cancel", "1")

    def deep_collect_cancel_requested(self) -> bool:
        return (self.get_meta("deep_collect_cancel") or "0") == "1"

    def clear_all_job_related_data(self) -> dict[str, int]:
        """
        清空本地岗位及相关审核/日报/采集数据。
        保留 companies、aggregator_blocklist；不触碰配置文件中的云端凭证。
        """
        self.ensure_collect_queue_schema()
        meta_keys = (
            "last_scan_finished_at",
            "last_scan_mode",
            "last_scan_summary",
            "last_nightly_finished_at",
            "last_nightly_attempt_at",
            "last_nightly_status",
            "last_nightly_error",
            "deep_collect_last_progress",
            "deep_collect_run_id",
            "deep_collect_cancel",
            "last_cloud_sync_at",
            "last_cloud_sync_id",
            "last_sync_ok_at",
            "cloud_revoke_ids",
        )
        with self.conn() as c:
            n_jobs = int(c.execute("DELETE FROM jobs").rowcount or 0)
            n_review = int(c.execute("DELETE FROM review_queue").rowcount or 0)
            n_src = int(c.execute("DELETE FROM source_verify_queue").rowcount or 0)
            n_digest = int(c.execute("DELETE FROM digest_log").rowcount or 0)
            n_queue = int(c.execute("DELETE FROM collect_queue").rowcount or 0)
            for key in meta_keys:
                c.execute("DELETE FROM sync_meta WHERE key=?", (key,))
        return {
            "jobs": n_jobs,
            "review_queue": n_review,
            "source_verify_queue": n_src,
            "digest_log": n_digest,
            "collect_queue": n_queue,
        }


def normalize_company_name(name: str) -> str:
    s = (name or "").strip().lower()
    for ch in (" ", "\u3000", "（", "）", "(", ")", "股份有限公司", "有限公司", "有限责任公司", "集团"):
        s = s.replace(ch.lower() if ch.isascii() else ch, "")
    return s


def _merge_json_list(existing_json: str | None, incoming: Any) -> list[str]:
    try:
        base = json.loads(existing_json or "[]")
    except json.JSONDecodeError:
        base = []
    if not isinstance(base, list):
        base = []
    if isinstance(incoming, str):
        try:
            incoming = json.loads(incoming)
        except json.JSONDecodeError:
            incoming = [incoming] if incoming else []
    if not incoming:
        incoming = []
    if not isinstance(incoming, list):
        incoming = [str(incoming)]
    seen = set()
    out: list[str] = []
    for item in list(base) + list(incoming):
        s = str(item).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _seed_url_key(url: str) -> str:
    from app.collector.filters import normalize_url_for_dedupe

    u = (url or "").strip()
    if not u:
        return ""
    return normalize_url_for_dedupe(u) or u.lower()


def _dedupe_seed_urls(urls: list[str]) -> list[str]:
    from app.collector.filters import looks_like_url

    seen: set[str] = set()
    out: list[str] = []
    for raw in urls:
        u = (raw or "").strip()
        if not looks_like_url(u):
            continue
        key = _seed_url_key(u)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(u)
    return out


def _apply_seed_url_replacements(
    urls: list[str], replacements: list[tuple[str, str]]
) -> list[str]:
    """在种子 URL 列表中用新链替换旧链；旧链不存在则追加新链；新链无效则移除旧链。"""
    from app.collector.filters import looks_like_url

    result = list(urls)
    for old, new in replacements:
        old = (old or "").strip()
        new = (new or "").strip()
        old_key = _seed_url_key(old) if old else ""
        new_ok = looks_like_url(new)
        if old_key and not new_ok:
            result = [u for u in result if _seed_url_key(u) != old_key]
            continue
        if not new_ok:
            continue
        new_key = _seed_url_key(new)
        if old_key and old_key == new_key:
            continue
        if old_key:
            next_result: list[str] = []
            replaced = False
            for u in result:
                if _seed_url_key(u) == old_key:
                    if not replaced:
                        next_result.append(new)
                        replaced = True
                    continue
                next_result.append(u)
            result = next_result
            if not replaced:
                result.append(new)
        else:
            if not any(_seed_url_key(u) == new_key for u in result):
                result.append(new)
    return _dedupe_seed_urls(result)
