"""夜间自动复检：刷新官方源，有变化则更新；超 3 个月未更新进异常。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any, Callable

from app.collector.cleanup import cleanup_expired_jobs
from app.collector.filters import resolve_lookback_days, resolve_list_lookback_months
from app.collector.website import run_website_collect
from app.db.local import LocalDB, utc_now

ProgressCb = Callable[[str], None]


def normalize_schedule(hour: Any, minute: Any = 0) -> tuple[int, int]:
    """Return a valid local schedule without treating midnight (0) as missing."""
    try:
        hour_value = int(hour)
    except (TypeError, ValueError):
        hour_value = 2
    try:
        minute_value = int(minute)
    except (TypeError, ValueError):
        minute_value = 0
    return max(0, min(23, hour_value)), max(0, min(59, minute_value))


def _local_now_naive() -> datetime:
    from app.timeutil import now as cn_now

    return cn_now().replace(tzinfo=None)


def next_local_run_at(
    hour: int,
    minute: int = 0,
    *,
    now: datetime | None = None,
) -> datetime:
    """Return the next local scheduled datetime."""
    current = now or _local_now_naive()
    run_hour, run_minute = normalize_schedule(hour, minute)
    target = current.replace(
        hour=run_hour,
        minute=run_minute,
        second=0,
        microsecond=0,
    )
    if target <= current:
        target += timedelta(days=1)
    return target


def seconds_until_next_local(
    hour: int,
    minute: int = 0,
    *,
    now: datetime | None = None,
) -> float:
    """距下次本地（Asia/Shanghai）hour:minute 的秒数（若已过则等到明天）。"""
    current = now or _local_now_naive()
    return max(1.0, (next_local_run_at(hour, minute, now=current) - current).total_seconds())


def _parse_meta_local(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed
    from app.timeutil import CN_TZ

    return parsed.astimezone(CN_TZ).replace(tzinfo=None)


def run_nightly_refresh(
    db: LocalDB,
    *,
    lookback_days: int | None = None,
    collect_months: int | None = None,
    list_collect_months: int | None = None,
    retention_days: int | None = None,
    limit_companies: int = 0,
    progress: ProgressCb | None = None,
) -> dict[str, Any]:
    """
    夜间复检：对全部带 career/hint URL 的公司重跑官网采集。
    - 覆盖校园招聘 + 实习生招聘频道（不含社招）；扫完各入口不提前中断
    - 页面有更新 → upsert 刷新本地岗位（同 URL/公司+标题不插重复行）
    - 字段无变化 → unchanged，不刷写
    - 最新信息超时间窗 → 进异常队列 stale_over_3m
    - 结束后按 retention_days 软删超期岗位
    - limit_companies<=0：全量；>0 时仅测/限流
    """
    days = resolve_lookback_days(lookback_days=lookback_days, collect_months=collect_months)
    list_months = resolve_list_lookback_months(list_collect_months)
    scope = "全部带 URL 公司" if not limit_companies or limit_companies <= 0 else f"最多 {limit_companies} 家"
    if progress:
        progress(
            f"夜间复检开始（校招+实习全量频道，近 {days} 天，列表翻页近 {list_months} 个月，{scope}）…"
        )
    jobs_before = db.count_jobs()
    web = run_website_collect(
        db,
        limit_companies=limit_companies if limit_companies and limit_companies > 0 else 0,
        lookback_days=days,
        list_collect_months=list_months,
        require_real_jobs=False,
        max_career_urls=0,
        stop_when_published=False,
        progress=progress,
    )
    retention = cleanup_expired_jobs(db, retention_days=retention_days)
    jobs_after = db.count_jobs()
    summary = (
        f"[{utc_now()}] 【夜间复检】官网 解析{web.get('parsed', 0)}/"
        f"新增或更新{web.get('published', 0)}/"
        f"无变化{web.get('unchanged', 0)}/"
        f"队列{web.get('queued', 0)}/"
        f"过期异常{web.get('stale', 0)}/错{web.get('errors', 0)}；"
        f"保留清理软删 {retention.get('deleted', 0)}；"
        f"本地岗位 {jobs_before}→{jobs_after}。"
    )
    db.add_digest(summary)
    finished = utc_now()
    db.set_meta("last_scan_finished_at", finished)
    db.set_meta("last_scan_mode", "nightly")
    db.set_meta("last_nightly_finished_at", finished)
    db.set_meta("last_scan_summary", summary)
    if progress:
        progress(summary)
    return {
        "text": summary,
        "finished_at": finished,
        "lookback_days": days,
        "list_collect_months": list_months,
        "web": web,
        "retention": retention,
        "jobs_before": jobs_before,
        "jobs_after": jobs_after,
    }


class NightlyScheduler:
    """Admin 进程内后台线程：到点跑复检。"""

    def __init__(
        self,
        db: LocalDB,
        *,
        enabled: bool = True,
        hour: int = 2,
        minute: int = 0,
        lookback_days: int = 90,
        limit_companies: int = 0,
        on_log: ProgressCb | None = None,
        get_config: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.db = db
        self.enabled = enabled
        self.hour = hour
        self.minute = minute
        self.lookback_days = lookback_days
        self.limit_companies = limit_companies
        self.on_log = on_log
        self.get_config = get_config
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._job_lock = threading.Lock()
        self.hour, self.minute = normalize_schedule(self.hour, self.minute)

    def start(self) -> None:
        if not self.enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(target=self._loop, name="nightly-refresh", daemon=True)
        self._thread.start()
        if self.on_log:
            self.on_log(f"夜间复检已启用：每天 {self.hour:02d}:{self.minute:02d} 自动检测更新（校招+实习全量）")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def update_schedule(
        self,
        *,
        enabled: bool,
        hour: Any,
        minute: Any,
        lookback_days: Any,
        limit_companies: Any,
    ) -> None:
        """Apply saved settings and wake the loop so they take effect immediately."""
        self.enabled = bool(enabled)
        self.hour, self.minute = normalize_schedule(hour, minute)
        try:
            self.lookback_days = max(1, int(lookback_days))
        except (TypeError, ValueError):
            self.lookback_days = 90
        try:
            self.limit_companies = max(0, int(limit_companies))
        except (TypeError, ValueError):
            self.limit_companies = 0
        self._wake.set()
        if self.enabled:
            self.start()

    def run_now_async(self, *, full: bool = True) -> None:
        """手动立刻跑一轮（不阻塞 UI）。full=True 时忽略公司数上限，扫全量校招+实习。"""
        def _job() -> None:
            self._execute_once(force_full=full)

        threading.Thread(target=_job, name="nightly-refresh-manual", daemon=True).start()

    def _reload_config(self) -> None:
        cfg = self.get_config() if self.get_config else {}
        if not cfg:
            return
        self.enabled = bool(cfg.get("nightly_enabled", True))
        self.hour, self.minute = normalize_schedule(
            cfg.get("nightly_hour", self.hour),
            cfg.get("nightly_minute", self.minute),
        )
        try:
            self.lookback_days = max(1, int(cfg.get("lookback_days", self.lookback_days)))
        except (TypeError, ValueError):
            self.lookback_days = 90
        raw_limit = cfg.get("nightly_limit_companies", self.limit_companies)
        try:
            self.limit_companies = max(0, int(raw_limit if raw_limit is not None else 0))
        except (TypeError, ValueError):
            self.limit_companies = 0

    def _pending_catch_up_slot(self, now: datetime | None = None) -> datetime | None:
        """Return the latest missed schedule slot that has not been attempted."""
        current = now or _local_now_naive()
        scheduled_today = current.replace(
            hour=self.hour,
            minute=self.minute,
            second=0,
            microsecond=0,
        )
        last_raw = self.db.get_meta("last_nightly_attempt_at") or self.db.get_meta(
            "last_nightly_finished_at"
        )
        last_attempt = _parse_meta_local(last_raw)
        if last_attempt is None:
            return scheduled_today if current >= scheduled_today else None
        latest_slot = scheduled_today if current >= scheduled_today else scheduled_today - timedelta(days=1)
        return latest_slot if last_attempt < latest_slot else None

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            self._reload_config()
            if not self.enabled:
                self._wake.wait(60)
                continue

            missed_slot = self._pending_catch_up_slot()
            if missed_slot is not None:
                if self.on_log:
                    self.on_log(
                        f"检测到错过的夜间复检计划（{missed_slot:%m-%d %H:%M}），现在自动补跑"
                    )
                self._execute_once(force_full=False)
                continue

            next_run = next_local_run_at(self.hour, self.minute)
            wait_s = max(1.0, (next_run - _local_now_naive()).total_seconds())
            if self.on_log:
                self.on_log(f"下次夜间复检：{next_run:%Y-%m-%d %H:%M}")
            if self._wake.wait(wait_s):
                continue
            if self._stop.is_set():
                break
            self._execute_once(force_full=False)

    def _execute_once(self, *, force_full: bool = False) -> bool:
        if not self._job_lock.acquire(blocking=False):
            if self.on_log:
                self.on_log("夜间复检正在进行中，本次重复触发已跳过")
            return False
        attempt_at = utc_now()
        self.db.set_meta("last_nightly_attempt_at", attempt_at)
        self.db.set_meta("last_nightly_status", "running")
        try:
            months = None
            list_months = None
            retention_days = None
            if self.get_config:
                cfg = self.get_config()
                months = cfg.get("collect_months")
                if cfg.get("list_collect_months") is not None:
                    list_months = int(cfg.get("list_collect_months") or 6)
                if cfg.get("retention_days") is not None:
                    retention_days = int(cfg.get("retention_days") or 365)
                raw_limit = cfg.get("nightly_limit_companies", self.limit_companies)
                try:
                    self.limit_companies = int(raw_limit if raw_limit is not None else 0)
                except (TypeError, ValueError):
                    self.limit_companies = 0
            limit = 0 if force_full else self.limit_companies
            run_nightly_refresh(
                self.db,
                lookback_days=self.lookback_days,
                collect_months=int(months) if months else None,
                list_collect_months=list_months,
                retention_days=retention_days,
                limit_companies=limit,
                progress=self.on_log,
            )
            self.db.set_meta("last_nightly_status", "success")
            self.db.set_meta("last_nightly_error", "")
            return True
        except Exception as exc:  # noqa: BLE001
            self.db.set_meta("last_nightly_status", "failed")
            self.db.set_meta("last_nightly_error", str(exc))
            if self.on_log:
                self.on_log(f"夜间复检失败：{exc}")
            return False
        finally:
            self._job_lock.release()
