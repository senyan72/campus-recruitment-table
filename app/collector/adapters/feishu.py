"""飞书招聘（*.jobs.feishu.cn）适配器：列表枚举 + 详情四字段。"""

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
    extract_og_title,
    find_embedded_json,
    soup_from_html,
)
from app.collector.filters import is_noise_nav_url, is_noise_title, map_recruit_bucket

# /campus/position/{id}/detail 或 /379481/position/{id}/detail
POSITION_DETAIL_RE = re.compile(
    r"(https?://[^/\s\"']+)?(/[^\"'\s]*)?/position/([^/\"'?#]+)/detail",
    re.I,
)
# 卡片元信息：城市 | 实习/正式 | 类别
CARD_META_RE = re.compile(
    r"([^\n|｜]{1,40}?)\s*[|｜]\s*(实习|正式|社招|校招|全职|兼职)\s*[|｜]\s*([^\n|｜]{1,40})",
)
NAV_PATH_SKIP = frozenset(
    {
        "login",
        "logout",
        "register",
        "user",
        "account",
        "help",
        "about",
        "privacy",
        "api",
        "v1",
        "referral",
        "share",
        "passport",
    }
)


def can_handle(url: str) -> bool:
    u = (url or "").lower()
    if "jobs.feishu.cn" in u or "jobs.f.mioffice.cn" in u:
        return True
    if "feishu.cn/hire" in u:
        return True
    # 其它 *.feishu.cn 仅在明显招聘路径时接管，避免误伤文档站
    if ".feishu.cn" in u and any(
        p in u for p in ("/position/", "/campus", "/internship", "/society", "/index/")
    ):
        return True
    return False


def is_detail_url(url: str) -> bool:
    return bool(re.search(r"/position/[^/]+/detail", url or "", re.I))


def extract_host_channel(url: str) -> tuple[str, str]:
    """从列表/详情 URL 推断 host 与 portal-channel。"""
    p = urlparse(url or "")
    host = p.netloc.lower()
    parts = [x for x in (p.path or "").split("/") if x]
    channel = "campus"
    if parts:
        head = parts[0].lower()
        if head == "position":
            channel = "index"
        elif head not in NAV_PATH_SKIP and head not in ("job", "jobs", "detail"):
            channel = parts[0]
    return host, channel


def detail_url(host: str, channel: str, post_id: str) -> str:
    ch = (channel or "campus").strip("/") or "campus"
    return f"https://{host}/{ch}/position/{post_id}/detail"


def extract_detail_links(base_url: str, html: str, *, limit: int = 80) -> list[str]:
    """从列表页 HTML 抽取 /position/{id}/detail，过滤登录等导航。"""
    host, channel = extract_host_channel(base_url)
    seen: set[str] = set()
    out: list[str] = []

    for m in POSITION_DETAIL_RE.finditer(html or ""):
        pid = m.group(3)
        if not pid or pid in seen:
            continue
        full = m.group(0)
        if full.startswith("http"):
            url = full.split("?")[0]
        else:
            path = m.group(2) or f"/{channel}"
            url = urljoin(f"https://{host}", f"{path}/position/{pid}/detail")
            if "/position/" not in url:
                url = detail_url(host, channel, pid)
        if is_noise_nav_url(url):
            continue
        seen.add(pid)
        out.append(url.split("#")[0])
        if len(out) >= limit:
            return out

    soup = soup_from_html(html)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ", strip=True)
        if not href or is_noise_nav_url(href, text) or is_noise_title(text):
            continue
        full = urljoin(base_url, href)
        m = re.search(r"/position/([^/]+)/detail", full, re.I)
        if not m:
            continue
        pid = m.group(1)
        if pid in seen:
            continue
        seen.add(pid)
        out.append(full.split("?")[0].split("#")[0])
        if len(out) >= limit:
            break
    return out


def map_feishu_job_type(recruit_type_name: str | None, title: str | None = None) -> tuple[str, str]:
    """岗位类型 → (recruit_project, recruit_bucket)。"""
    name = (recruit_type_name or "").strip()
    text = f"{name} {title or ''}"
    if "实习" in text:
        return (name or "实习", "日常实习")
    if any(k in text for k in ("正式", "校招", "全职", "校园")):
        return (name or "正式", "校招")
    if "社招" in text or "社会招聘" in text:
        return (name or "社招", "校招")
    project, bucket, _ = classify_from_text(title, name)
    project = project or name or "校园招聘"
    bucket = bucket or map_recruit_bucket(project, title) or "校招"
    return project, bucket


