"""hotjob / wecruit（*.hotjob.cn）适配器：SPA 列表走 positionInfo API。"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse

import httpx

from app.collector.adapters.base import ParseResult
from app.collector.adapters.generic import parse_generic
from app.collector.extract import classify_from_text
from app.collector.filters import extract_job_tags, is_noise_title, sanitize_job_title

# /SUxxxx/pb/school.html | interns.html | social.html
_TENANT_RE = re.compile(r"/(SU[0-9a-fA-F]{8,})(?:/|$)", re.I)
_SPA_SHELL_RE = re.compile(
    r"""(?:id=["']root["']|id=["']ietips["']|You need to enable JavaScript|/pb/js/(?:school|interns|social)\.js)""",
    re.I,
)

# school/campus → 1, social/society → 2, interns → 12（见 pb/js/*.js）
_CHANNEL_RECRUIT_TYPE = {
    "campus": 1,
    "intern": 12,
    "social": 2,
}
# 浏览器 posDetail SPA 读 query.postType，再映射为 recruitType；缺省会导致详情空白
_CHANNEL_POST_TYPE = {
    "campus": "school",
    "intern": "intern",
    "social": "social",
}
_POST_TYPE_CHANNEL = {v: k for k, v in _CHANNEL_POST_TYPE.items()}
_RECRUIT_TYPE_META = {
    1: ("campus", "校园招聘", "校招"),
    12: ("intern", "实习生招聘", "日常实习"),
    2: ("social", "社会招聘", None),
}

# 浏览器 SPA 同源走 wecruit；www 偶发返回「系统维护」短文本，作次选回退。
API_HOST = "https://wecruit.hotjob.cn"
API_HOSTS = (
    "https://wecruit.hotjob.cn",
    "https://www.hotjob.cn",
)
# HTML GET：wecruit 偶被阿里云 WAF 拦成 405；www 同路径 SPA 壳可作回退。
PAGE_HOSTS = (
    "wecruit.hotjob.cn",
    "www.hotjob.cn",
)
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
}
_MAINTENANCE_HINTS = ("系统正在维护", "请稍候访问", "系统维护中")


def is_api_maintenance_text(text: str | None) -> bool:
    """仅识别短维护 boilerplate，避免整页 HTML 里偶发「维护」误判。"""
    t = (text or "").strip()
    if not t or len(t) > 500:
        return False
    if "<html" in t.lower() and len(t) > 200:
        return False
    return any(h in t for h in _MAINTENANCE_HINTS)


def can_handle(url: str) -> bool:
    u = (url or "").lower()
    return "hotjob.cn" in u or "wecruit.hotjob.cn" in u


def extract_tenant(url: str | None) -> str | None:
    m = _TENANT_RE.search(url or "")
    return m.group(1) if m else None


def is_waf_block_page(html: str | None, *, status_code: int | None = None) -> bool:
    """阿里云/Tengine 安全拦截页（常见 HTTP 405 + errors.aliyun.com）。"""
    if status_code in (405, 403) and not (html or "").strip():
        return True
    t = html or ""
    if not t:
        return False
    low = t.lower()
    if "errors.aliyun.com" in low or 'id="renderdata"' in low:
        if status_code in (405, 403) or "<title>405</title>" in low or "been blocked" in low:
            return True
    if status_code in (405, 403) and ("潜在安全威胁" in t or "potential threats" in low):
        return True
    return False


def page_url_candidates(url: str) -> list[str]:
    """同一 pb 路径在 wecruit / www 之间的 HTML 抓取候选（去重保序）。"""
    raw = (url or "").strip()
    if not raw or not can_handle(raw):
        return [raw] if raw else []
    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    out: list[str] = []
    seen: set[str] = set()

    def _add(u: str) -> None:
        if u and u not in seen:
            seen.add(u)
            out.append(u)

    _add(raw)
    if host in PAGE_HOSTS or host.endswith(".hotjob.cn"):
        for alt in PAGE_HOSTS:
            if alt == host:
                continue
            _add(parsed._replace(netloc=alt).geturl())
    return out


def is_spa_list_shell(html: str | None) -> bool:
    """静态 HTML 仅 SPA 壳（#root + IE 提示 / js bundle），无岗位卡片。"""
    if not html:
        return False
    if not _SPA_SHELL_RE.search(html):
        return False
    # 已有服务端表格/卡片时不算壳
    low = html.lower()
    if "<table" in low and ("职位名称" in html or "岗位名称" in html):
        return False
    if "position-item" in low or "job-card" in low:
        if "{{item" not in html and "postName" in html:
            return False
    return True


