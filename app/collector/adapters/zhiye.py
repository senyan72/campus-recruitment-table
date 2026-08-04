"""北森/智联校园（zhiye / italent）适配器：列表枚举 + 详情四字段。"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.collector.adapters.base import ParseResult
from app.collector.adapters.generic import parse_generic
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
    is_noise_nav_url,
    is_noise_title,
    recover_location_from_jd,
    recover_title_from_jd,
)

DETAIL_HREF_RE = re.compile(
    r"(?:/job(?:s)?/(?:detail|info)/[^\"'\s?#]+|"
    r"/(?:campus|intern|social)/jobs/[^\"'\s?#]+|"
    r"/recruitment/detail[^\"'\s]*|"
    r"/position/detail[^\"'\s]*|"
    r"jobadid=[^\"'\s&#]+|"
    r"[?&](?:postId|jobId|positionId)=[^\"'\s&#]+)",
    re.I,
)

_JOBAD_API_PATH = "/api/JobAd/GetJobAdPageList"

_SPA_SHELL_RE = re.compile(
    r"recruitment-portal|id=[\"']app[\"']|You need to enable JavaScript",
    re.I,
)

_CATEGORY_BY_CHANNEL: dict[str, list[list[str]]] = {
    "campus": [["2"], ["3"]],
    "intern": [["3"]],
    "social": [["1"]],
}

_API_HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Content-Type": "application/json",
}


def can_handle(url: str) -> bool:
    u = (url or "").lower()
    return "zhiye.com" in u or "italent.cn" in u or "tms.beisen.com" in u


def origin_from_url(url: str) -> str:
    p = urlparse(url or "")
    if p.scheme and p.netloc:
        return f"{p.scheme}://{p.netloc}"
    return ""


def list_path_prefix(url: str) -> str:
    """从列表 URL 推断详情 path 前缀，如 /campus/jobs。"""
    parts = [x for x in (urlparse(url or "").path or "").split("/") if x]
    if not parts:
        return "/campus/jobs"
    head = parts[0].lower()
    if head in ("campus", "intern", "social", "school"):
        sub = parts[1].lower() if len(parts) > 1 else "jobs"
        if sub not in ("jobs", "job"):
            sub = "jobs"
        return f"/{head}/{sub}"
    if len(parts) >= 2 and parts[1].lower() in ("jobs", "job"):
        return f"/{parts[0]}/{parts[1].lower()}"
    return f"/{parts[0]}/jobs"


def categories_from_url(url: str) -> list[list[str]]:
    """按 URL 频道返回需请求的 Category 批次（北森：1≈社招, 2≈校招, 3≈实习）。"""
    from app.collector.portal_nav import channel_from_url

    ch = channel_from_url(url) or "campus"
    return _CATEGORY_BY_CHANNEL.get(ch, [["2"], ["3"], ["1"]])


def is_spa_list_shell(html: str | None) -> bool:
    """静态 HTML 为 recruitment-portal SPA 壳，无服务端表格/链接。"""
    if not html:
        return False
    if not _SPA_SHELL_RE.search(html):
        return False
    low = html.lower()
    if "<table" in low and ("职位名称" in html or "岗位名称" in html):
        return False
    if re.search(r"/(?:campus|intern|social)/jobs/[0-9a-f-]{8,}", html, re.I):
        return False
    return True


def _date_ymd(value: Any) -> str | None:
    s = str(value or "").strip()
    if not s or s.startswith("0001-"):
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else None


def _jd_from_row(row: dict[str, Any]) -> str | None:
    duty = (row.get("Duty") or "").strip()
    require = (row.get("Require") or "").strip()
    parts: list[str] = []
    if duty:
        parts.append(f"岗位职责\n{duty}")
    if require:
        parts.append(f"任职要求\n{require}")
    if not parts:
        return None
    return "\n\n".join(parts)[:20000]


def _location_from_row(row: dict[str, Any]) -> str | None:
    locs = row.get("LocNames")
    if isinstance(locs, list):
        names = [str(x).strip() for x in locs if x and str(x).strip()]
        if names:
            return " / ".join(names)
    return None


def _category_bucket(category_id: str | None) -> tuple[str | None, str | None]:
    cid = str(category_id or "").strip()
    if cid == "3":
        return "实习生招聘", "日常实习"
    if cid == "2":
        return "校园招聘", "校招"
    if cid == "1":
        return "社会招聘", None
    return None, None


def detail_url_for_row(list_url: str, row_id: str, job_ad_id: Any = None) -> str:
    """Build the current Beisen detail route.

    New recruitment-portal tenants route detail pages through the numeric
    ``jobAdId`` query parameter. Keep the UUID path as a compatibility
    fallback for older tenants and callers that do not have ``JobAdId``.
    """
    origin = origin_from_url(list_url)
    channel = (list_path_prefix(list_url).split("/", 2)[1] or "campus").lower()
    numeric = str(job_ad_id or "").strip()
    if numeric.isdigit():
        return f"{origin}/{channel}/detail?jobAdId={numeric}"
    prefix = list_path_prefix(list_url)
    return f"{origin}{prefix}/{row_id}"


def jobad_row_to_result(row: dict[str, Any], *, list_url: str) -> ParseResult | None:
    title = (row.get("JobAdName") or "").strip() or None
    if is_noise_title(title):
        title = None
    jd = _jd_from_row(row)
    if not title and jd:
        title = recover_title_from_jd(jd)
    if is_noise_title(title):
        title = None

    row_id = str(row.get("Id") or "").strip()
    if not row_id:
        return None

    location = _location_from_row(row)
    cat_project, cat_bucket = _category_bucket(row.get("CategoryId"))
    project, bucket, tags = classify_from_text(title, jd)
    project = project or cat_project
    bucket = bucket or cat_bucket
    tags = tags or extract_job_tags(title)

    apply_url = detail_url_for_row(list_url, row_id, row.get("JobAdId"))
    open_at = _date_ymd(row.get("ChangeDate") or row.get("PostDate"))

    conf = 0.55
    if title:
        conf += 0.15
    if jd and len(jd) > 40:
        conf += 0.2
    if location:
        conf += 0.05

    extras: dict[str, Any] = {
        "adapter": "zhiye",
        "source": "zhiye_api",
        "job_ad_id": row.get("JobAdId"),
        "category_id": row.get("CategoryId"),
    }
    if open_at:
        extras["open_at"] = open_at
    if jd and len(jd) > 40:
        extras["needs_fetch"] = False
    else:
        extras["needs_fetch"] = True

    degree = row.get("Degree")
    salary = row.get("Salary")
    headcount = row.get("HeadCount")

    return ParseResult(
        title=title,
        jd_text=jd,
        recruit_project=project,
        recruit_bucket=bucket,
        work_location=location,
        job_tags=tags,
        education=str(degree).strip() if degree else None,
        salary_range=str(salary).strip() if salary else None,
        headcount=str(headcount).strip() if headcount not in (None, 0, "0") else None,
        parse_status="ok" if title else "needs_browser",
        confidence=min(conf, 0.95),
        apply_url=apply_url,
        extras=extras,
    )


def _safe_referer(list_url: str, origin: str) -> str:
    """Referer 仅用 origin+path，避免 LocId 等 query 含中文导致 httpx 头编码失败。"""
    p = urlparse(list_url or origin)
    path = p.path or "/"
    return f"{origin}{path}"


def _api_headers(origin: str, list_url: str) -> dict[str, str]:
    return {
        **_API_HEADERS_BASE,
        "Origin": origin,
        "Referer": _safe_referer(list_url, origin),
    }


def fetch_jobad_page_list(
    list_url: str,
    *,
    page_index: int = 0,
    page_size: int = 50,
    category: list[str] | None = None,
    timeout: float = 30.0,
) -> tuple[list[dict[str, Any]], int | None]:
    """
    POST {origin}/api/JobAd/GetJobAdPageList。
    响应可能带 UTF-8 BOM，用 utf-8-sig 解码。
    """
    origin = origin_from_url(list_url)
    if not origin:
        return [], None
    url = f"{origin}{_JOBAD_API_PATH}"
    body: dict[str, Any] = {
        "PageIndex": page_index,
        "PageSize": max(1, min(int(page_size), 50)),
        "KeyWords": "",
        "SpecialType": 0,
    }
    if category:
        body["Category"] = category
    try:
        with httpx.Client(
            headers=_api_headers(origin, list_url),
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            resp = client.post(url, json=body)
            if resp.status_code >= 400:
                return [], None
            text = resp.content.decode("utf-8-sig")
            data = json.loads(text)
    except Exception:  # noqa: BLE001
        return [], None
    if not isinstance(data, dict) or data.get("Code") not in (200, "200"):
        return [], None
    rows = data.get("Data") or []
    jobs = [r for r in rows if isinstance(r, dict)]
    total = None
    if data.get("Count") is not None:
        try:
            total = int(data.get("Count"))
        except (TypeError, ValueError):
            total = None
    return jobs, total


def enumerate_positions_via_api(
    list_url: str,
    *,
    limit: int = 40,
    max_pages: int = 8,
    page_size: int = 50,
    timeout: float = 30.0,
) -> list[ParseResult]:
    out: list[ParseResult] = []
    seen_ids: set[str] = set()
    for cat_batch in categories_from_url(list_url):
        for page_idx in range(max_pages):
            rows, total = fetch_jobad_page_list(
                list_url,
                page_index=page_idx,
                page_size=page_size,
                category=cat_batch,
                timeout=timeout,
            )
            if not rows:
                break
            for row in rows:
                rid = str(row.get("Id") or "").strip()
                if not rid or rid in seen_ids:
                    continue
                seen_ids.add(rid)
                pr = jobad_row_to_result(row, list_url=list_url)
                if pr:
                    out.append(pr)
                if len(out) >= limit:
                    return out
            if total is not None and (page_idx + 1) * page_size >= total:
                break
            if len(rows) < page_size:
                break
    return out


def extract_detail_links(base_url: str, html: str, *, limit: int = 40) -> list[str]:
    soup = soup_from_html(html)
    seen: set[str] = set()
    out: list[str] = []

    def _add(full: str) -> bool:
        full = full.split("#")[0].strip()
        if not full or full in seen or is_noise_nav_url(full):
            return False
        low = full.lower()
        if not DETAIL_HREF_RE.search(low) and not re.search(
            r"(detail|jobadid|postid|jobid|positionid)", low
        ):
            return False
        seen.add(full)
        out.append(full)
        return len(out) >= limit

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ", strip=True)
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        if is_noise_nav_url(href, text) or is_noise_title(text):
            continue
        full = urljoin(base_url, href)
        if urlparse(full).scheme not in ("http", "https"):
            continue
        if _add(full):
            return out

    for m in re.finditer(
        r"https?://[^\"'\s]+?(?:job|position|recruit)[^\"'\s]*(?:detail|jobadid)[^\"'\s]*",
        html or "",
        re.I,
    ):
        if _add(m.group(0)):
            return out
    return out


def _location_from_blob(blob: dict) -> str | None:
    for key in ("Location", "locations", "city", "City", "workLocation", "WorkLocation"):
        v = blob.get(key)
        if isinstance(v, list):
            parts = [str(x.get("Name") or x.get("name") or x).strip() for x in v if x]
            parts = [p for p in parts if p and p != "None"]
            if parts:
                return " / ".join(parts)
        if isinstance(v, dict):
            name = v.get("Name") or v.get("name")
            if name:
                return str(name).strip()
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def parse_zhiye_detail(url: str, html: str) -> ParseResult:
    soup = soup_from_html(html)
    title = extract_og_title(soup)
    jd = None
    location = None
    for blob in find_embedded_json(html):
        t, d = dig_title_jd(blob)
        title = title or t
        jd = jd or d
        if isinstance(blob, dict):
            location = location or _location_from_blob(blob)

    if not jd:
        for sel in ("[class*='description']", "[class*='detail']", "article", "main"):
            el = soup.select_one(sel)
            if el:
                text = el.get_text("\n", strip=True)
                if text and len(text) > 40:
                    jd = text[:20000]
                    break
    if not jd:
        jd = extract_longest_text_block(soup)
    if is_noise_title(title):
        title = recover_title_from_jd(jd)
    if is_noise_title(title):
        title = None
    location = location or recover_location_from_jd(jd)
    if not title and not jd:
        return parse_generic(url, html)

    project, bucket, tags = classify_from_text(title, jd)
    tags = tags or extract_job_tags(title)
    conf = 0.55
    if title:
        conf += 0.15
    if jd and len(jd) > 40:
        conf += 0.2
    if location:
        conf += 0.05
    return ParseResult(
        title=title,
        jd_text=jd,
        recruit_project=project,
        recruit_bucket=bucket,
        work_location=location,
        job_tags=tags,
        parse_status="ok" if title else "needs_browser",
        confidence=min(conf, 0.95),
        apply_url=url,
        extras={"adapter": "zhiye", "source": "zhiye_detail"},
    )


def enumerate_positions(
    list_url: str,
    html: str,
    *,
    limit: int = 40,
    fetch_api: bool = True,
    max_pages: int = 8,
    page_size: int = 50,
    timeout: float = 30.0,
) -> list[ParseResult]:
    from app.collector.adapters.generic import enumerate_job_table_rows

    table_posts = enumerate_job_table_rows(list_url, html, limit=limit)
    if table_posts:
        for p in table_posts:
            p.extras["adapter"] = "zhiye"
            if p.apply_url and "__job=" not in (p.apply_url or ""):
                p.extras["needs_fetch"] = True
        return table_posts

    links = extract_detail_links(list_url, html, limit=limit)
    if links:
        return [
            ParseResult(
                title=None,
                apply_url=link,
                parse_status="needs_browser",
                confidence=0.2,
                extras={"source": "zhiye_html_link", "needs_fetch": True, "adapter": "zhiye"},
            )
            for link in links
        ]

    # Some tenants (for example iflytek.zhiye.com) serve a full branded home page
    # with no job links/table. The public JobAd API is still the canonical list.
    if fetch_api:
        api_posts = enumerate_positions_via_api(
            list_url,
            limit=limit,
            max_pages=max_pages,
            page_size=page_size,
            timeout=timeout,
        )
        if api_posts:
            return api_posts

    return []


def parse_zhiye(url: str, html: str) -> ParseResult:
    low = (url or "").lower()
    if re.search(r"(detail|jobadid|postid=|jobid=|positionid=)", low):
        return parse_zhiye_detail(url, html)
    if re.search(r"/(?:campus|intern|social)/jobs/[0-9a-f-]{8,}", low):
        return parse_zhiye_detail(url, html)
    posts = enumerate_positions(url, html, limit=3, fetch_api=True, max_pages=1)
    if posts:
        return ParseResult(
            title=None,
            parse_status="needs_browser",
            confidence=0.15,
            apply_url=url,
            extras={"source": "zhiye_list", "is_list": True, "detail_links": len(posts)},
        )
    return parse_zhiye_detail(url, html)
