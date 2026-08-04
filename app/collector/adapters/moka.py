"""Moka 招聘页适配器：列表枚举 detail + 详情四字段。"""

from __future__ import annotations

import base64
import html as html_lib
import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.collector.adapters._aes_cbc import aes128_cbc_decrypt
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
    sanitize_job_title,
)

DETAIL_HREF_RE = re.compile(
    r"(?:/api/outer/ats-jc-apply/websites/[^\"'\s]+/jobs/[^\"'\s]+|"
    r"/job(?:s)?/[^\"'\s?#]+|"
    r"jobadid=[^\"'\s&#]+|"
    r"/position/[^\"'\s]+)",
    re.I,
)

# #/job/<uuid> 或 path /jobs/<id>
_JOB_ID_IN_URL_RE = re.compile(
    r"(?:[#&?]|/|/jobs?/|jobadid=)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.I,
)
# 支持 /m/campus-recruitment/... 与 /campus-recruitment/...（无 m 前缀）
_CAMPUS_SITE_RE = re.compile(
    r"(https?://[^/]+/(?:m/)?(?:campus-recruitment|social-recruitment|campus_apply|"
    r"recommendation-recruitment)/([^/]+)/(\d+))",
    re.I,
)
_SITE_URL_RE = re.compile(
    r"https?://[^/]+/(?:m/)?(?P<kind>campus-recruitment|social-recruitment|campus_apply|"
    r"recommendation-recruitment)/(?P<org>[^/]+)/(?P<site>\d+)",
    re.I,
)

JOBS_V2_API = "https://app.mokahr.com/api/outer/ats-apply/website/jobs/v2"
JOB_DETAIL_API = "https://app.mokahr.com/api/outer/ats-apply/website/job"
# 上游 limit>50 返回 code 102「参数错误」
MOKA_MAX_PAGE_SIZE = 50

_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def can_handle(url: str) -> bool:
    """识别 Moka 门户（含 envision-career.com 等自定义域名）。"""
    u = (url or "").lower()
    if "mokahr.com" in u or "moka.com" in u:
        return True
    # 自定义域名：/campus-recruitment/{org}/{siteId} 等路径
    return bool(_SITE_URL_RE.search(url or ""))


def parse_site_from_url(url: str) -> tuple[str | None, int | None, str | None]:
    """从门户 URL 解析 (org_slug, site_id, kind)。"""
    m = _SITE_URL_RE.search(url or "")
    if not m:
        return None, None, None
    try:
        site_id = int(m.group("site"))
    except (TypeError, ValueError):
        return m.group("org"), None, m.group("kind")
    return m.group("org"), site_id, m.group("kind")


def job_stats_total(html: str | None = None, data: dict[str, Any] | None = None) -> int | None:
    init = data if isinstance(data, dict) else load_init_data(html)
    if not init:
        return None
    stats = init.get("jobStats")
    if isinstance(stats, dict) and stats.get("total") is not None:
        try:
            return int(stats.get("total"))
        except (TypeError, ValueError):
            return None
    return None