def _cities_from_item(item: dict[str, Any]) -> str | None:
    cities = item.get("city_list") or []
    names: list[str] = []
    if isinstance(cities, list):
        for c in cities:
            if isinstance(c, dict) and c.get("name"):
                names.append(str(c["name"]).strip())
            elif isinstance(c, str) and c.strip():
                names.append(c.strip())
    if names:
        return " / ".join(names)
    info = item.get("city_info")
    if isinstance(info, dict) and info.get("name"):
        return str(info["name"]).strip()
    return None


def _jd_from_item(item: dict[str, Any]) -> str | None:
    parts: list[str] = []
    for key in ("description", "requirement", "job_description", "detail"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    if not parts:
        return None
    # 去重拼接
    seen: set[str] = set()
    uniq: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return "\n\n".join(uniq)[:20000]


def job_post_to_result(item: dict[str, Any], *, apply_url: str) -> ParseResult:
    title = (item.get("title") or item.get("name") or "").strip() or None
    jd = _jd_from_item(item)
    location = _cities_from_item(item)
    recruit_type = item.get("recruit_type") or {}
    type_name = None
    if isinstance(recruit_type, dict):
        type_name = recruit_type.get("name") or recruit_type.get("zh_cn")
    elif isinstance(recruit_type, str):
        type_name = recruit_type
    category = None
    for key in ("job_category", "job_function"):
        obj = item.get(key)
        if isinstance(obj, dict) and obj.get("name"):
            category = str(obj["name"]).strip()
            break
    project, bucket = map_feishu_job_type(type_name, title)
    if not bucket:
        project2, bucket2, _ = classify_from_text(title, jd)
        project = project or project2
        bucket = bucket or bucket2
    tags = []
    from app.collector.filters import extract_job_tags

    tags = extract_job_tags(title)
    conf = 0.55
    if title:
        conf += 0.15
    if jd and len(jd) > 40:
        conf += 0.2
    if location:
        conf += 0.05
    if type_name:
        conf += 0.05
    return ParseResult(
        title=title,
        jd_text=jd,
        recruit_project=project,
        recruit_bucket=bucket,
        work_location=location,
        job_tags=tags,
        raw_category=category or type_name,
        parse_status="ok" if title else "needs_browser",
        confidence=min(conf, 0.95),
        apply_url=apply_url,
        extras={"feishu_recruit_type": type_name, "source": "feishu_api"},
    )


def _api_headers(host: str, channel: str) -> dict[str, str]:
    ch = channel or "campus"
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "portal-channel": ch,
        "portal-platform": "pc",
        "website-path": ch,
        "Referer": f"https://{host}/{ch}/position",
        "Origin": f"https://{host}",
    }


def search_job_posts(
    host: str,
    channel: str,
    *,
    page: int = 1,
    page_size: int = 50,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """POST /api/v1/search/job/posts；失败返回空列表。"""
    if not host:
        return []
    offset = max(0, (page - 1) * page_size)
    payload = {
        "keyword": "",
        "limit": page_size,
        "offset": offset,
        "portal_type": 3,
        "portal_entrance": 1,
        "language": "zh",
    }
    url = f"https://{host}/api/v1/search/job/posts"
    try:
        with httpx.Client(headers=_api_headers(host, channel), timeout=timeout, follow_redirects=True) as client:
            resp = client.post(url, content=json.dumps(payload))
            if resp.status_code >= 400:
                return []
            data = resp.json()
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, dict) or data.get("code") not in (0, "0", None):
        # 部分租户 code!=0 表示 channel 错误
        if isinstance(data, dict) and data.get("code") not in (0, "0"):
            return []
    body = data.get("data") if isinstance(data, dict) else None
    if not isinstance(body, dict):
        return []
    rows = body.get("job_post_list") or []
    return [r for r in rows if isinstance(r, dict)]