def post_type_for_channel(channel: str | None) -> str | None:
    return _CHANNEL_POST_TYPE.get(channel or "")


def channel_from_post_type(post_type: str | None) -> str | None:
    raw = (post_type or "").strip().lower()
    if not raw:
        return None
    if raw in _POST_TYPE_CHANNEL:
        return _POST_TYPE_CHANNEL[raw]
    if raw in ("interns", "internship"):
        return "intern"
    if raw in ("campus", "school"):
        return "campus"
    if raw in ("society", "social"):
        return "social"
    return None


def channel_hint_from_fields(fields: dict[str, Any] | None) -> str | None:
    """从岗位字段推断频道（补全历史 URL 缺失的 postType）。"""
    src = fields or {}
    extras = src.get("extras") if isinstance(src.get("extras"), dict) else {}
    for key in ("portal_channel", "channel"):
        ch = (extras.get(key) or src.get(key) or "").strip().lower()
        if ch in _CHANNEL_POST_TYPE:
            return ch
    blob = " ".join(
        str(src.get(k) or "")
        for k in ("recruit_bucket", "recruit_project", "raw_category", "title")
    )
    if any(x in blob for x in ("实习", "intern")):
        return "intern"
    if any(x in blob for x in ("社会招聘", "社招")):
        return "social"
    if any(x in blob for x in ("校招", "校园招聘", "校园")):
        return "campus"
    return None


def detail_url(
    tenant: str,
    post_id: str,
    *,
    host: str = "wecruit.hotjob.cn",
    channel: str | None = None,
) -> str:
    tid = (tenant or "").strip()
    pid = (post_id or "").strip()
    base = f"https://{host}/{tid}/pb/posDetail.html?postId={pid}"
    pt = post_type_for_channel(channel)
    if pt:
        return f"{base}&postType={pt}"
    return base


def ensure_posdetail_post_type(
    url: str | None,
    *,
    channel: str | None = None,
    fields: dict[str, Any] | None = None,
) -> str:
    """
    补全 posDetail.html 缺失的 postType=。
    浏览器 SPA 用 postType→recruitType 拉详情；缺参时接口收到 undefined，页面空白。
    """
    raw = (url or "").strip()
    if not raw or not can_handle(raw):
        return raw
    if not re.search(r"/pb/(?:posDetail|position)\.html", raw, re.I):
        return raw
    parsed = urlparse(raw)
    q = parse_qs(parsed.query or "", keep_blank_values=True)
    if (q.get("postType") or [""])[0].strip():
        return raw
    ch = channel or channel_from_post_type((q.get("postType") or [None])[0])
    if not ch:
        from app.collector.portal_nav import channel_from_url

        ch = channel_from_url(raw) or channel_hint_from_fields(fields)
    pt = post_type_for_channel(ch)
    if not pt:
        return raw
    pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query or "", keep_blank_values=True)
        if k.lower() != "posttype"
    ]
    pairs.append(("postType", pt))
    return parsed._replace(query=urlencode(pairs)).geturl()


def list_page_url(tenant: str, channel: str, *, host: str = "wecruit.hotjob.cn") -> str:
    page = {"campus": "school", "intern": "interns", "social": "social"}.get(channel, "school")
    return f"https://{host}/{tenant}/pb/{page}.html"


def recruit_type_for_channel(channel: str | None) -> int:
    return _CHANNEL_RECRUIT_TYPE.get(channel or "campus", 1)


def channel_from_recruit_type(recruit_type: int | str | None) -> str | None:
    try:
        rt = int(recruit_type)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    meta = _RECRUIT_TYPE_META.get(rt)
    return meta[0] if meta else None