def decrypt_moka_envelope(
    envelope: dict[str, Any] | None,
    aes_iv: str | None,
) -> dict[str, Any] | None:
    """解密 jobs/v2 响应 ``{data, necromancer}``（key=necromancer, iv=aesIv）。"""
    if not isinstance(envelope, dict):
        return None
    data_b64 = envelope.get("data")
    key_s = envelope.get("necromancer")
    iv_s = (aes_iv or "").strip()
    if not isinstance(data_b64, str) or not isinstance(key_s, str) or not iv_s:
        return None
    key = key_s.encode("utf-8")
    iv = iv_s.encode("utf-8")
    if len(key) != 16 or len(iv) != 16:
        return None
    try:
        plain = aes128_cbc_decrypt(base64.b64decode(data_b64), key, iv)
        parsed = json.loads(plain.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return parsed if isinstance(parsed, dict) else None


def fetch_jobs_page(
    org_id: str,
    site_id: int | str,
    *,
    offset: int = 0,
    limit: int = 50,
    aes_iv: str,
    list_url: str | None = None,
    timeout: float = 30.0,
) -> tuple[list[dict[str, Any]], int | None]:
    """
    POST /api/outer/ats-apply/website/jobs/v2?orgId=…
    返回 (jobs, total|None)。失败返回 ([], None)。
    """
    oid = (org_id or "").strip()
    if not oid or not aes_iv:
        return [], None
    page_size = max(1, min(int(limit or 50), MOKA_MAX_PAGE_SIZE))
    off = max(0, int(offset or 0))
    referer = (list_url or f"https://app.mokahr.com/campus-recruitment/{oid}/{site_id}").split("#")[0]
    headers = {
        **_API_HEADERS,
        "Accept": "application/json,*/*",
        "Content-Type": "application/json",
        "Origin": "https://app.mokahr.com",
        "Referer": referer,
    }
    body = {
        "orgId": oid,
        "siteId": str(site_id),
        "limit": page_size,
        "offset": off,
        "needStat": True,
        "locale": "zh-CN",
    }
    url = f"{JOBS_V2_API}?orgId={oid}"
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=timeout) as client:
            resp = client.post(url, json=body)
            if resp.status_code >= 400:
                return [], None
            envelope = resp.json()
    except Exception:  # noqa: BLE001
        return [], None
    decoded = decrypt_moka_envelope(envelope if isinstance(envelope, dict) else None, aes_iv)
    if not decoded or decoded.get("code") not in (0, "0", None):
        return [], None
    data = decoded.get("data")
    if not isinstance(data, dict):
        return [], None
    rows = data.get("jobs") or []
    jobs = [r for r in rows if isinstance(r, dict)]
    total = None
    stats = data.get("jobStats")
    if isinstance(stats, dict) and stats.get("total") is not None:
        try:
            total = int(stats.get("total"))
        except (TypeError, ValueError):
            total = None
    return jobs, total


def _html_fragment_to_text(fragment: str | None) -> str | None:
    """jobDescription 等 HTML 片段 → 纯文本。"""
    raw = (fragment or "").strip()
    if not raw:
        return None
    if "<" in raw and ">" in raw:
        soup = soup_from_html(f"<div>{raw}</div>")
        text = soup.get_text("\n", strip=True)
    else:
        text = html_lib.unescape(raw)
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    return text or None


def fetch_job_detail(
    org_id: str,
    site_id: int | str,
    job_id: str,
    *,
    aes_iv: str,
    list_url: str | None = None,
    timeout: float = 45.0,
) -> dict[str, Any] | None:
    """
    POST /api/outer/ats-apply/website/job?orgId=…
    返回解密后的岗位 dict（含 jobDescription），失败返回 None。
    """
    oid = (org_id or "").strip()
    jid = (job_id or "").strip()
    iv = (aes_iv or "").strip()
    if not oid or not jid or not iv or site_id is None:
        return None
    referer = (list_url or f"https://app.mokahr.com/campus-recruitment/{oid}/{site_id}").split("#")[0]
    headers = {
        **_API_HEADERS,
        "Accept": "application/json,*/*",
        "Content-Type": "application/json",
        "Origin": "https://app.mokahr.com",
        "Referer": referer,
    }
    body = {
        "orgId": oid,
        "siteId": str(site_id),
        "jobId": jid,
        "locale": "zh-CN",
    }
    url = f"{JOB_DETAIL_API}?orgId={oid}"
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=timeout) as client:
            resp = client.post(url, json=body)
            if resp.status_code >= 400:
                return None
            envelope = resp.json()
    except Exception:  # noqa: BLE001
        return None
    decoded = decrypt_moka_envelope(envelope if isinstance(envelope, dict) else None, iv)
    if not decoded or decoded.get("code") not in (0, "0", None):
        return None
    if not decoded.get("success", True) and decoded.get("data") is None:
        return None
    data = decoded.get("data")
    return data if isinstance(data, dict) else None


def parse_job_detail_dict(
    obj: dict[str, Any],
    *,
    list_url: str,
    company: str | None = None,
) -> ParseResult | None:
    """将 website/job 详情 dict 转为 ParseResult（含 JD 正文）。"""
    if not isinstance(obj, dict):
        return None
    # 详情接口用 jobDescription；列表字段兼容 description
    jd_html = obj.get("jobDescription") or obj.get("description") or obj.get("detail")
    jd_s = _html_fragment_to_text(jd_html if isinstance(jd_html, str) else None)
    merged = dict(obj)
    if jd_s:
        merged["description"] = jd_s
    pr = job_dict_to_result(merged, list_url=list_url, company=company, source="moka_job_api")
    if pr is None:
        return None
    pr.extras = dict(pr.extras or {})
    pr.extras["needs_fetch"] = False
    pr.extras["source"] = "moka_job_api"
    if jd_s:
        pr.jd_text = jd_s
    return pr


