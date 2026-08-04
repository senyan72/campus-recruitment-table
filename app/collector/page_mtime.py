"""从 HTML/JS 源码推断页面内容更新时间（北森/智联校园等）。

优先级（由高到低，调用方负责：显式标签日期仍优于本模块）：
1. 平台 version（$bs_vars / stc.beisen.com/YYYY.MM.DD.xxx）
2. Logo 媒体文件名中的 YYYYMMDD
3. Banner 等较旧资源路径中的日期
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

PageMtimeSource = Literal[
    "labeled",
    "inline_latest",
    "platform_version",
    "logo_media",
    "banner_media",
]

# $bs_vars / JSON 内 version:'2025.05.30.001'
_BS_VERSION_RE = re.compile(
    r"""['"]version['"]\s*:\s*['"](20\d{2}\.\d{1,2}\.\d{1,2}\.\d+)['"]""",
    re.I,
)
# //stc.beisen.com/2025.05.30.001/...
_STC_PATH_RE = re.compile(
    r"(?:stc\.beisen\.com|stc\.italent\.cn)/((?:20\d{2})\.\d{1,2}\.\d{1,2}\.\d+)/",
    re.I,
)
# logo / medias_YYYYMMDD / YYYYMMDDlogo
_LOGO_MEDIA_RE = re.compile(
    r"""(?P<path>[^\s"'<>]*?(?:medias[_-]?(?P<d8a>20\d{6})|(?P<d8b>20\d{6})\s*logo)[^\s"'<>]*?)""",
    re.I,
)
# banner 路径旁的 8 位或 7 位（2022817 → 2022-08-17）日期
_BANNER_MEDIA_RE = re.compile(
    r"""(?P<path>[^\s"'<>]*?(?:(?P<d8>20\d{6})|(?P<d7>20\d{2}[1-9]\d{2}))[^\s"'<>]*banner[^\s"'<>]*)""",
    re.I,
)
_BANNER_BEFORE_RE = re.compile(
    r"""(?P<path>[^\s"'<>]*banner[^\s"'<>]*?(?:(?P<d8>20\d{6})|(?P<d7>20\d{2}[1-9]\d{2}))[^\s"'<>]*)""",
    re.I,
)
# 显式「更新日期：…」
_LABELED_UPDATE_RE = re.compile(
    r"(?:更新(?:日期|时间)|发布(?:日期|时间)|开放时间|岗位发布时间)\s*[:：]?\s*"
    r"(20\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2})?日?)",
    re.I,
)
# hotjob / 迪卡侬等：地点旁「2026-07-31 最新」或「2026年7月31日最新发布」
_INLINE_LATEST_DATE_RE = re.compile(
    r"(20\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2})?日?)\s*最新(?:发布)?"
)
_DOTNET_TICKS_RE = re.compile(r"[?&]v=(\d{15,20})\b")


@dataclass(frozen=True)
class PageMtimeHit:
    date: str  # YYYY-MM-DD
    source: PageMtimeSource
    raw: str = ""


