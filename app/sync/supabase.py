"""Supabase REST 增量同步与自动发布。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import APP_VERSION
from app.db.local import LocalDB, utc_now


class ViewerSessionError(RuntimeError):
    """Viewer 云端会话不可用或已过期。"""


class SupabaseSync:
    def __init__(
        self,
        url: str,
        anon_key: str,
        service_role_key: str = "",
        session_token: str = "",
    ) -> None:
        self.base = (url or "").rstrip("/")
        self.anon_key = anon_key or ""
        self.service_role_key = service_role_key or ""
        self.session_token = session_token or ""

    @property
    def enabled(self) -> bool:
        return bool(self.base and self.anon_key)

    def _headers(self, *, write: bool = False) -> dict[str, str]:
        key = self.service_role_key if write and self.service_role_key else self.anon_key
        return {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=representation",
        }

    def _rest(self, path: str) -> str:
        return f"{self.base}/rest/v1/{path.lstrip('/')}"

    def check_min_version(self) -> tuple[bool, str]:
        """读取 app_meta.min_version；失败时放行。"""
        if not self.enabled:
            return True, APP_VERSION
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(
                    self._rest("app_meta"),
                    params={"key": "eq.min_version", "select": "value"},
                    headers=self._headers(write=False),
                )
                if resp.status_code >= 400:
                    return True, APP_VERSION
                rows = resp.json()
                if not rows:
                    return True, APP_VERSION
                min_v = str(rows[0].get("value") or "0.0.0")
                ok = _version_tuple(APP_VERSION) >= _version_tuple(min_v)
                return ok, min_v
        except Exception:
            return True, APP_VERSION

    def pull_jobs(
        self,
        db: LocalDB,
        since: str | None = None,
        *,
        page_size: int = 1000,
    ) -> int:
        """按稳定复合游标增量拉取 active/deleted 岗位。"""
        if not self.enabled:
            return 0
        page_size = max(1, min(int(page_size or 1000), 1000))
        cursor_at = since or db.get_meta("last_cloud_sync_at") or "1970-01-01T00:00:00+00:00"
        cursor_id = "" if since is not None else (db.get_meta("last_cloud_sync_id") or "")
        total = 0

        if not self.session_token:
            raise ViewerSessionError("Viewer 未登录云端会话，请重新启动并登录")

        with httpx.Client(timeout=60.0) as client:
            while True:
                body = {
                    "p_session_token": self.session_token,
                    "p_updated_at": cursor_at,
                    "p_after_id": cursor_id or None,
                    "p_limit": page_size,
                }
                resp = client.post(
                    f"{self.base}/rest/v1/rpc/viewer_sync_jobs",
                    headers=self._headers(write=False),
                    content=json.dumps(body, ensure_ascii=False),
                )
                if resp.status_code in (401, 403):
                    self.session_token = ""
                    raise ViewerSessionError("Viewer 云端会话已过期，请重新启动并登录")
                if resp.status_code >= 400:
                    detail = (resp.text or "")[:240]
                    raise ViewerSessionError(f"Viewer 云端同步不可用 HTTP {resp.status_code}：{detail}")
                rows = resp.json()
                if not isinstance(rows, list):
                    raise RuntimeError("Supabase 岗位同步返回了非列表数据")
                if not rows:
                    break

                last = rows[-1]
                next_at = str(last.get("updated_at") or "").strip()
                next_id = str(last.get("id") or "").strip()
                if not next_at or not next_id:
                    raise RuntimeError("Supabase 岗位同步缺少 updated_at 或 id，无法推进游标")
                if next_at == cursor_at and next_id == cursor_id:
                    raise RuntimeError("Supabase 岗位同步游标未推进")

                total += db.replace_jobs_from_cloud([_cloud_to_local(r) for r in rows])
                cursor_at, cursor_id = next_at, next_id
                db.set_meta("last_cloud_sync_at", cursor_at)
                db.set_meta("last_cloud_sync_id", cursor_id)

                if len(rows) < page_size:
                    break

        db.set_meta("last_sync_ok_at", utc_now())
        return total

    def create_viewer_session(self, account: str, password: str) -> str | None:
        """通过云端 RPC 登录并在内存中保存短期会话令牌。"""
        if not self.enabled:
            return None
        acct = (account or "").strip()
        if not acct or not password:
            return None
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                f"{self.base}/rest/v1/rpc/viewer_login_session",
                headers=self._headers(write=False),
                content=json.dumps(
                    {"p_account": acct, "p_password": password},
                    ensure_ascii=False,
                ),
            )
            if resp.status_code >= 400:
                detail = (resp.text or "")[:240]
                raise ViewerSessionError(
                    f"云端登录服务不可用 HTTP {resp.status_code}：{detail}"
                )
            payload = resp.json()
        rows = payload if isinstance(payload, list) else [payload]
        token = str(rows[0].get("session_token") or "") if rows else ""
        if not token:
            return None
        self.session_token = token
        return token

    def clear_viewer_session(self) -> None:
        self.session_token = ""

    def push_job(self, job: dict[str, Any]) -> None:
        if not self.enabled:
            raise RuntimeError("未配置 Supabase")
        if not self.service_role_key:
            raise RuntimeError("自动发布需要 service_role_key（仅 Admin）")
        payload = _local_to_cloud(job)
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                self._rest("jobs"),
                headers={**self._headers(write=True), "Prefer": "resolution=merge-duplicates"},
                content=json.dumps(payload, ensure_ascii=False),
            )
            resp.raise_for_status()

    def require_write_config(self) -> None:
        """推送前校验配置；未配置时抛出可读错误（勿在 jobs 为空时跳过）。"""
        if not self.enabled:
            raise RuntimeError("未配置 Supabase。请到「配置」页填写 URL 与 Anon Key 并保存。")
        if not self.service_role_key:
            raise RuntimeError(
                "未配置 Service Role Key。推送云端需要写权限（仅本机 Admin，勿打包进同学端）。"
            )

    def push_jobs(self, jobs: list[dict[str, Any]], *, chunk_size: int = 200) -> int:
        """批量 upsert jobs；按 chunk 分批，避免单次 payload 过大。"""
        self.require_write_config()
        if not jobs:
            return 0
        payload = [_local_to_cloud(j) for j in jobs]
        chunk_size = max(1, int(chunk_size or 200))
        pushed = 0
        with httpx.Client(timeout=90.0) as client:
            for i in range(0, len(payload), chunk_size):
                chunk = payload[i : i + chunk_size]
                resp = client.post(
                    self._rest("jobs"),
                    headers={**self._headers(write=True), "Prefer": "resolution=merge-duplicates"},
                    content=json.dumps(chunk, ensure_ascii=False),
                )
                if resp.status_code >= 400:
                    detail = (resp.text or "")[:400]
                    raise RuntimeError(
                        f"Supabase 推送失败 HTTP {resp.status_code}。"
                        f"请检查 Service Role Key / jobs 表 RLS 与列映射。\n{detail}"
                    )
                pushed += len(chunk)
        return pushed

    def publish_local_active_jobs(self, db: LocalDB, limit: int = 200) -> int:
        """推送本地 active jobs。jobs 为空时返回 0（调用方应提示用户先采集）。"""
        self.require_write_config()
        jobs = db.list_jobs(status="active", limit=limit)
        return self.push_jobs(jobs)

    def publish_local_jobs_for_sync(
        self,
        db: LocalDB,
        limit: int = 2000,
        *,
        job_ids: list[str] | None = None,
    ) -> dict[str, int]:
        """推送「岗位显示」中的 active，并附带 deleted（便于清理后覆盖云端 status）。

        job_ids 非空时仅推送这些 id（仍会附带全部 deleted，保证软删同步）。
        返回 {"pushed", "active", "deleted"} 供 UI 弹窗说明。
        """
        self.require_write_config()
        if job_ids:
            id_set = {str(i) for i in job_ids if i}
            active = [
                j
                for j in db.get_jobs_by_ids(list(id_set))
                if (j.get("status") or "") == "active"
            ]
        else:
            active = db.list_jobs(status="active", limit=limit)
        deleted = db.list_jobs(status="deleted", limit=limit)
        # 反审核曾推送岗：下次推送以 deleted 覆盖云端，本地仍为 pending_review
        revoke = db.list_cloud_revoke_jobs(limit=limit)
        # 同 id 以 deleted / revoke 为准（清理与撤云优先）
        by_id: dict[str, dict] = {j["id"]: j for j in active if j.get("id")}
        for j in deleted:
            if j.get("id"):
                by_id[j["id"]] = j
        for j in revoke:
            if j.get("id"):
                by_id[j["id"]] = j
        jobs = list(by_id.values())
        pushed = self.push_jobs(jobs)
        if pushed:
            # 仅标记本地仍为 active/deleted；撤云的 pending_review 保持「未推送」
            mark_ids: list[str] = []
            for jid in [x.get("id") for x in jobs if x.get("id")]:
                local = db.get_job(str(jid)) or {}
                if (local.get("status") or "") in ("active", "deleted"):
                    mark_ids.append(str(jid))
            if mark_ids:
                db.mark_jobs_cloud_synced(mark_ids)
            db.remove_cloud_revoke_ids([str(r["id"]) for r in revoke if r.get("id")])
        return {
            "pushed": pushed,
            "active": len(active),
            "deleted": len(deleted),
            "revoked": len(revoke),
        }

    def push_app_users(self, db: LocalDB) -> int:
        """全量推送本地 app_users 到 Supabase（service_role）。"""
        self.require_write_config()
        users = db.list_app_users()
        payload = [_local_to_cloud_user(u) for u in users]
        with httpx.Client(timeout=30.0) as client:
            resp = client.delete(
                self._rest("app_users"),
                params={"account": "not.is.null"},
                headers={**self._headers(write=True), "Prefer": "return=minimal"},
            )
            if resp.status_code >= 400:
                detail = (resp.text or "")[:400]
                if resp.status_code == 404 and "app_users" in detail:
                    raise RuntimeError(
                        "清除云端账号失败：Supabase 中缺少 public.app_users 表。"
                        "请在 Supabase SQL Editor 执行项目文件 "
                        "supabase/migrations/20260803_schema_compat.sql，"
                        "再重新推送账号。\n"
                        f"{detail}"
                    )
                raise RuntimeError(
                    f"清除云端账号失败 HTTP {resp.status_code}。"
                    f"请检查 app_users 表是否已创建。\n{detail}"
                )
            if payload:
                resp = client.post(
                    self._rest("app_users"),
                    headers={**self._headers(write=True), "Prefer": "return=minimal"},
                    content=json.dumps(payload, ensure_ascii=False),
                )
                if resp.status_code >= 400:
                    detail = (resp.text or "")[:400]
                    raise RuntimeError(
                        f"Supabase 推送账号失败 HTTP {resp.status_code}。\n{detail}"
                    )
        return len(payload)

    def pull_app_users(self, db: LocalDB) -> int:
        """从 Supabase 拉取账号到本地（Admin 用 service_role）。"""
        self.require_write_config()
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(
                self._rest("app_users"),
                params={"select": "account,password_hash,notes,enabled,expires_at,updated_at"},
                headers=self._headers(write=True),
            )
            resp.raise_for_status()
            rows = resp.json()
        mapped = [_cloud_to_local_user(r) for r in rows]
        db.replace_app_users_from_cloud(mapped)
        return len(mapped)

    def verify_viewer_login(self, account: str, password: str) -> bool:
        """兼容旧调用方：登录成功即建立内存会话。"""
        try:
            return bool(self.create_viewer_session(account, password))
        except ViewerSessionError:
            return False

    def viewer_has_accounts(self) -> bool:
        """云端是否已有启用账号（RPC，不读取 hash）。"""
        if not self.enabled:
            return False
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                f"{self.base}/rest/v1/rpc/viewer_has_accounts",
                headers=self._headers(write=False),
                content="{}",
            )
            if resp.status_code >= 400:
                return False
            return bool(resp.json())


def _local_to_cloud_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "account": user.get("account"),
        "password_hash": user.get("password_hash"),
        "notes": user.get("notes"),
        "enabled": bool(int(user.get("enabled") or 0)),
        "expires_at": user.get("expires_at") or None,
        "updated_at": user.get("updated_at") or utc_now(),
    }


def _cloud_to_local_user(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "account": row.get("account"),
        "password_hash": row.get("password_hash"),
        "notes": row.get("notes"),
        "enabled": bool(row.get("enabled")),
        "expires_at": row.get("expires_at"),
        "updated_at": row.get("updated_at"),
    }


def _version_tuple(v: str) -> tuple[int, ...]:
    parts = []
    for p in (v or "0").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _cloud_to_local(row: dict[str, Any]) -> dict[str, Any]:
    tags = row.get("job_tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except json.JSONDecodeError:
            tags = []
    return {
        "id": row.get("id"),
        "company_id": row.get("company_id"),
        "group_name": row.get("group_name"),
        "company": row.get("company") or "",
        "recruit_project": row.get("recruit_project"),
        "recruit_bucket": row.get("recruit_bucket"),
        "company_nature": row.get("company_nature"),
        "title": row.get("title") or "",
        "source_url": row.get("source_url") or "",
        "apply_url": row.get("apply_url"),
        "deadline": row.get("deadline"),
        "work_location": row.get("work_location"),
        "industry": row.get("industry"),
        "education": row.get("education"),
        "salary_range": row.get("salary_range"),
        "headcount": row.get("headcount"),
        "open_at": row.get("open_at"),
        "graduation_batch": row.get("graduation_batch"),
        "jd_text": row.get("jd_text"),
        "job_tags": tags,
        "raw_category": row.get("raw_category"),
        "parse_status": row.get("parse_status") or "ok",
        "confidence": row.get("confidence") or 0.0,
        "status": row.get("status") or "active",
        "cloud_updated_at": row.get("updated_at"),
        "updated_at": row.get("updated_at"),
    }


def _local_to_cloud(job: dict[str, Any]) -> dict[str, Any]:
    tags = job.get("job_tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except json.JSONDecodeError:
            tags = []
    now = job.get("updated_at") or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return {
        "id": job.get("id"),
        "company_id": job.get("company_id"),
        "group_name": job.get("group_name"),
        "company": job.get("company"),
        "recruit_project": job.get("recruit_project"),
        "recruit_bucket": job.get("recruit_bucket"),
        "company_nature": job.get("company_nature"),
        "title": job.get("title"),
        "source_url": job.get("source_url"),
        "apply_url": job.get("apply_url"),
        "deadline": job.get("deadline"),
        "work_location": job.get("work_location"),
        "industry": job.get("industry"),
        "education": job.get("education"),
        "salary_range": job.get("salary_range"),
        "headcount": job.get("headcount"),
        "open_at": job.get("open_at"),
        "graduation_batch": job.get("graduation_batch"),
        "jd_text": job.get("jd_text"),
        "job_tags": tags,
        "raw_category": job.get("raw_category"),
        "parse_status": job.get("parse_status") or "ok",
        "confidence": job.get("confidence") or 0.0,
        "status": job.get("status") or "active",
        "updated_at": now,
    }
