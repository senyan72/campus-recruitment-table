"""通用 HTML 适配器：详情解析 + 列表表格行枚举。"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from app.collector.adapters.base import ParseResult
from app.collector.extract import (
    classify_from_text,
    dig_title_jd,
    extract_longest_text_block,
    extract_og_title,
    find_embedded_json,
    soup_from_html,
)
from app.collector.filters import (
    extract_job_tags,
    is_chrome_shell_title,
    is_job_detail_link,
    is_noise_location,
    is_noise_nav_url,
    is_noise_title,
    recover_location_from_jd,
    recover_title_from_jd,
    sanitize_job_title,
    strip_career_nav_boilerplate,
)

CITY_RE = re.compile(
    r"(?:工作地点|工作地|工作城市|办公地点|办公地址|所在城市|"
    r"base\s*地|上班地点|地点)[:：\s]*([^\n；;|｜]{2,60})",
    re.I,
)

# 列表表头常见列名
TITLE_HEADERS = ("职位名称", "岗位名称", "职位", "岗位", "job title", "position", "title")
CATEGORY_HEADERS = ("招聘类别", "招聘类型", "招聘项目", "类别", "职位类别", "岗位类别")
TYPE_HEADERS = (
    "职位类型",
    "岗位类型",
    "工作类型",
    "序列",
    "职能类别",
    "job type",
)
LOCATION_HEADERS = ("工作地点", "工作城市", "地点", "城市", "base地", "base", "location")
DATE_HEADERS = (
    "岗位发布时间",
    "更新日期",
    "更新时间",
    "发布时间",
    "发布日期",
    "开招日期",
    "date",
    "updated",
)
HEADCOUNT_HEADERS = ("招聘人数", "需求人数", "用人名额", "招聘名额", "人数", "headcount")


def _norm_header(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip()).lower()


def _header_kind(cell: str) -> str | None:
    h = _norm_header(cell)
    if not h:
        return None
    # 先精确匹配，避免「职位」吞掉「职位类别」
    groups = (
        ("title", TITLE_HEADERS),
        ("category", CATEGORY_HEADERS),
        ("job_type", TYPE_HEADERS),
        ("location", LOCATION_HEADERS),
        ("date", DATE_HEADERS),
        ("headcount", HEADCOUNT_HEADERS),
    )
    for name, group in groups:
        for g in group:
            if _norm_header(g) == h:
                return name
    # 再模糊：非 title 优先（类别/地点/人数/日期），title 短词最后
    for name, group in groups:
        if name == "title":
            continue
        for g in group:
            ng = _norm_header(g)
            if ng in h or h in ng:
                return name
    for g in TITLE_HEADERS:
        ng = _norm_header(g)
        # 「职位」「岗位」仅精确；「职位名称」等可模糊
        if len(ng) <= 2:
            continue
        if ng in h or h in ng:
            return "title"
    return None


def _cell_texts(tr) -> list[str]:
    cells = tr.find_all(["th", "td"], recursive=False)
    if not cells:
        cells = tr.find_all(["th", "td"])
    return [c.get_text(" ", strip=True) for c in cells]


def _row_link(tr, base_url: str) -> str | None:
    for a in tr.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        text = a.get_text(" ", strip=True)
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        if is_noise_nav_url(href, text):
            continue
        full = urljoin(base_url, href)
        if urlparse(full).scheme not in ("http", "https"):
            continue
        if is_job_detail_link(full, text) or text:
            return full.split("#")[0]
    return None


def synthetic_job_url(list_url: str, title: str, *identity_parts: str | None) -> str:
    """Build a stable identity URL for rows without their own detail link."""
    raw = (list_url or "").split("#")[0].strip()
    identity = " | ".join(
        str(part or "").strip()
        for part in (title, *identity_parts)
        if str(part or "").strip()
    )[:240]
    try:
        p = urlparse(raw)
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != "__job"]
        q.append(("__job", identity))
        return urlunparse((p.scheme, p.netloc, p.path or "", p.params, urlencode(q), ""))
    except Exception:
        return f"{raw}?__job={identity}"


_synthetic_job_url = synthetic_job_url


def enumerate_job_table_rows(
    base_url: str,
    html: str,
    *,
    limit: int = 40,
    channel_project: str | None = None,
    channel_bucket: str | None = None,
) -> list[ParseResult]:
    """
    从列表页 HTML 表格解析多岗位（优先职位名称列）。
    典型表头：职位名称 | 职位类别 | 工作地点 | 招聘人数 | 工作类型 | 更新日期
    （hotjob/wecruit campus / interns.html 等同构表）
    """
    soup = soup_from_html(html)
    out: list[ParseResult] = []
    seen_keys: set[str] = set()

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header_map: dict[str, int] = {}
        data_start = 0
        # 前两行内找表头
        for hi in range(min(2, len(rows))):
            texts = _cell_texts(rows[hi])
            mapping: dict[str, int] = {}
            for idx, cell in enumerate(texts):
                kind = _header_kind(cell)
                if kind and kind not in mapping:
                    mapping[kind] = idx
            if "title" in mapping and len(mapping) >= 1:
                header_map = mapping
                data_start = hi + 1
                break
        if "title" not in header_map:
            continue

        for tr in rows[data_start:]:
            cells = _cell_texts(tr)
            if not cells:
                continue
            ti = header_map["title"]
            if ti >= len(cells):
                continue
            title = sanitize_job_title((cells[ti] or "").strip())
            if not title or is_noise_title(title):
                continue
            # 跳过仍像表头的行
            if _header_kind(title) == "title":
                continue

            def _col(kind: str) -> str | None:
                i = header_map.get(kind)
                if i is None or i >= len(cells):
                    return None
                v = (cells[i] or "").strip()
                return v or None

            category = _col("category")
            job_type = _col("job_type")
            location = _col("location")
            if is_noise_location(location):
                location = None
            updated = _col("date")
            hc_raw = _col("headcount")
            headcount = None
            if hc_raw:
                from app.collector.label_fields import normalize_headcount

                headcount = normalize_headcount(hc_raw)
            detail = _row_link(tr, base_url)
            apply_url = detail or synthetic_job_url(
                base_url, title, location, category, job_type
            )
            key = f"{title}|{apply_url}"
            if key in seen_keys:
                continue
            seen_keys.add(key)

            project, bucket, tags = classify_from_text(
                title, f"{category or ''} {job_type or ''} {location or ''}"
            )
            if channel_project:
                project = channel_project
                bucket = channel_bucket
            elif category and not project:
                project = category
            if channel_project == "社会招聘":
                project = "社会招聘"
                bucket = None
            tags = tags or extract_job_tags(title)
            if job_type and job_type not in tags:
                tags = list(tags) + [job_type]

            extras: dict = {
                "adapter": "generic",
                "source": "html_job_table",
                "needs_fetch": bool(detail),
                "list_url": base_url,
                "from_table": True,
            }
            if updated:
                extras["published_at"] = updated
                extras["list_updated_at"] = updated

            out.append(
                ParseResult(
                    title=title,
                    jd_text=None,
                    recruit_project=project or category,
                    recruit_bucket=bucket
                    or ("校招" if category and "校园" in category else None),
                    work_location=location,
                    job_tags=tags,
                    raw_category=category or job_type,
                    headcount=headcount,
                    parse_status="ok",
                    confidence=0.55 if detail else 0.45,
                    apply_url=apply_url,
                    extras=extras,
                )
            )
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    if out:
        from app.collector.page_mtime import extract_page_mtime

        page_hit = extract_page_mtime(html, prefer_labeled=True)
        if page_hit:
            for p in out:
                ex = p.extras if isinstance(p.extras, dict) else {}
                if not ex.get("list_updated_at") and not ex.get("published_at"):
                    ex = dict(ex)
                    ex["list_updated_at"] = page_hit.date
                    ex["published_at"] = page_hit.date
                    ex["page_mtime_source"] = page_hit.source
                    p.extras = ex
    return out


def parse_generic(url: str, html: str) -> ParseResult:
    soup = soup_from_html(html)
    raw_title = extract_og_title(soup)
    jd = None
    location = None
    for blob in find_embedded_json(html):
        t, d = dig_title_jd(blob)
        raw_title = raw_title or t
        jd = jd or d
        if raw_title and jd:
            break
    if not jd:
        jd = extract_longest_text_block(soup)
    # 页面 chrome（Search Jobs / 招贤纳才）不得当岗位名；优先正文/岗位码
    title = sanitize_job_title(raw_title)
    if not title or is_noise_title(raw_title or ""):
        title = sanitize_job_title(recover_title_from_jd(jd)) or title
    if is_noise_title(title):
        title = None
    # 仅非 chrome 的噪声标题片段可并入 JD；Search Jobs 等不得污染岗位介绍
    if (
        raw_title
        and is_noise_title(raw_title)
        and not is_chrome_shell_title(raw_title)
        and jd
        and raw_title not in jd[:200]
    ):
        jd = f"{raw_title}\n{jd}"
    jd = strip_career_nav_boilerplate(jd) or jd
    if jd:
        m = CITY_RE.search(jd[:2000])
        if m and not is_noise_location(m.group(1).strip()):
            location = m.group(1).strip()
        location = location or recover_location_from_jd(jd)
    if is_noise_location(location):
        location = None
    project, bucket, tags = classify_from_text(title, jd)
    tags = tags or extract_job_tags(title)
    conf = 0.3
    if title:
        conf += 0.2
    if jd and len(jd) > 80:
        conf += 0.2
    if bucket:
        conf += 0.1
    if location:
        conf += 0.05
    status = "ok" if title else "needs_browser"
    return ParseResult(
        title=title,
        jd_text=jd,
        recruit_project=project,
        recruit_bucket=bucket,
        work_location=location,
        job_tags=tags,
        parse_status=status,
        confidence=min(conf, 0.85),
        apply_url=url,
        extras={"adapter": "generic", "raw_page_title": raw_title},
    )