def fetch_position_detail(
    tenant: str,
    post_id: str,
    *,
    recruit_type: int | None = None,
    list_url: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """
    POST /wecruit/positionInfo/listPositionDetail/{tenant}。
    浏览器会带 recruitType；仅 postId 时接口通常也可返回详情。
    """
    tid = (tenant or "").strip()
    pid = (post_id or "").strip()
    if not tid or not pid:
        return {}
    referer = list_url or detail_url(tid, pid)
    headers = {
        **DEFAULT_HEADERS,
        "Origin": "https://wecruit.hotjob.cn",
        "Referer": referer,
    }
    body: dict[str, str] = {"postId": pid, "isFrompb": "true"}
    if recruit_type is not None:
        body["recruitType"] = str(int(recruit_type))
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=timeout) as client:
            for host in API_HOSTS:
                url = f"{host}/wecruit/positionInfo/listPositionDetail/{tid}"
                try:
                    resp = client.post(url, data=body)
                except Exception:  # noqa: BLE001
                    continue
                if resp.status_code >= 400:
                    continue
                text = resp.text or ""
                ctype = (resp.headers.get("content-type") or "").lower()
                if "json" not in ctype and not text.lstrip().startswith("{"):
                    continue
                try:
                    data = resp.json()
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(data, dict) and str(data.get("state") or "") in ("200", "0"):
                    payload = data.get("data")
                    if isinstance(payload, dict) and payload.get("postId"):
                        return payload
    except Exception:  # noqa: BLE001
        return {}
    return {}


def fetch_position_list(
    tenant: str,
    recruit_type: int,
    *,
    page: int = 1,
    page_size: int = 50,
    project_code: str | None = None,
    timeout: float = 30.0,
    list_url: str | None = None,
) -> dict[str, Any]:
    """
    POST form 到 /wecruit/positionInfo/listPosition/{tenant}（补充枚举用）。
    优先 wecruit.hotjob.cn（与浏览器同源），www 作回退。
    JSON body 会被拒；必须用 x-www-form-urlencoded。
    """
    tid = (tenant or "").strip()
    if not tid:
        return {}
    referer = list_url or list_page_url(tid, channel_from_recruit_type(recruit_type) or "campus")
    headers = {
        **DEFAULT_HEADERS,
        "Origin": "https://wecruit.hotjob.cn",
        "Referer": referer,
    }
    body = {
        "isFrompb": "true",
        "recruitType": str(int(recruit_type)),
        "pageSize": str(max(1, min(int(page_size), 100))),
        "currentPage": str(max(1, int(page))),
    }
    if project_code:
        body["projectCode"] = project_code
    last_maintenance: dict[str, Any] | None = None
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=timeout) as client:
            for host in API_HOSTS:
                url = f"{host}/wecruit/positionInfo/listPosition/{tid}"
                try:
                    resp = client.post(url, data=body)
                except Exception:  # noqa: BLE001
                    continue
                if resp.status_code >= 400:
                    continue
                ctype = (resp.headers.get("content-type") or "").lower()
                text = resp.text or ""
                if "json" not in ctype and not text.lstrip().startswith("{"):
                    if is_api_maintenance_text(text):
                        last_maintenance = {
                            "state": "503",
                            "message": "系统正在维护中，请稍候访问...",
                            "data": None,
                            "_hotjob_maintenance": True,
                        }
                        continue
                    continue
                try:
                    data = resp.json()
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(data, dict):
                    return data
    except Exception:  # noqa: BLE001
        return last_maintenance or {}
    return last_maintenance or {}


def _page_rows(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int, int]:
    if str(payload.get("state") or "") not in ("200", "0"):
        return [], 0, 0
    data = payload.get("data")
    if not isinstance(data, dict):
        return [], 0, 0
    page_form = data.get("pageForm")
    if not isinstance(page_form, dict):
        return [], 0, 0
    rows = page_form.get("pageData") or []
    if not isinstance(rows, list):
        rows = []
    total_page = int(page_form.get("totalPage") or 0) or 0
    data_count = int(page_form.get("dataCount") or 0) or 0
    return [r for r in rows if isinstance(r, dict)], total_page, data_count