def enumerate_positions(
    list_url: str,
    html: str | None = None,
    *,
    max_pages: int = 5,
    page_size: int = 50,
    limit: int = 40,
) -> list[ParseResult]:
    """
    枚举飞书列表岗位，优先 API，其次 HTML 详情链接。
    每条含：岗位名称、JD、岗位类型、base 地。
    """
    host, channel = extract_host_channel(list_url)
    results: list[ParseResult] = []
    seen_ids: set[str] = set()

    channels_to_try = [channel]
    for alt in ("campus", "internship", "index", "society"):
        if alt not in channels_to_try:
            channels_to_try.append(alt)

    for ch in channels_to_try:
        got_any = False
        for page in range(1, max_pages + 1):
            rows = search_job_posts(host, ch, page=page, page_size=page_size)
            if not rows:
                break
            got_any = True
            for item in rows:
                pid = str(item.get("id") or "").strip()
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                apply = detail_url(host, ch, pid)
                results.append(job_post_to_result(item, apply_url=apply))
                if len(results) >= limit:
                    return results
            if len(rows) < page_size:
                break
        if got_any:
            break

    if results:
        return results

    # HTML 回退：只枚举 detail 链接（详情需调用方再抓）
    links = extract_detail_links(list_url, html or "", limit=limit)
    for link in links:
        results.append(
            ParseResult(
                title=None,
                apply_url=link,
                parse_status="needs_browser",
                confidence=0.2,
                extras={"source": "feishu_html_link", "needs_fetch": True},
            )
        )
    return results


def parse_card_meta_from_text(text: str) -> tuple[str | None, str | None, str | None]:
    """解析「城市 | 实习/正式 | 类别」。"""
    m = CARD_META_RE.search(text or "")
    if not m:
        return None, None, None
    return m.group(1).strip(), m.group(2).strip(), m.group(3).strip()


def parse_feishu_detail(url: str, html: str) -> ParseResult:
    """解析单个职位详情页：名称 / JD / 类型 / base 地。"""
    soup = soup_from_html(html)
    title = extract_og_title(soup)
    jd = None
    location = None
    type_name = None
    category = None

    for blob in find_embedded_json(html):
        t, d = dig_title_jd(blob)
        title = title or t
        jd = jd or d
        if isinstance(blob, dict):
            loc = _cities_from_item(blob)
            location = location or loc
            rt = blob.get("recruit_type")
            if isinstance(rt, dict):
                type_name = type_name or rt.get("name")
            for key in ("job_category", "job_function"):
                obj = blob.get(key)
                if isinstance(obj, dict) and obj.get("name"):
                    category = category or str(obj["name"])

    # 页面可见卡片/元信息
    page_text = soup.get_text("\n", strip=True) if soup else ""
    city, typ, cat = parse_card_meta_from_text(page_text[:3000])
    location = location or city
    type_name = type_name or typ
    category = category or cat

    if not jd:
        # 详情正文区块
        for sel in ("article", "[class*='description']", "[class*='detail']", "main"):
            el = soup.select_one(sel) if soup else None
            if el:
                text = el.get_text("\n", strip=True)
                if text and len(text) > 40:
                    jd = text[:20000]
                    break
    if not jd:
        from app.collector.extract import extract_longest_text_block

        jd = extract_longest_text_block(soup)

    if is_noise_title(title):
        title = None

    if not title and not jd:
        return parse_generic(url, html)

    project, bucket = map_feishu_job_type(type_name, title)
    from app.collector.filters import extract_job_tags

    tags = extract_job_tags(title)
    conf = 0.6
    if title:
        conf += 0.15
    if jd and len(jd) > 40:
        conf += 0.15
    if location:
        conf += 0.05
    if type_name:
        conf += 0.05

    return ParseResult(
        title=title,
        jd_text=jd,
        recruit_project=project,
        recruit_bucket=bucket,
        work_location=location,
        job_tags=tags,
        raw_category=category or type_name,
        parse_status="ok" if title else "needs_browser",
        confidence=min(conf, 0.95),
        apply_url=url,
        extras={"feishu_recruit_type": type_name, "source": "feishu_detail"},
    )


def parse_feishu(url: str, html: str) -> ParseResult:
    """路由入口：详情页深挖；列表页尽量用 API 合成首条或回落 generic。"""
    if is_detail_url(url):
        return parse_feishu_detail(url, html)
    # 列表页：尝试 API 取第一条有标题的（单页 parse 语义）；完整枚举走 enumerate_positions
    posts = enumerate_positions(url, html, max_pages=1, limit=3)
    for p in posts:
        if p.title and not p.extras.get("needs_fetch"):
            p.extras["list_page"] = True
            return p
    # 列表壳页勿把导航当标题
    og = extract_og_title(soup_from_html(html))
    if og and not is_noise_title(og) and "飞书" not in og:
        project, bucket, tags = classify_from_text(og, None)
        return ParseResult(
            title=og,
            recruit_project=project,
            recruit_bucket=bucket,
            job_tags=tags,
            parse_status="ok",
            confidence=0.4,
            apply_url=url,
            extras={"source": "feishu_list_shell"},
        )
    return ParseResult(
        title=None,
        parse_status="needs_browser",
        confidence=0.1,
        apply_url=url,
        extras={"source": "feishu_list", "is_list": True},
    )