def fetch_all_jobs_via_api(
    org_id: str,
    site_id: int | str,
    *,
    aes_iv: str,
    list_url: str | None = None,
    limit: int = 200,
    max_pages: int = 20,
    page_size: int = 50,
    timeout: float = 30.0,
    total_hint: int | None = None,
) -> list[dict[str, Any]]:
    """分页拉取 Moka 岗位列表（AES 信封），直到 total / limit / 末页。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    page_size = max(1, min(int(page_size or 50), MOKA_MAX_PAGE_SIZE))
    total = total_hint
    for page in range(max(1, int(max_pages or 1))):
        offset = page * page_size
        rows, page_total = fetch_jobs_page(
            org_id,
            site_id,
            offset=offset,
            limit=page_size,
            aes_iv=aes_iv,
            list_url=list_url,
            timeout=timeout,
        )
        if page_total is not None:
            total = page_total
        if not rows:
            break
        for row in rows:
            jid = str(row.get("id") or "").strip()
            if jid:
                if jid in seen:
                    continue
                seen.add(jid)
            out.append(row)
            if len(out) >= limit:
                return out
        if total is not None and len(out) >= total:
            break
        if len(rows) < page_size:
            break
    return out


def load_init_data(html: str | None) -> dict[str, Any] | None:
    """解析 Moka 页内 ``<input id="init-data">``（HTML 实体编码的 JSON）。"""
    if not html:
        return None
    soup = soup_from_html(html)
    el = soup.find("input", id="init-data")
    raw = (el.get("value") if el else None) or ""
    if not raw.strip():
        # 回退：裸 value="{&quot;…}"
        m = re.search(
            r'<input[^>]+id=["\']init-data["\'][^>]+value=["\'](.*?)["\']',
            html,
            re.I | re.S,
        )
        raw = m.group(1) if m else ""
    if not raw.strip():
        return None
    try:
        data = json.loads(html_lib.unescape(raw))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def extract_embedded_jobs(html: str | None) -> list[dict[str, Any]]:
    """从 init-data / 解实体后的 JSON 抽取岗位对象列表（HTML 源码优先，不访问 API）。"""
    data = load_init_data(html)
    jobs: list[dict[str, Any]] = []
    if data:
        raw_jobs = data.get("jobs")
        if isinstance(raw_jobs, list):
            jobs.extend(x for x in raw_jobs if isinstance(x, dict) and x.get("title"))
    if jobs:
        return jobs

    # 回退：整页解实体后找 "jobs":[…]
    text = html_lib.unescape(html or "")
    for m in re.finditer(r'"jobs"\s*:\s*\[', text):
        arr = _slice_json_array(text, m.end() - 1)
        if not arr:
            continue
        try:
            parsed = json.loads(arr)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, list):
            continue
        for item in parsed:
            if isinstance(item, dict) and item.get("title") and (
                item.get("id") or item.get("mjCode") or item.get("publishedAt")
            ):
                jobs.append(item)
        if jobs:
            break
    return jobs


def _slice_json_array(text: str, start: int) -> str | None:
    if start < 0 or start >= len(text) or text[start] != "[":
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def company_from_init_data(html: str | None) -> str | None:
    data = load_init_data(html)
    if not data:
        return None
    org = data.get("org")
    if isinstance(org, dict):
        for key in ("name", "displayName"):
            v = str(org.get(key) or "").strip()
            if v and v.lower() != "ey":  # 优先中文名
                return v
        v = str(org.get("displayName") or org.get("name") or "").strip()
        return v or None
    return None


def detail_url_for_job(list_url: str, job_id: str) -> str:
    """拼 Moka 校园/社招门户详情 hash 链接。"""
    jid = (job_id or "").strip()
    base = (list_url or "").strip()
    if not jid:
        return base
    m = _CAMPUS_SITE_RE.search(base)
    if m:
        root = m.group(1).split("?")[0].rstrip("/")
        return f"{root}#/job/{jid}"
    # 已有 #/job/ 则替换；否则附加
    if "#/job/" in base.lower():
        return re.sub(r"#/job/[^/?#]+", f"#/job/{jid}", base, flags=re.I)
    if "#" in base:
        return f"{base.split('#', 1)[0]}#/job/{jid}"
    return f"{base}#/job/{jid}"


def extract_detail_links(base_url: str, html: str, *, limit: int = 40) -> list[str]:
    """从 Moka 列表页抽取职位详情链接。"""
    soup = soup_from_html(html)
    seen: set[str] = set()
    out: list[str] = []

    def _add(full: str) -> bool:
        full = full.split("#")[0].strip() if "__job=" not in full else full.strip()
        # 保留 hash 详情
        if "#/job/" in (full or "").lower():
            pass
        else:
            full = full.split("#")[0].strip()
        if not full or full in seen:
            return False
        if is_noise_nav_url(full):
            return False
        low = full.lower()
        if not DETAIL_HREF_RE.search(low) and "jobadid" not in low and "#/job/" not in low:
            return False
        # 排除纯列表根
        if low.rstrip("/").endswith(("/jobs", "/campus", "/social", "/apply")):
            return False
        seen.add(full)
        out.append(full)
        return len(out) >= limit

    for job in extract_embedded_jobs(html):
        jid = str(job.get("id") or "").strip()
        if jid and _add(detail_url_for_job(base_url, jid)):
            return out

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

    for m in DETAIL_HREF_RE.finditer(html or ""):
        frag = m.group(0)
        if frag.startswith("jobadid="):
            continue
        full = urljoin(base_url, frag)
        if _add(full):
            return out
    return out


def _location_from_obj(obj: dict) -> str | None:
    for key in ("locations", "city", "cities", "workLocation", "work_location", "location"):
        v = obj.get(key)
        if isinstance(v, list):
            names = []
            for x in v:
                if isinstance(x, dict):
                    names.append(
                        str(
                            x.get("name")
                            or x.get("cityName")
                            or x.get("city")
                            or x.get("address")
                            or x.get("provinceName")
                            or x.get("country")
                            or ""
                        ).strip()
                    )
                else:
                    names.append(str(x).strip())
            names = [n for n in names if n]
            if names:
                return " / ".join(dict.fromkeys(names))
        if isinstance(v, dict):
            name = str(
                v.get("name")
                or v.get("cityName")
                or v.get("city")
                or v.get("address")
                or v.get("provinceName")
                or v.get("country")
                or ""
            ).strip()
            if name:
                return name
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _project_from_obj(obj: dict, title: str | None, jd: str | None) -> tuple[str | None, str | None]:
    for key in ("commitment", "jobType", "recruitType", "job_type"):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            project, bucket, _ = classify_from_text(v, f"{title or ''} {jd or ''}")
            return project or v.strip(), bucket
    # hireMode: 1 社招 / 2 校招（Moka 常见）
    hm = obj.get("hireMode")
    try:
        hm_i = int(hm)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        hm_i = None
    if hm_i == 2:
        project, bucket, _ = classify_from_text(title, jd)
        if not project:
            if title and any(k in title for k in ("实习", "intern")):
                return "实习生招聘", "日常实习"
            return "校园招聘", "校招"
        return project, bucket
    if hm_i == 1:
        return "社会招聘", None
    return classify_from_text(title, jd)[:2]


def _date_ymd(value: Any) -> str | None:
    s = str(value or "").strip()
    if not s:
        return None
    # 2026-07-31T07:18:52.000Z → 2026-07-31
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else None


def job_dict_to_result(
    obj: dict[str, Any],
    *,
    list_url: str,
    company: str | None = None,
    source: str = "moka_init_data",
) -> ParseResult | None:
    title = sanitize_job_title(str(obj.get("title") or obj.get("name") or "").strip())
    if not title or is_noise_title(title):
        return None
    job_id = str(obj.get("id") or "").strip()
    jd = obj.get("description") or obj.get("jobDescription") or obj.get("detail")
    if isinstance(jd, str):
        jd_s = jd.strip() or None
    else:
        jd_s = None
    location = _location_from_obj(obj)
    project, bucket = _project_from_obj(obj, title, jd_s)
    _, _, tags = classify_from_text(title, jd_s)
    tags = tags or extract_job_tags(title)
    published = _date_ymd(
        obj.get("publishedAt") or obj.get("openedAt") or obj.get("updatedAt") or obj.get("createdAt")
    )
    open_at = _date_ymd(obj.get("openedAt") or obj.get("publishedAt"))
    apply = detail_url_for_job(list_url, job_id) if job_id else list_url
    dept = obj.get("department")
    dept_name = None
    if isinstance(dept, dict):
        dept_name = str(dept.get("name") or "").strip() or None
    elif isinstance(dept, str):
        dept_name = dept.strip() or None
    zhineng = obj.get("zhineng")
    category = None
    if isinstance(zhineng, dict):
        category = str(zhineng.get("name") or "").strip() or None
    from_api = source in ("moka_list_api", "moka_job_api")
    extras: dict[str, Any] = {
        "adapter": "moka",
        "source": source,
        "list_url": list_url,
        "list_page_url": list_url,
        "from_html": not from_api,
        "from_api": from_api,
        "job_id": job_id or None,
    }
    org_id = str(obj.get("orgId") or "").strip() or None
    if org_id:
        extras["org_id"] = org_id
    site_raw = obj.get("siteId")
    if site_raw is not None and str(site_raw).strip():
        extras["site_id"] = site_raw
    if company:
        extras["company"] = company
    if published:
        extras["published_at"] = published
        extras["list_updated_at"] = published
    if open_at:
        extras["open_at"] = open_at
    elif published:
        extras["open_at"] = published
    if dept_name:
        extras["department"] = dept_name
    if obj.get("mjCode"):
        extras["mj_code"] = str(obj.get("mjCode"))
    # 列表行无 JD：标记可再抓详情（当前源码无详情正文时保持 HTML 已有字段）
    if not jd_s:
        extras["needs_fetch"] = True

    conf = 0.6
    if location:
        conf += 0.05
    if published:
        conf += 0.05
    if jd_s and len(jd_s) > 40:
        conf += 0.15
    return ParseResult(
        title=title,
        jd_text=jd_s,
        recruit_project=project,
        recruit_bucket=bucket,
        work_location=location,
        raw_category=category,
        job_tags=tags,
        parse_status="ok",
        confidence=min(conf, 0.95),
        apply_url=apply,
        extras=extras,
    )


def parse_moka_detail(url: str, html: str) -> ParseResult:
    """详情页：岗位名称 / JD / 类型 / base 地。优先 init-data 嵌入岗位。"""
    company = company_from_init_data(html)
    embedded = extract_embedded_jobs(html)
    job_id = None
    m = _JOB_ID_IN_URL_RE.search(url or "")
    if m:
        job_id = m.group(1)

    if embedded:
        chosen = None
        if job_id:
            for j in embedded:
                if str(j.get("id") or "") == job_id:
                    chosen = j
                    break
        if chosen is None and len(embedded) == 1:
            chosen = embedded[0]
        if chosen is not None:
            pr = job_dict_to_result(chosen, list_url=url, company=company)
            if pr:
                pr.extras = dict(pr.extras or {})
                pr.extras["source"] = "moka_detail_init_data"
                pr.extras.pop("needs_fetch", None)
                return pr
        # 多岗且 URL 未指定：不当成单岗详情
        if len(embedded) > 1 and not job_id:
            return ParseResult(
                title=None,
                parse_status="needs_browser",
                confidence=0.15,
                apply_url=url,
                extras={
                    "adapter": "moka",
                    "source": "moka_list",
                    "is_list": True,
                    "detail_links": len(embedded),
                    "company": company,
                },
            )

    soup = soup_from_html(html)
    title = extract_og_title(soup)
    jd = None
    location = None
    project = None
    bucket = None

    for blob in find_embedded_json(html):
        t, d = dig_title_jd(blob)
        title = title or t
        jd = jd or d
        if isinstance(blob, dict):
            location = location or _location_from_obj(blob)
            p, b = _project_from_obj(blob, title, jd)
            project = project or p
            bucket = bucket or b

    for m in re.finditer(r"(\{[^{}]{0,200}\"title\"\s*:\s*\"[^\"]+\"[^{}]{0,1200}\})", html or ""):
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        title = title or obj.get("title") or obj.get("name")
        jd = jd or obj.get("description") or obj.get("jobDescription") or obj.get("detail")
        location = location or _location_from_obj(obj)
        p, b = _project_from_obj(
            obj, title if isinstance(title, str) else None, jd if isinstance(jd, str) else None
        )
        project = project or p
        bucket = bucket or b

    if not jd:
        jd = extract_longest_text_block(soup)
    if is_noise_title(title):
        title = recover_title_from_jd(jd if isinstance(jd, str) else None)
    if is_noise_title(title):
        title = None
    if isinstance(jd, str) and is_noise_title(jd):
        jd = None
    location = location or recover_location_from_jd(jd if isinstance(jd, str) else None)
    if not title and not jd:
        # 多岗列表已在上方处理；此处再试 generic
        return parse_generic(url, html)

    title_s = sanitize_job_title(str(title).strip()) if title else None
    if title_s and is_noise_title(title_s):
        title_s = None
    jd_s = jd.strip() if isinstance(jd, str) else jd
    if not project:
        project, bucket, tags = classify_from_text(title_s, jd_s)
    else:
        _, _, tags = classify_from_text(title_s, jd_s)
        bucket = bucket or classify_from_text(project, title_s)[1]
    tags = tags or extract_job_tags(title_s)
    conf = 0.55
    if title_s:
        conf += 0.15
    if jd_s and len(jd_s) > 40:
        conf += 0.2
    if location:
        conf += 0.05
    extras: dict[str, Any] = {"adapter": "moka", "source": "moka_detail"}
    if company:
        extras["company"] = company
    return ParseResult(
        title=title_s,
        jd_text=jd_s,
        recruit_project=project,
        recruit_bucket=bucket,
        work_location=str(location).strip() if location else None,
        job_tags=tags,
        parse_status="ok" if title_s else "needs_browser",
        confidence=min(conf, 0.95),
        apply_url=url,
        extras=extras,
    )


def _mark_outside_lookback(posts: list[ParseResult], *, list_collect_months: int | None) -> None:
    from app.collector.filters import list_date_cutoff, parse_date_loose, resolve_list_lookback_months

    cutoff = list_date_cutoff(months=resolve_list_lookback_months(list_collect_months))
    for pr in posts:
        when = (pr.extras or {}).get("list_updated_at") or (pr.extras or {}).get("published_at")
        d = parse_date_loose(str(when or ""))
        if d and d < cutoff:
            ex = dict(pr.extras or {})
            ex["outside_lookback"] = True
            pr.extras = ex


def _merge_job_dicts(
    html_jobs: list[dict[str, Any]],
    api_jobs: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], str]]:
    """合并 HTML / API 岗位；同 id 以 API 为准（可带 JD），保留 HTML 独有字段补全。"""
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    source_of: dict[str, str] = {}

    def _put(obj: dict[str, Any], source: str) -> None:
        jid = str(obj.get("id") or "").strip()
        if not jid:
            # 无 id 用标题兜底键
            jid = f"_t:{obj.get('title') or len(order)}"
        if jid not in by_id:
            order.append(jid)
            by_id[jid] = dict(obj)
            source_of[jid] = source
            return
        base = by_id[jid]
        if source == "moka_list_api":
            merged = dict(base)
            merged.update(obj)
            # API 常缺 locations：保留 HTML 的
            if not obj.get("locations") and base.get("locations"):
                merged["locations"] = base["locations"]
            if not obj.get("location") and base.get("location"):
                merged["location"] = base["location"]
            by_id[jid] = merged
            source_of[jid] = "moka_list_api"
        else:
            # HTML 补全 API 缺字段
            for k, v in obj.items():
                if v and not by_id[jid].get(k):
                    by_id[jid][k] = v

    for obj in html_jobs:
        _put(obj, "moka_init_data")
    for obj in api_jobs:
        _put(obj, "moka_list_api")
    return [(by_id[jid], source_of[jid]) for jid in order]


def enumerate_positions(
    list_url: str,
    html: str,
    *,
    limit: int = 40,
    max_pages: int = 20,
    page_size: int = 50,
    timeout: float = 30.0,
    fetch_api: bool = True,
    list_collect_months: int | None = None,
) -> list[ParseResult]:
    """
    列表页枚举：HTML #init-data 优先，若 jobStats.total 大于嵌入条数则用
    jobs/v2 API 分页补全（与浏览器「126 结果」一致）；窗外岗位标 outside_lookback。
    """
    from app.collector.adapters.generic import enumerate_job_table_rows

    init = load_init_data(html)
    company = company_from_init_data(html)
    html_jobs = extract_embedded_jobs(html)
    total_hint = job_stats_total(data=init)
    aes_iv = str((init or {}).get("aesIv") or "").strip()
    org = (init or {}).get("org") if isinstance(init, dict) else None
    org_id = None
    site_id: int | str | None = None
    if isinstance(org, dict):
        org_id = str(org.get("id") or "").strip() or None
        site_id = org.get("siteId") or (init or {}).get("siteId")
    if not org_id or site_id is None:
        url_org, url_site, _ = parse_site_from_url(list_url)
        org_id = org_id or url_org
        site_id = site_id if site_id is not None else url_site
    if site_id is not None and not isinstance(site_id, int):
        try:
            site_id = int(site_id)
        except (TypeError, ValueError):
            pass

    # init-data 常只嵌前 ~15 条（ZTE jobStats.total=126）；不足 limit 时用 API 补
    need_api = bool(
        fetch_api
        and org_id
        and site_id is not None
        and aes_iv
        and len(html_jobs) < limit
        and (total_hint is None or total_hint > len(html_jobs))
    )
    api_jobs: list[dict[str, Any]] = []
    if need_api:
        api_jobs = fetch_all_jobs_via_api(
            org_id,
            site_id,
            aes_iv=aes_iv,
            list_url=list_url,
            limit=limit,
            max_pages=max_pages,
            page_size=page_size,
            timeout=timeout,
            total_hint=total_hint,
        )

    merged = _merge_job_dicts(html_jobs, api_jobs) if (html_jobs or api_jobs) else []
    posts: list[ParseResult] = []
    for obj, source in merged:
        if org_id and not obj.get("orgId"):
            obj = {**obj, "orgId": org_id}
        if site_id is not None and obj.get("siteId") is None:
            obj = {**obj, "siteId": site_id}
        pr = job_dict_to_result(obj, list_url=list_url, company=company, source=source)
        if pr:
            pr.extras = dict(pr.extras or {})
            if org_id:
                pr.extras["org_id"] = org_id
            if site_id is not None:
                pr.extras["site_id"] = site_id
            if aes_iv:
                pr.extras["aes_iv"] = aes_iv
            posts.append(pr)
        if len(posts) >= limit:
            break
    if posts:
        _mark_outside_lookback(posts, list_collect_months=list_collect_months)
        return posts

    table_posts = enumerate_job_table_rows(list_url, html, limit=limit)
    if table_posts:
        for p in table_posts:
            p.extras["adapter"] = "moka"
            if p.apply_url and "__job=" not in (p.apply_url or ""):
                p.extras["needs_fetch"] = True
            if company and not (p.extras or {}).get("company"):
                p.extras["company"] = company
        return table_posts
    links = extract_detail_links(list_url, html, limit=limit)
    return [
        ParseResult(
            title=None,
            apply_url=link,
            parse_status="needs_browser",
            confidence=0.2,
            extras={"source": "moka_html_link", "needs_fetch": True, "adapter": "moka"},
        )
        for link in links
    ]


def parse_moka(url: str, html: str) -> ParseResult:
    low = (url or "").lower()
    if "jobadid=" in low or re.search(r"/jobs?/[^/]+", low) or "/position/" in low or "#/job/" in low:
        return parse_moka_detail(url, html)
    # 纯 parse 不打 API；完整列表由 enumerate_positions / fill_from_url 分页补全
    posts = enumerate_positions(url, html, limit=40, fetch_api=False)
    titled = [p for p in posts if p.title and not is_noise_title(p.title)]
    total_hint = job_stats_total(html=html)
    if titled:
        # 列表壳：不把公司门户当岗位；具体岗由 enumerate / resolve 使用
        return ParseResult(
            title=None,
            parse_status="needs_browser",
            confidence=0.15,
            apply_url=url,
            extras={
                "source": "moka_list",
                "is_list": True,
                "detail_links": int(total_hint or len(titled)),
                "company": company_from_init_data(html),
            },
        )
    if posts:
        return ParseResult(
            title=None,
            parse_status="needs_browser",
            confidence=0.15,
            apply_url=url,
            extras={"source": "moka_list", "is_list": True, "detail_links": len(posts)},
        )
    return parse_moka_detail(url, html)