def row_to_result(
    row: dict[str, Any],
    *,
    tenant: str,
    list_url: str,
    channel: str | None = None,
) -> ParseResult | None:
    title = sanitize_job_title(str(row.get("postName") or "").strip())
    if not title or is_noise_title(title):
        return None
    post_id = str(row.get("postId") or "").strip()
    if not post_id:
        return None

    rt = row.get("recruitType")
    ch = channel or channel_from_recruit_type(rt) or "campus"
    meta = _RECRUIT_TYPE_META.get(_CHANNEL_RECRUIT_TYPE.get(ch, 1))
    project = meta[1] if meta else "校园招聘"
    bucket = meta[2] if meta else "校招"
    if ch == "social":
        project, bucket = "社会招聘", None

    location = str(row.get("workPlaceStr") or row.get("workPlace") or "").strip() or None
    category = str(row.get("postTypeName") or "").strip() or None
    updated = str(row.get("publishDate") or row.get("publishFirstDate") or "").strip() or None
    if updated and " " in updated:
        updated = updated.split(" ", 1)[0]
    from app.collector.label_fields import normalize_headcount

    headcount = normalize_headcount(str(row.get("recruitNumStr") or row.get("recruitNum") or ""))
    education = str(row.get("educationStr") or "").strip() or None
    company = str(row.get("company") or "").strip() or None
    deadline = str(row.get("endDate") or "").strip() or None
    if deadline and " " in deadline:
        deadline = deadline.split(" ", 1)[0]
    if deadline and deadline.startswith("2030"):
        # 平台占位长期截止，不当真实 deadline
        deadline = None

    apply = detail_url(tenant, post_id, channel=ch)
    _, _, tags = classify_from_text(title, f"{category or ''} {location or ''}")
    tags = tags or extract_job_tags(title)

    extras: dict[str, Any] = {
        "adapter": "hotjob",
        "source": "hotjob_list_api",
        "needs_fetch": True,
        "list_url": list_url,
        "list_page_url": list_url,
        "from_api": True,
        "portal_channel": ch,
        "post_id": post_id,
    }
    if company:
        extras["company"] = company
    if updated:
        extras["published_at"] = updated
        extras["list_updated_at"] = updated
    if category:
        extras["raw_category"] = category
    if row.get("department"):
        extras["department"] = str(row.get("department"))

    return ParseResult(
        title=title,
        recruit_project=project,
        recruit_bucket=bucket,
        work_location=location,
        raw_category=category,
        headcount=headcount,
        education=education,
        deadline=deadline,
        job_tags=tags,
        parse_status="ok",
        confidence=0.7,
        apply_url=apply,
        extras=extras,
    )


def enumerate_positions(
    list_url: str,
    html: str | None = None,
    *,
    limit: int = 40,
    max_pages: int = 20,
    page_size: int = 50,
    timeout: float = 30.0,
    channel: str | None = None,
    recruit_types: list[int] | None = None,
) -> list[ParseResult]:
    """
    枚举 wecruit 岗位列表（API）。
    默认按 URL 频道拉一页类型；也可传入 recruit_types 显式拉取。
    """
    from app.collector.portal_nav import channel_from_url

    tenant = extract_tenant(list_url)
    if not tenant:
        return []

    ch = channel or channel_from_url(list_url) or "campus"
    # 详情页作入口时，Referer / list_url 仍用频道列表页
    if re.search(r"/pb/(?:posDetail|position)\.html", list_url or "", re.I):
        effective_list = list_page_url(tenant, ch)
    else:
        effective_list = list_url or list_page_url(tenant, ch)
    types = recruit_types or [recruit_type_for_channel(ch)]
    q = parse_qs(urlparse(list_url or "").query or "")
    project_code = (q.get("projectCode") or [None])[0]

    from app.collector.filters import list_date_cutoff, parse_date_loose, resolve_list_lookback_months

    cutoff = list_date_cutoff(months=resolve_list_lookback_months(None))
    out: list[ParseResult] = []
    seen: set[str] = set()
    for rt in types:
        rt_ch = channel_from_recruit_type(rt) or ch
        referer = list_page_url(tenant, rt_ch)
        for page in range(1, max_pages + 1):
            payload = fetch_position_list(
                tenant,
                rt,
                page=page,
                page_size=page_size,
                project_code=project_code,
                timeout=timeout,
                list_url=referer,
            )
            if payload.get("_hotjob_maintenance"):
                break
            rows, total_page, _ = _page_rows(payload)
            if not rows:
                break
            for row in rows:
                pr = row_to_result(
                    row, tenant=tenant, list_url=effective_list, channel=rt_ch
                )
                if not pr:
                    continue
                key = f"{pr.title}|{pr.apply_url}"
                if key in seen:
                    continue
                seen.add(key)
                when = (pr.extras or {}).get("list_updated_at") or (pr.extras or {}).get(
                    "published_at"
                )
                d = parse_date_loose(str(when or ""))
                if d and d < cutoff:
                    ex = dict(pr.extras or {})
                    ex["outside_lookback"] = True
                    pr.extras = ex
                out.append(pr)
                if len(out) >= limit:
                    return out
            if total_page and page >= total_page:
                break
            if len(rows) < page_size:
                break
    return out


