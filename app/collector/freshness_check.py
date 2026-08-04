"""岗位审核「信息时效性识别」：近半年窗判定 + 可选抓取源码日期。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

import httpx

from app.collector.adapters.router import DEFAULT_HEADERS
from app.collector.fill_from_url import pick_fill_url
from app.collector.filters import (
    current_grad_batch,
    list_date_cutoff,
    normalize_grad_batch,
    parse_date_loose,
    parse_grad_batch,
    resolve_list_lookback_months,
)
from app.collector.label_fields import extract_labeled_fields, html_to_visible_text
from app.collector.page_mtime import PageMtimeHit, extract_page_mtime
from app.timeutil import today as cn_today

TimelinessAction = Literal["keep", "delete", "keep_no_date", "error"]


@dataclass
class TimelinessItem:
    job_id: str
    title: str
    ref_date: date | None
    source: str | None
    in_window: bool | None
    action: TimelinessAction
    message: str
    open_at_write: str | None = None  # 新识别到、建议写回的 open_at
    error: str | None = None


@dataclass
class TimelinessReport:
    months: int
    cutoff: date
    items: list[TimelinessItem] = field(default_factory=list)

    @property
    def keep_ids(self) -> list[str]:
        return [i.job_id for i in self.items if i.action in ("keep", "keep_no_date")]

    @property
    def delete_ids(self) -> list[str]:
        return [i.job_id for i in self.items if i.action == "delete"]

    @property
    def writebacks(self) -> list[tuple[str, str]]:
        return [
            (i.job_id, i.open_at_write)
            for i in self.items
            if i.open_at_write and i.action != "error"
        ]

    def summary_zh(self) -> str:
        keep_n = len(self.keep_ids)
        del_n = len(self.delete_ids)
        err_n = sum(1 for i in self.items if i.action == "error")
        no_date_n = sum(1 for i in self.items if i.action == "keep_no_date")
        parts = [
            f"近 {self.months} 个月窗口（≥ {self.cutoff.isoformat()}）",
            f"保留 {keep_n}",
            f"删除 {del_n}",
        ]
        if no_date_n:
            parts.append(f"其中无日期保留 {no_date_n}")
        if err_n:
            parts.append(f"失败 {err_n}")
        lines = ["；".join(parts) + "。"]
        for i in self.items[:12]:
            title = (i.title or i.job_id)[:28]
            if i.ref_date:
                lines.append(
                    f"· {title}：{i.ref_date.isoformat()}（{i.source or '?'}）→ "
                    f"{'保留' if i.action != 'delete' else '删除'}"
                )
            else:
                lines.append(f"· {title}：{i.message}")
        if len(self.items) > 12:
            lines.append(f"…共 {len(self.items)} 条")
        return "\n".join(lines)


def _source_label(src: str | None) -> str:
    return {
        "open_at": "open_at",
        "labeled": "标签日期",
        "platform_version": "平台版本",
        "logo_media": "logo 资源",
        "banner_media": "banner 资源",
        "updated_at": "updated_at",
        "created_at": "created_at",
    }.get(src or "", src or "")


def resolve_job_content_date(
    job: dict[str, Any],
    *,
    html: str | None = None,
) -> tuple[date | None, str | None, str | None]:
    """
    解析招聘信息参照日（非 DB 触摸时间优先）。

    优先级：open_at > JD/可见标签「更新/发布日期」> 源码 page_mtime
    > 仅当 open_at 等皆无时才弱用 created_at（不用 DB updated_at，避免刚编辑即「仍新」）。
    返回 (date, source, iso_for_writeback|None)。
    """
    open_raw = job.get("open_at")
    d = parse_date_loose(open_raw if isinstance(open_raw, str) else None)
    if d:
        return d, "open_at", None

    jd = str(job.get("jd_text") or "")
    if jd:
        labels = extract_labeled_fields(jd)
        for key in ("open_at",):
            d = parse_date_loose(labels.get(key))
            if d:
                return d, "labeled", d.isoformat()

    hit: PageMtimeHit | None = None
    if html:
        visible = html_to_visible_text(html)
        hit = extract_page_mtime(html, visible_text=visible, prefer_labeled=True)
        if hit:
            d = parse_date_loose(hit.date)
            if d:
                return d, hit.source, hit.date

    # 弱回退：创建_at（入库日），仍无则 None
    created = job.get("created_at")
    d = parse_date_loose(created if isinstance(created, str) else None)
    if d:
        return d, "created_at", None
    return None, None, None


def evaluate_timeliness(
    jobs: list[dict[str, Any]],
    *,
    months: int | None = None,
    today: date | None = None,
    fetch_missing: bool = True,
    timeout: float = 20.0,
) -> TimelinessReport:
    """对岗位列表做近半年窗判定；缺日期时可抓取页面补源码日期。"""
    m = resolve_list_lookback_months(months)
    day = today or cn_today()
    cutoff = list_date_cutoff(months=m, today=day)
    report = TimelinessReport(months=m, cutoff=cutoff)

    client: httpx.Client | None = None
    try:
        if fetch_missing:
            client = httpx.Client(
                headers=DEFAULT_HEADERS, follow_redirects=True, timeout=timeout
            )
        for job in jobs:
            jid = str(job.get("id") or "")
            title = str(job.get("title") or "")
            html: str | None = None
            # 先本地字段
            ref, src, write = resolve_job_content_date(job, html=None)
            if ref is None and fetch_missing and client is not None:
                url = pick_fill_url(job.get("apply_url"), job.get("source_url"))
                if url:
                    try:
                        resp = client.get(url)
                        resp.raise_for_status()
                        html = resp.text
                        ref, src, write = resolve_job_content_date(job, html=html)
                    except Exception as exc:  # noqa: BLE001
                        report.items.append(
                            TimelinessItem(
                                job_id=jid,
                                title=title,
                                ref_date=None,
                                source=None,
                                in_window=None,
                                action="error",
                                message=f"抓取失败：{exc}",
                                error=str(exc),
                            )
                        )
                        continue
            if ref is None:
                report.items.append(
                    TimelinessItem(
                        job_id=jid,
                        title=title,
                        ref_date=None,
                        source=None,
                        in_window=None,
                        action="keep_no_date",
                        message="未能识别更新日期，已保留",
                    )
                )
                continue
            in_win = ref >= cutoff
            # 时效性只看发布/更新日；届别不符仅提示，绝不改写成目标届
            batch_note = ""
            title_batch = parse_grad_batch(title) or parse_grad_batch(
                str(job.get("jd_text") or "")
            )
            page_batch = title_batch or normalize_grad_batch(
                str(job.get("graduation_batch") or "") or None
            )
            target = current_grad_batch(day)
            if page_batch and page_batch != target:
                batch_note = (
                    f"；标题/页面届别为{page_batch}（非目标{target}，未改写届别）"
                )
            report.items.append(
                TimelinessItem(
                    job_id=jid,
                    title=title,
                    ref_date=ref,
                    source=_source_label(src),
                    in_window=in_win,
                    action="keep" if in_win else "delete",
                    message=(
                        f"识别到更新日期：{ref.isoformat()}（{_source_label(src)}）；"
                        + ("仍在近半年内" if in_win else "已超出近半年，建议删除")
                        + batch_note
                    ),
                    open_at_write=write if write and not job.get("open_at") else None,
                )
            )
    finally:
        if client is not None:
            client.close()
    return report