def _valid_ymd(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_platform_version_token(token: str | None) -> date | None:
    """YYYY.MM.DD.xxx → date。"""
    s = (token or "").strip()
    m = re.match(r"^(20\d{2})\.(\d{1,2})\.(\d{1,2})(?:\.\d+)?$", s)
    if not m:
        return None
    return _valid_ymd(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def parse_yyyymmdd(raw: str | None) -> date | None:
    s = (raw or "").strip()
    if len(s) == 8 and s.isdigit():
        return _valid_ymd(int(s[:4]), int(s[4:6]), int(s[6:8]))
    return None


def parse_compact_banner_date(raw: str | None) -> date | None:
    """支持 20220817 或 2022817（月无前导零）。"""
    s = (raw or "").strip()
    d = parse_yyyymmdd(s)
    if d:
        return d
    if len(s) == 7 and s.isdigit() and s.startswith("20"):
        y = int(s[:4])
        # M + DD：月 1–9
        mo = int(s[4])
        day = int(s[5:7])
        return _valid_ymd(y, mo, day)
    return None


def dotnet_ticks_to_date(ticks: int | str | None) -> date | None:
    """可选：.NET ticks（自 0001-01-01）→ UTC 日期，作佐证。"""
    try:
        t = int(ticks)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if t < 600_000_000_000_000_000:  # ~1900
        return None
    try:
        # 1 tick = 100ns
        epoch = datetime(1, 1, 1, tzinfo=timezone.utc)
        dt = epoch + timedelta(microseconds=t // 10)
        if dt.year < 2000 or dt.year > 2100:
            return None
        return dt.date()
    except (OverflowError, ValueError):
        return None


def _fmt(d: date) -> str:
    return d.isoformat()


def extract_labeled_update_date(text: str | None) -> PageMtimeHit | None:
    """可见文本中的「更新日期/发布时间」标签。"""
    raw = text or ""
    m = _LABELED_UPDATE_RE.search(raw)
    if not m:
        return None
    from app.collector.filters import parse_date_loose

    d = parse_date_loose(m.group(1))
    if not d:
        return None
    return PageMtimeHit(date=_fmt(d), source="labeled", raw=m.group(0)[:80])


def extract_inline_latest_publish_date(text: str | None) -> PageMtimeHit | None:
    """
    无标签的「日期 + 最新」岗位发布时间。
    例：上海市 | 2026-07-31 最新；2026年7月31日最新发布。
    """
    raw = text or ""
    m = _INLINE_LATEST_DATE_RE.search(raw)
    if not m:
        return None
    from app.collector.filters import parse_date_loose

    d = parse_date_loose(m.group(1))
    if not d:
        return None
    return PageMtimeHit(date=_fmt(d), source="inline_latest", raw=m.group(0)[:80])


def extract_platform_version_date(html: str | None) -> PageMtimeHit | None:
    raw = html or ""
    m = _BS_VERSION_RE.search(raw)
    if m:
        d = parse_platform_version_token(m.group(1))
        if d:
            return PageMtimeHit(date=_fmt(d), source="platform_version", raw=m.group(1))
    m = _STC_PATH_RE.search(raw)
    if m:
        d = parse_platform_version_token(m.group(1))
        if d:
            return PageMtimeHit(date=_fmt(d), source="platform_version", raw=m.group(1))
    return None


def extract_logo_media_date(html: str | None) -> PageMtimeHit | None:
    raw = html or ""
    best: PageMtimeHit | None = None
    for m in _LOGO_MEDIA_RE.finditer(raw):
        token = m.group("d8a") or m.group("d8b")
        d = parse_yyyymmdd(token)
        if not d:
            continue
        path = m.group("path") or ""
        # 可选 ticks 佐证：差距过大则跳过该命中
        tm = _DOTNET_TICKS_RE.search(path)
        if tm:
            td = dotnet_ticks_to_date(tm.group(1))
            if td and abs((td - d).days) > 366:
                continue
        hit = PageMtimeHit(date=_fmt(d), source="logo_media", raw=path[:120])
        if best is None or hit.date > best.date:
            best = hit
    return best


def extract_banner_media_date(html: str | None) -> PageMtimeHit | None:
    raw = html or ""
    best: PageMtimeHit | None = None
    for rx in (_BANNER_MEDIA_RE, _BANNER_BEFORE_RE):
        for m in rx.finditer(raw):
            token = m.group("d8") or m.group("d7")
            d = parse_compact_banner_date(token)
            if not d:
                continue
            path = m.group("path") or ""
            hit = PageMtimeHit(date=_fmt(d), source="banner_media", raw=path[:120])
            if best is None or hit.date > best.date:
                best = hit
    return best


def extract_page_mtime(
    html: str | None,
    *,
    visible_text: str | None = None,
    prefer_labeled: bool = True,
) -> PageMtimeHit | None:
    """
    综合抽取页面更新/发布时间。
    prefer_labeled=True 时先扫可见「更新日期」标签与「日期 最新」文案，再扫源码线索。
    """
    if prefer_labeled:
        for extract_fn in (extract_labeled_update_date, extract_inline_latest_publish_date):
            hit = extract_fn(visible_text or "")
            if hit:
                return hit
            # 源码里也可能有标签/内联文案
            hit = extract_fn(html or "")
            if hit:
                return hit
    for fn in (
        extract_platform_version_date,
        extract_logo_media_date,
        extract_banner_media_date,
    ):
        hit = fn(html)
        if hit:
            return hit
    return None


def apply_page_mtime_to_result(result, html: str | None) -> None:
    """仅在 extras 尚无 published_at / list_updated_at 时填入源码日期。"""
    if not html:
        return
    extras = result.extras if isinstance(getattr(result, "extras", None), dict) else {}
    if extras.get("published_at") or extras.get("list_updated_at") or extras.get("open_at"):
        return
    hit = extract_page_mtime(html)
    if not hit:
        return
    extras = dict(extras)
    extras["published_at"] = hit.date
    extras["list_updated_at"] = hit.date
    extras["page_mtime_source"] = hit.source
    extras["page_mtime_raw"] = hit.raw
    result.extras = extras