def _jd_from_detail_row(row: dict[str, Any]) -> str | None:
    parts: list[str] = []
    for key, label in (
        ("duty", "岗位职责"),
        ("workContent", "工作内容"),
        ("serviceCondition", "任职要求"),
        ("remark", "备注"),
    ):
        val = str(row.get(key) or "").strip()
        if not val:
            continue
        if label and label not in val[:20]:
            parts.append(f"{label}\n{val}")
        else:
            parts.append(val)
    text = "\n\n".join(parts).strip()
    return text or None


def parse_posdetail_from_api(url: str, *, timeout: float = 30.0) -> ParseResult | None:
    """posDetail.html?postId=… 走 listPositionDetail，补 SPA 空壳无 JD。"""
    if not re.search(r"/pb/(?:posDetail|position)\.html", url or "", re.I):
        return None
    tenant = extract_tenant(url)
    q = parse_qs(urlparse(url or "").query or "")
    post_id = (q.get("postId") or [None])[0]
    if not tenant or not post_id:
        return None
    from app.collector.portal_nav import channel_from_url

    ch = channel_from_url(url) or channel_from_post_type((q.get("postType") or [None])[0])
    rt = recruit_type_for_channel(ch) if ch else None
    row = fetch_position_detail(
        tenant, post_id, recruit_type=rt, list_url=url, timeout=timeout
    )
    if not row:
        # 无频道时再试不带 recruitType（仅 postId 通常可用）
        if rt is not None:
            row = fetch_position_detail(
                tenant, post_id, recruit_type=None, list_url=url, timeout=timeout
            )
    if not row:
        return None
    # 详情接口字段名与列表略有差异
    norm = dict(row)
    if not norm.get("educationStr") and norm.get("education"):
        norm["educationStr"] = norm.get("education")
    if not norm.get("workPlaceStr") and norm.get("workPlace"):
        norm["workPlaceStr"] = norm.get("workPlace")
    # 用详情行的 recruitType 校正频道与可打开链接
    ch = channel_from_recruit_type(norm.get("recruitType")) or ch or "campus"
    pr = row_to_result(norm, tenant=tenant, list_url=list_page_url(tenant, ch), channel=ch)
    if not pr:
        return None
    jd = _jd_from_detail_row(row)
    if jd:
        pr.jd_text = jd
        ex = dict(pr.extras or {})
        ex["needs_fetch"] = False
        ex["source"] = "hotjob_detail_api"
        pr.extras = ex
        pr.confidence = max(float(pr.confidence or 0), 0.85)
    apply = ensure_posdetail_post_type(url, channel=ch) or pr.apply_url
    pr.apply_url = apply
    return pr


def parse_hotjob(url: str, html: str) -> ParseResult:
    """详情优先 generic；列表 SPA 壳标记为 list_page，避免 IE 提示当岗位。

    posDetail 的 JD 请走 parse_posdetail_from_api / enrich（HTML 仅为 SPA 壳）。
    """
    if is_spa_list_shell(html):
        from app.collector.portal_nav import channel_from_url

        ch = channel_from_url(url)
        project = bucket = None
        if ch == "campus":
            project, bucket = "校园招聘", "校招"
        elif ch == "intern":
            project, bucket = "实习生招聘", "日常实习"
        elif ch == "social":
            project, bucket = "社会招聘", None
        return ParseResult(
            title=None,
            jd_text=None,
            recruit_project=project,
            recruit_bucket=bucket,
            parse_status="needs_browser",
            confidence=0.15,
            apply_url=ensure_posdetail_post_type(url, channel=ch) or url,
            extras={
                "adapter": "hotjob",
                "source": "hotjob_list_shell",
                "is_list": True,
                "list_page": True,
                "portal_channel": ch,
                "tenant": extract_tenant(url),
            },
        )
    result = parse_generic(url, html)
    result.extras = dict(result.extras or {})
    result.extras["adapter"] = "hotjob"
    ch = (result.extras or {}).get("portal_channel")
    if result.apply_url:
        result.apply_url = ensure_posdetail_post_type(result.apply_url, channel=ch)
    return result
