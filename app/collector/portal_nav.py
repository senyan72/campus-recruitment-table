"""门户频道识别（校园/实习/社招）与列表卡片枚举（五矿类门户）。"""

from __future__ import annotations

import re
from datetime import date
from typing import Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from app.collector.adapters.base import ParseResult
from app.collector.extract import classify_from_text, soup_from_html
from app.collector.filters import (
    extract_job_tags,
    is_job_detail_link,
    is_noise_location,
    is_noise_nav_url,
    is_noise_title,
    list_date_cutoff,
    parse_date_loose,
    resolve_list_lookback_months,
    sanitize_job_title,
)

# URL path → 频道
_PATH_CHANNEL = (
    (re.compile(r"/(campus|school|campusrecruit|xyzp|campus-?job|校招)", re.I), "campus"),
    # interns.html / intern / internship …
    (re.compile(r"/(interns?(?:\.html)?|internship|trainee|sxzp|实习)", re.I), "intern"),
    (re.compile(r"/(social|society|shzp|social-?recruit|社招)", re.I), "social"),
)

_CHANNEL_TO_PROJECT = {
    "campus": ("校园招聘", "校招"),
    "intern": ("实习生招聘", "日常实习"),
    "social": ("社会招聘", None),
}

_NAV_TEXTS = {
    "campus": ("校园招聘", "校招专区", "校园招聘专区", "应届生招聘"),
    "intern": (
        "实习生专项",
        "实习生招聘",
        "实习招聘",
        "日常实习",
        "暑期实习",
        "实习生",
    ),
    "social": ("社会招聘", "社会人才", "社招专区", "正式员工招聘"),
}

_ACTIVE_CLASS_RE = re.compile(
    r"(active|current|selected|is-active|on|cur|hover|choose)",
    re.I,
)

_DATE_RE = re.compile(r"(20\d{2}[-/.年]\d{1,2}([-/.月]\d{1,2})?日?)")
_LABELED_DATE_RE = re.compile(
    r"(?:更新日期|更新时间|岗位发布时间|发布时间|发布日期|开招日期)[:：\s]*"
    r"(20\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2})?日?)",
    re.I,
)
# hotjob 等：地点旁「2026-07-31 最新」
_INLINE_LATEST_DATE_RE = re.compile(
    r"(20\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2})?日?)\s*最新(?:发布)?"
)
_LABELED_HEADCOUNT_RE = re.compile(
    r"(?:招聘人数|需求人数|用人名额|招聘名额)[:：\s]*([^\n|｜]{1,20})",
    re.I,
)
_LABELED_LOCATION_RE = re.compile(
    r"(?:工作地点|工作城市|上班地点|地点)[:：\s]*([^\n|｜]{2,40})",
    re.I,
)
_LABELED_CATEGORY_RE = re.compile(
    r"(?:职位类别|岗位类别|职能类别|职位类型|岗位类型)[:：\s]*([^\n|｜]{2,40})",
    re.I,
)
_COMPANY_HINT_RE = re.compile(
    r"(有限责任公司|股份有限公司|有限公司|集团|研究院|设计院|分公司|公司)$"
)


def channel_from_url(url: str | None) -> str | None:
    """从 URL path 判断频道：campus / intern / social。"""
    path = (urlparse(url or "").path or "").lower()
    if not path:
        return None
    for cre, ch in _PATH_CHANNEL:
        if cre.search(path):
            return ch
    q = (urlparse(url or "").query or "").lower()
    if "campus" in q or "school" in q:
        return "campus"
    if "intern" in q:
        return "intern"
    if "social" in q or "society" in q:
        return "social"
    return None


def channel_from_nav_html(html: str | None) -> str | None:
    """
    从顶栏导航高亮识别频道。
    优先 class 含 active/current 的链接文案；否则看 aria-current / 选中态。
    """
    if not html:
        return None
    soup = soup_from_html(html)
    candidates: list[tuple[int, str]] = []  # (score, channel)

    for a in soup.find_all("a"):
        text = (a.get_text(" ", strip=True) or "").strip()
        if not text or len(text) > 20:
            continue
        ch = _match_nav_text(text)
        if not ch:
            continue
        score = 1
        cls = " ".join(a.get("class") or [])
        parent_cls = " ".join((a.parent.get("class") if a.parent else None) or [])
        if _ACTIVE_CLASS_RE.search(cls) or _ACTIVE_CLASS_RE.search(parent_cls):
            score += 5
        if (a.get("aria-current") or "").lower() in ("page", "true"):
            score += 5
        href = a.get("href") or ""
        href_ch = channel_from_url(href) if href else None
        if href_ch == ch:
            score += 1
        candidates.append((score, ch))

    if not candidates:
        return None
    candidates.sort(key=lambda x: -x[0])
    best_score, best_ch = candidates[0]
    # 无高亮时不臆测（并列低分忽略）
    if best_score < 5:
        return None
    return best_ch


def _match_nav_text(text: str) -> str | None:
    t = re.sub(r"\s+", "", text)
    for ch, names in _NAV_TEXTS.items():
        for n in names:
            if t == n or t.startswith(n):
                return ch
    return None


def detect_recruit_channel(url: str | None, html: str | None = None) -> tuple[str | None, str | None, str | None]:
    """
    返回 (channel, recruit_project, recruit_bucket)。
    优先级：导航高亮 > URL path。
    社招 → project=社会招聘, bucket=None（不得为校招）。
    实习 → project=实习生招聘, bucket=日常实习。
    """
    nav_ch = channel_from_nav_html(html)
    url_ch = channel_from_url(url)
    ch = nav_ch or url_ch
    if not ch:
        return None, None, None
    project, bucket = _CHANNEL_TO_PROJECT[ch]
    return ch, project, bucket


def extract_channel_nav_links(base_url: str, html: str | None) -> dict[str, str]:
    """
    从顶栏导航抽取频道入口链接：campus / intern / social → 绝对 URL。
    用于从门户页「找到并进入」实习生招聘 / 校园招聘。
    """
    if not html:
        return {}
    soup = soup_from_html(html)
    out: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        text = (a.get_text(" ", strip=True) or "").strip()
        if not text or len(text) > 20:
            continue
        ch = _match_nav_text(text)
        if not ch:
            continue
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        full = urljoin(base_url, href).split("#")[0]
        if urlparse(full).scheme not in ("http", "https"):
            continue
        # 同频道保留首次；若当前链接 path 更明确可覆盖
        prev = out.get(ch)
        if not prev:
            out[ch] = full
            continue
        href_ch = channel_from_url(full)
        if href_ch == ch and channel_from_url(prev) != ch:
            out[ch] = full
    return out


def derive_channel_list_url(url: str | None) -> tuple[str | None, str | None]:
    """
    从详情/深层 URL 推导同频道列表入口。
    例：/campus/detail/101 → (/campus, campus)；/intern/job/2 → (/intern, intern)。
    hotjob posDetail.html?postType=intern → /pb/interns.html。
    """
    if not url:
        return None, None
    parsed = urlparse(url)
    path = parsed.path or ""
    if not path:
        return None, None
    # wecruit：详情页上卷到 school/interns/social 列表
    pb = hotjob_pb_channel_urls(url)
    if pb and re.search(r"/pb/(?:posDetail|position)\.html", path, re.I):
        ch = channel_from_url(url) or "campus"
        list_u = pb.get(ch) or pb.get("campus")
        if not list_u:
            return None, None
        return list_u, ch
    # 显式频道段
    for seg, ch in (
        ("campusrecruit", "campus"),
        ("campus-job", "campus"),
        ("campus", "campus"),
        ("school", "campus"),
        ("xyzp", "campus"),
        ("internship", "intern"),
        ("intern", "intern"),
        ("trainee", "intern"),
        ("sxzp", "intern"),
        ("social-recruit", "social"),
        ("social", "social"),
        ("society", "social"),
        ("shzp", "social"),
    ):
        m = re.search(rf"(?P<prefix>.*/{seg})(?:/|$)", path, re.I)
        if not m:
            continue
        list_path = m.group("prefix")
        # 已是列表根则不必再推
        rest = path[len(list_path) :].strip("/")
        if not rest:
            return None, ch
        list_url = urlunparse(parsed._replace(path=list_path, query="", fragment=""))
        return list_url, ch
    # /xxx/detail/123 或 /xxx/details/123 → /xxx
    m = re.search(r"(?P<prefix>.+)/(?:details?|position|job|post)/\d+", path, re.I)
    if m:
        list_path = m.group("prefix")
        list_url = urlunparse(parsed._replace(path=list_path, query="", fragment=""))
        return list_url, channel_from_url(list_url)
    return None, None


def hotjob_pb_channel_urls(base_url: str | None) -> dict[str, str]:
    """
    wecruit SPA：/{SUxxx}/pb/* 无顶栏 HTML 时合成兄弟频道。
    匹配 school/interns/social 列表，也匹配 posDetail.html 等详情页。
    campus 对应 school.html（campus.html 常 404）。
    """
    m = re.search(
        r"(?P<root>https?://[^/]+/(?P<tenant>SU[0-9a-fA-F]+)/pb/)"
        r"(?:school|campus|interns?|social|posDetail|position)\.html",
        base_url or "",
        re.I,
    )
    if not m:
        # 兜底：任意 /SUxxx/pb/ 路径（含无后缀）
        m = re.search(
            r"(?P<root>https?://[^/]+/(?P<tenant>SU[0-9a-fA-F]+)/pb/)",
            base_url or "",
            re.I,
        )
    if not m:
        return {}
    root = m.group("root")
    return {
        "campus": f"{root}school.html",
        "intern": f"{root}interns.html",
        "social": f"{root}social.html",
    }


def expand_career_channel_urls(
    base_url: str,
    html: str | None,
    *,
    include: tuple[str, ...] = ("campus", "intern"),
) -> list[str]:
    """
    展开采集入口：当前页 + 导航中的校园/实习频道（默认不含社招）。
    详情 URL 会额外上卷到同频道列表根，保证从详情仍能枚举卡片。
    """
    links = extract_channel_nav_links(base_url, html)
    # hotjob SPA 壳无 <a> 导航：按 path 合成校招/实习入口
    for ch, u in hotjob_pb_channel_urls(base_url).items():
        links.setdefault(ch, u)
    out: list[str] = []
    seen: set[str] = set()

    def _add(u: str | None) -> None:
        if not u:
            return
        key = u.split("?")[0].rstrip("/").lower()
        if key in seen:
            return
        seen.add(key)
        out.append(u)

    # 当前页若属 include 频道或未知（门户首页），先保留
    cur_ch = channel_from_url(base_url) or channel_from_nav_html(html)
    if cur_ch is None or cur_ch in include:
        _add(base_url)
    # 详情 → 列表根（无导航时仍可从 path 上卷）
    parent_list, parent_ch = derive_channel_list_url(base_url)
    if parent_list and (parent_ch is None or parent_ch in include):
        _add(parent_list)
        if parent_ch and parent_ch not in links:
            links = dict(links)
            links[parent_ch] = parent_list
    for ch in include:
        _add(links.get(ch))
    return out or [base_url]


def apply_channel_to_result(result: ParseResult, url: str | None, html: str | None) -> ParseResult:
    """把门户频道写入 ParseResult（不覆盖更强的详情标签社招/实习）。"""
    ch, project, bucket = detect_recruit_channel(url, html)
    if not ch or not project:
        return result
    extras = result.extras if isinstance(result.extras, dict) else {}
    extras["portal_channel"] = ch
    result.extras = extras

    rp = (result.recruit_project or "").strip()
    # 详情已明确社招/实习时不降级覆盖
    if any(k in rp for k in ("社会招聘", "社招")) and ch != "social":
        return result
    if any(k in rp for k in ("日常实习", "暑期实习", "实习生招聘", "实习生")) and ch != "intern":
        return result

    if ch == "social":
        result.recruit_project = "社会招聘"
        if result.recruit_bucket == "校招":
            result.recruit_bucket = None
        return result

    if not rp or rp in ("校园招聘", "全职", "日常实习") or (ch == "campus" and "社招" not in rp):
        result.recruit_project = project
    if bucket and (not result.recruit_bucket or (result.recruit_bucket == "校招" and ch == "intern")):
        result.recruit_bucket = bucket
    elif ch == "campus" and not result.recruit_bucket:
        result.recruit_bucket = "校招"
    return result


def extract_detail_heading_title(html: str | None) -> str | None:
    """详情页加粗大标题：优先 h1 / .job-title，其次 strong/b（拒绝站点 chrome）。"""
    if not html:
        return None
    soup = soup_from_html(html)
    for sel in (
        "article h1",
        ".job-detail h1",
        ".detail h1",
        "main h1",
        "h1",
        "h2.job-title",
        ".job-title",
        ".position-title",
        ".post-title",
    ):
        node = soup.select_one(sel)
        if not node:
            continue
        text = sanitize_job_title(node.get_text(" ", strip=True))
        if text and 2 <= len(text) <= 80:
            return text
    # 加粗标题回落（五矿类详情常见）
    for tag in ("strong", "b"):
        for node in soup.find_all(tag):
            text = sanitize_job_title(node.get_text(" ", strip=True))
            if not text:
                continue
            if 2 <= len(text) <= 40 and not re.search(r"[:：]", text):
                return text
    return None


_DETAIL_HREF_RE = re.compile(r"/details?/\d+|job[_-]?detail|positionid|/position/[^/]+/detail", re.I)


def enumerate_detail_link_jobs(
    base_url: str,
    html: str,
    *,
    limit: int = 40,
    channel_project: str | None = None,
    channel_bucket: str | None = None,
) -> list[ParseResult]:
    """
    Apple / 大厂搜索结果页：h2/h3/a 指向 /details/{id} 的真实岗位，一链一行。
    避免把整页 Search Jobs chrome 压成一条。
    """
    soup = soup_from_html(html)
    out: list[ParseResult] = []
    seen: set[str] = set()

    candidates: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        full = urljoin(base_url, href).split("#")[0]
        if not _DETAIL_HREF_RE.search(full) and not is_job_detail_link(full, a.get_text(" ", strip=True)):
            continue
        title = ""
        # 优先标题节点
        parent = a.find_parent(["h1", "h2", "h3", "h4"])
        if parent:
            title = parent.get_text(" ", strip=True)
        if not title:
            title = a.get_text(" ", strip=True)
        if not title:
            for sib in (a.find_previous(["h2", "h3"]), a.find_parent("li")):
                if sib is None:
                    continue
                t = sib.get_text(" ", strip=True) if sib.name in ("h2", "h3") else ""
                if not t and sib.name == "li":
                    h = sib.find(["h2", "h3", "h4"])
                    t = h.get_text(" ", strip=True) if h else ""
                if t:
                    title = t
                    break
        title = sanitize_job_title(title) or ""
        if not title or is_noise_title(title) or is_noise_nav_url(full, title):
            continue
        # 过长多半是整段卡片，截首行
        if len(title) > 80:
            title = title.split("\n")[0].strip()[:80]
            title = sanitize_job_title(title) or ""
        if not title:
            continue
        # 频道/导航壳（校园招聘、实习生招聘）不是具体岗位
        compact = re.sub(r"\s+", "", title)
        if compact in {
            "校园招聘",
            "社会招聘",
            "实习生招聘",
            "实习招聘",
            "校招",
            "社招",
            "招聘",
            "首页",
        }:
            continue
        candidates.append((title, full))

    for title, apply_url in candidates:
        key = f"{title}|{apply_url}"
        if key in seen:
            continue
        seen.add(key)
        project, bucket, tags = classify_from_text(title, "")
        if channel_project:
            project = channel_project
            bucket = channel_bucket
        if channel_project == "社会招聘":
            project = "社会招聘"
            bucket = None
        out.append(
            ParseResult(
                title=title,
                recruit_project=project,
                recruit_bucket=bucket,
                job_tags=tags or extract_job_tags(title),
                parse_status="ok",
                confidence=0.55,
                apply_url=apply_url,
                extras={
                    "adapter": "generic",
                    "source": "html_detail_link",
                    "needs_fetch": True,
                    "list_url": base_url,
                    "from_search_list": True,
                },
            )
        )
        if len(out) >= limit:
            break
    return out


def _card_labeled_meta(card) -> dict[str, str]:
    """从卡片可见文案抽 更新日期 / 招聘人数 / 地点 / 职位类别（联储证券式标签）。"""
    blob = card.get_text("\n", strip=True) if card is not None else ""
    out: dict[str, str] = {}
    if not blob:
        return out
    m = _LABELED_DATE_RE.search(blob)
    if m:
        out["updated"] = m.group(1).strip()
    else:
        m = _INLINE_LATEST_DATE_RE.search(blob)
        if m:
            out["updated"] = m.group(1).strip()
    m = _LABELED_HEADCOUNT_RE.search(blob)
    if m:
        from app.collector.label_fields import normalize_headcount

        hc = normalize_headcount(m.group(1))
        if hc:
            out["headcount"] = hc
    m = _LABELED_LOCATION_RE.search(blob)
    if m:
        loc = m.group(1).strip()
        if loc and not is_noise_location(loc):
            out["location"] = loc
    m = _LABELED_CATEGORY_RE.search(blob)
    if m:
        cat = m.group(1).strip()
        if cat and len(cat) <= 40:
            out["category"] = cat
    return out


def enumerate_job_cards(
    base_url: str,
    html: str,
    *,
    limit: int = 40,
    channel_project: str | None = None,
    channel_bucket: str | None = None,
) -> list[ParseResult]:
    """
    列表卡片枚举：岗位名 / 公司 / 地点 / 更新日期 / 招聘人数。
    适配五矿类与联储证券式标签卡片（非 table）。
    """
    soup = soup_from_html(html)
    out: list[ParseResult] = []
    seen: set[str] = set()

    # 常见卡片容器（含联储/hotjob 式 position / recruit 网格）
    card_sels = (
        ".job-card",
        ".job-item",
        ".position-card",
        ".position-item",
        ".post-item",
        ".list-item",
        "li.job",
        "div[class*='job-list'] > div",
        "div[class*='position-list'] > div",
        "div[class*='jobList'] > div",
        "ul.job-list > li",
        "ul[class*='position'] > li",
        "div[class*='recruit'] div[class*='item']",
        "div[class*='job'][class*='item']",
        "div.card",
    )
    cards = []
    for sel in card_sels:
        found = soup.select(sel)
        if len(found) >= 2:
            cards = found
            break
    if not cards:
        # 回落：含「详情」链接且邻近有公司/地点/更新日期文本的块
        cards = _guess_cards_from_links(soup, base_url)

    for card in cards:
        title, link = _card_title_link(card, base_url)
        title = sanitize_job_title(title)
        if not title or is_noise_title(title):
            continue
        labeled = _card_labeled_meta(card)
        company = _card_field(card, ("company", "org", "unit", "employer"))
        if not company:
            company = _guess_company_text(card)
        location = _card_field(card, ("loc", "city", "place", "address", "work-city"))
        if not location:
            location = labeled.get("location")
        if not location:
            location = _guess_location_text(card, title, company)
        if is_noise_location(location):
            location = None
        updated = _card_field(card, ("date", "time", "update", "publish"))
        if updated and not _DATE_RE.search(updated):
            # class 命中但文案是整段「更新日期：…」
            lm = _LABELED_DATE_RE.search(updated) or _DATE_RE.search(updated)
            updated = lm.group(1) if lm else updated
        if not updated:
            updated = labeled.get("updated")
        if not updated:
            m = _DATE_RE.search(card.get_text(" ", strip=True))
            updated = m.group(1) if m else None
        headcount = labeled.get("headcount")
        if not headcount:
            hc_raw = _card_field(card, ("headcount", "hc", "quota", "number", "count"))
            if hc_raw:
                from app.collector.label_fields import normalize_headcount

                headcount = normalize_headcount(hc_raw)
        category = labeled.get("category") or _card_field(
            card, ("category", "type", "job-type", "position-type")
        )

        if link:
            apply_url = link
        else:
            from app.collector.adapters.generic import synthetic_job_url

            apply_url = synthetic_job_url(base_url, title, location, category, company)
        key = f"{title}|{apply_url}|{company or ''}"
        if key in seen:
            continue
        seen.add(key)

        project, bucket, tags = classify_from_text(
            title, f"{company or ''} {location or ''} {category or ''}"
        )
        if channel_project:
            project = channel_project
            bucket = channel_bucket
        # 社招频道强制
        if channel_project == "社会招聘":
            project = "社会招聘"
            bucket = None

        extras: dict = {
            "adapter": "generic",
            "source": "html_job_card",
            "needs_fetch": bool(link and "__job=" not in (link or "")),
            "list_url": base_url,
            "from_card": True,
        }
        if company:
            extras["company"] = company
        if updated:
            extras["published_at"] = updated
            extras["list_updated_at"] = updated
        if category:
            extras["raw_category"] = category

        out.append(
            ParseResult(
                title=title,
                recruit_project=project,
                recruit_bucket=bucket,
                work_location=location,
                raw_category=category,
                headcount=headcount,
                job_tags=tags or extract_job_tags(title),
                parse_status="ok",
                confidence=0.5 if link else 0.4,
                apply_url=apply_url,
                extras=extras,
            )
        )
        if len(out) >= limit:
            break
    # 卡片无日期时，用页面源码更新时间（平台 version 等）填补翻页窗信号
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


def _guess_cards_from_links(soup, base_url: str) -> list:
    blocks = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        href = a.get("href") or ""
        if not text or is_noise_title(text) or is_noise_nav_url(href, text):
            continue
        full = urljoin(base_url, href)
        if not is_job_detail_link(full, text) and "detail" not in href.lower() and "job" not in href.lower():
            # 仍可能是岗位名链接
            if len(text) < 4 or len(text) > 40:
                continue
        parent = a.parent
        for _ in range(4):
            if parent is None:
                break
            blob = parent.get_text(" ", strip=True)
            if (
                company_hint_in(blob)
                or _LABELED_DATE_RE.search(blob)
                or _LABELED_HEADCOUNT_RE.search(blob)
                or _DATE_RE.search(blob)
            ):
                blocks.append(parent)
                break
            parent = parent.parent
    # 去重（按 id）
    seen_ids: set[int] = set()
    uniq = []
    for b in blocks:
        i = id(b)
        if i in seen_ids:
            continue
        seen_ids.add(i)
        uniq.append(b)
    return uniq[:60]


def company_hint_in(text: str) -> bool:
    return bool(_COMPANY_HINT_RE.search(text or ""))


def _card_title_link(card, base_url: str) -> tuple[str | None, str | None]:
    for sel in (
        "h3",
        "h2",
        "h4",
        ".title",
        ".name",
        ".job-name",
        ".position-name",
        ".job-title",
        ".post-title",
        "a",
    ):
        node = card.select_one(sel) if sel.startswith(".") or sel == "a" else card.find(sel)
        if sel == "a":
            node = None
            for a in card.find_all("a", href=True):
                t = a.get_text(" ", strip=True)
                if t and not is_noise_title(t) and 2 <= len(t) <= 60:
                    node = a
                    break
        if not node:
            continue
        title = node.get_text(" ", strip=True)
        if not title or is_noise_title(title):
            continue
        # 跳过频道壳/统计文案
        compact = re.sub(r"\s+", "", title)
        if compact in {"校园招聘", "社会招聘", "实习生招聘", "在招职位"} or re.match(
            r"在招职位\d+", compact
        ):
            continue
        href = None
        if node.name == "a" and node.get("href"):
            href = urljoin(base_url, node["href"]).split("#")[0]
        else:
            a = node.find("a", href=True) or card.find("a", href=True)
            if a and a.get("href"):
                href = urljoin(base_url, a["href"]).split("#")[0]
        return title, href
    return None, None


def _card_field(card, class_hints: tuple[str, ...]) -> str | None:
    for hint in class_hints:
        for node in card.find_all(True):
            cls = " ".join(node.get("class") or []).lower()
            if hint.strip() in cls:
                text = node.get_text(" ", strip=True)
                if text and len(text) <= 80:
                    return text
    return None


def _guess_company_text(card) -> str | None:
    for line in card.get_text("\n", strip=True).splitlines():
        s = line.strip()
        if 4 <= len(s) <= 60 and _COMPANY_HINT_RE.search(s):
            return s
    return None


def _guess_location_text(card, title: str | None, company: str | None) -> str | None:
    blob = card.get_text(" ", strip=True)
    for sep in ("|", "｜", "/", "·"):
        if sep in blob:
            parts = [p.strip() for p in blob.split(sep)]
            for p in parts:
                if p and p != title and p != company and 2 <= len(p) <= 20:
                    if _DATE_RE.search(p) or _COMPANY_HINT_RE.search(p):
                        continue
                    if re.search(r"(市|省|区|县|北京|上海|广州|深圳|长沙|武汉|成都|杭州|南京)", p):
                        return p
    return None


# ---- 列表翻页（校园/实习卡片网格）----

_NEXT_TEXT_RE = re.compile(r"^\s*(下一页|下页|后一页|next|>|›|»|→)\s*$", re.I)
_NEXT_LABEL_RE = re.compile(r"(下一页|下页|next\s*page|\bnext\b)", re.I)
_DISABLED_RE = re.compile(r"(disabled|is-disabled|btn-disabled|ant-pagination-disabled)", re.I)
_PAGE_PARAM_KEYS = (
    "page",
    "pageNo",
    "pageNum",
    "pageIndex",
    "p",
    "current",
    "currentPage",
    "pn",
)
FetchPage = Callable[[str], str]


def _control_disabled(node) -> bool:
    if node is None:
        return True
    if (node.get("aria-disabled") or "").lower() in ("true", "1"):
        return True
    if node.has_attr("disabled"):
        return True
    cls = " ".join(node.get("class") or [])
    if _DISABLED_RE.search(cls):
        return True
    parent = node.parent
    for _ in range(3):
        if parent is None:
            break
        pcls = " ".join(parent.get("class") or [])
        if _DISABLED_RE.search(pcls):
            return True
        if (parent.get("aria-disabled") or "").lower() in ("true", "1"):
            return True
        parent = parent.parent
    return False


def _extract_page_num(url: str) -> int | None:
    q = parse_qs(urlparse(url or "").query)
    for key in _PAGE_PARAM_KEYS:
        vals = q.get(key) or q.get(key.lower())
        if not vals:
            continue
        try:
            n = int(str(vals[0]).strip())
            if n >= 1:
                return n
        except ValueError:
            continue
    return None


def _replace_page_param(url: str, page: int, param_key: str | None = None) -> str:
    parsed = urlparse(url)
    q = parse_qs(parsed.query, keep_blank_values=True)
    key = param_key
    if not key:
        for cand in _PAGE_PARAM_KEYS:
            if cand in q or cand.lower() in q:
                key = cand if cand in q else cand.lower()
                break
        key = key or "page"
    q[key] = [str(page)]
    # flatten for urlencode
    flat: list[tuple[str, str]] = []
    for k, vals in q.items():
        for v in vals:
            flat.append((k, v))
    return urlunparse(parsed._replace(query=urlencode(flat)))


def _detect_active_page(soup) -> int | None:
    for sel in (
        ".pagination .active",
        ".pager .active",
        ".el-pager .active",
        "li.active",
        "a.active",
        "[aria-current='page']",
    ):
        node = soup.select_one(sel)
        if not node:
            continue
        text = node.get_text(" ", strip=True)
        m = re.search(r"(\d+)", text or "")
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
        href = node.get("href") if node.name == "a" else None
        if not href:
            a = node.find("a", href=True)
            href = a.get("href") if a else None
        if href:
            n = _extract_page_num(href)
            if n:
                return n
    return None


def _collect_page_links(base_url: str, soup) -> dict[int, str]:
    out: dict[int, str] = {}
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        full = urljoin(base_url, href).split("#")[0]
        n = _extract_page_num(full)
        if n is None:
            text = (a.get_text(" ", strip=True) or "").strip()
            if re.fullmatch(r"\d{1,4}", text):
                try:
                    n = int(text)
                except ValueError:
                    n = None
                if n and n >= 1:
                    # 数字页码链可能无 page 参数（路径型）；仍记录
                    out.setdefault(n, full)
            continue
        out.setdefault(n, full)
    return out


def find_next_page_url(current_url: str, html: str) -> str | None:
    """
    解析下一页 URL。
    优先：rel=next / 文案「下一页」「>」等；其次 page 查询参数 +1。
    下一页控件 disabled / 缺失 → None。
    """
    if not html:
        return None
    soup = soup_from_html(html)

    # 显式 next 链接
    candidates: list = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        rel = (a.get("rel") or [])
        rel_s = " ".join(rel) if isinstance(rel, list) else str(rel)
        label = f"{a.get('aria-label') or ''} {a.get('title') or ''}"
        text = a.get_text(" ", strip=True) or ""
        score = 0
        if re.search(r"\bnext\b", rel_s, re.I):
            score += 5
        if _NEXT_LABEL_RE.search(label):
            score += 4
        if _NEXT_TEXT_RE.match(text):
            score += 4
        cls = " ".join(a.get("class") or []).lower()
        if re.search(r"(next|page-next|btn-next)", cls):
            score += 3
        parent_cls = " ".join((a.parent.get("class") if a.parent else None) or []).lower()
        if re.search(r"(next|page-next)", parent_cls):
            score += 2
        if score:
            candidates.append((score, a))

    candidates.sort(key=lambda x: -x[0])
    for _score, a in candidates:
        if _control_disabled(a):
            return None
        href = (a.get("href") or "").strip()
        full = urljoin(current_url, href).split("#")[0]
        if urlparse(full).scheme not in ("http", "https"):
            continue
        # 避免「下一页」指向同页
        if full.rstrip("/") == (current_url or "").split("#")[0].rstrip("/"):
            continue
        return full

    # 禁用的下一页（非 <a>）：视为无下一页
    for node in soup.find_all(["span", "button", "li", "div"]):
        text = node.get_text(" ", strip=True) or ""
        label = f"{node.get('aria-label') or ''} {node.get('title') or ''}"
        cls = " ".join(node.get("class") or [])
        if not (
            _NEXT_TEXT_RE.match(text)
            or _NEXT_LABEL_RE.search(label)
            or re.search(r"(next|page-next|btn-next)", cls, re.I)
        ):
            continue
        if _control_disabled(node) or node.name != "a":
            # 无可用 <a> 的 next 控件 → 停止
            if _control_disabled(node) or not node.find("a", href=True):
                return None

    # HTTP page 参数：从分页数字链或当前 URL 推下一页
    page_links = _collect_page_links(current_url, soup)
    active = _detect_active_page(soup) or _extract_page_num(current_url) or 1
    if (active + 1) in page_links:
        return page_links[active + 1]
    if page_links:
        # 用已有链接的参数名构造 page=active+1
        sample = next(iter(page_links.values()))
        key = None
        q = parse_qs(urlparse(sample).query)
        for cand in _PAGE_PARAM_KEYS:
            if cand in q:
                key = cand
                break
        if key:
            return _replace_page_param(sample, active + 1, param_key=key)
        # 路径型：若存在更大页码链接已在上面返回；否则停
        if any(n > active for n in page_links):
            nxt = min(n for n in page_links if n > active)
            return page_links[nxt]

    cur_page = _extract_page_num(current_url)
    if cur_page is not None and page_links:
        return _replace_page_param(current_url, cur_page + 1)
    return None


def filter_posts_by_list_window(
    posts: list[ParseResult],
    *,
    months: int | None = None,
    today: date | None = None,
    keep_outside: bool = False,
) -> tuple[list[ParseResult], bool]:
    """
    按列表「更新日期」划分窗内/窗外，并决定是否停止继续翻页。

    停止策略（假定列表大致按日期降序，亦兼容乱序）：
    - 有日期且全部早于窗口 → keep_outside=False 时丢弃本页；True 时保留并标注，停止翻页；
    - 有日期且出现窗外 → 窗内必留；keep_outside 时窗外也留（标注），停止翻页；
    - 全部在窗内或无日期可解析 → 全部保留，可继续翻页。
    """
    if not posts:
        return [], False
    cutoff = list_date_cutoff(months=months, today=today)
    in_window: list[ParseResult] = []
    outside: list[ParseResult] = []
    dated_out = 0
    dated_in = 0
    for p in posts:
        extras = p.extras if isinstance(p.extras, dict) else {}
        raw = extras.get("list_updated_at") or extras.get("published_at") or extras.get("open_at")
        d = parse_date_loose(raw if isinstance(raw, str) else None)
        if d is None:
            in_window.append(p)
            continue
        if d >= cutoff:
            dated_in += 1
            extras = dict(extras)
            extras["outside_lookback"] = False
            p.extras = extras
            in_window.append(p)
        else:
            dated_out += 1
            extras = dict(extras)
            extras["outside_lookback"] = True
            extras["outside_lookback_months"] = resolve_list_lookback_months(months)
            p.extras = extras
            outside.append(p)
    if dated_out and not dated_in:
        if keep_outside:
            return outside, True
        return [], True
    if dated_out:
        if keep_outside:
            return in_window + outside, True
        return in_window, True
    return in_window, False


def enumerate_list_with_pagination(
    start_url: str,
    html: str,
    *,
    fetch_page: FetchPage | None = None,
    enumerate_page: Callable[[str, str], list[ParseResult]] | None = None,
    lookback_months: int | None = None,
    today: date | None = None,
    max_pages: int = 40,
    max_posts: int = 200,
    per_page_limit: int = 40,
    channel_project: str | None = None,
    channel_bucket: str | None = None,
    keep_outside: bool = False,
    known_urls: set[str] | None = None,
    known_titles: set[str] | None = None,
) -> list[ParseResult]:
    """
    多页列表枚举：抽完当前页 → 跟下一页，直到无下一页或日期窗外。
    fetch_page 为空时仅处理当前页（不破坏单页站）。
    keep_outside=True：窗外岗位仍保留并标注 outside_lookback（供勾选，不默认识别失败）。
    """
    months = resolve_list_lookback_months(lookback_months)

    def _page_all_known(posts: list[ParseResult]) -> bool:
        if not posts or known_urls is None or known_titles is None:
            return False
        from app.collector.fill_from_url import candidate_is_known

        for p in posts:
            if candidate_is_known(
                title=p.title,
                apply_url=p.apply_url,
                source_url=start_url,
                known_urls=known_urls,
                known_titles=known_titles,
            ):
                continue
            return False
        return True

    def _enum(u: str, h: str) -> list[ParseResult]:
        if enumerate_page is not None:
            return enumerate_page(u, h)
        return enumerate_job_cards(
            u,
            h,
            limit=per_page_limit,
            channel_project=channel_project,
            channel_bucket=channel_bucket,
        )

    out: list[ParseResult] = []
    seen: set[str] = set()
    url = start_url
    page_html = html
    visited: set[str] = set()

    for _ in range(max(1, int(max_pages))):
        key = (url or "").split("#")[0].rstrip("/").lower()
        if key in visited:
            break
        visited.add(key)

        page_posts = _enum(url, page_html)
        # 无 fetch 时仅单页枚举，不做日期窗裁剪（兼容离线 fixture / 单页站）
        if fetch_page is None:
            kept, stop_further = page_posts, True
        else:
            kept, stop_further = filter_posts_by_list_window(
                page_posts,
                months=months,
                today=today,
                keep_outside=keep_outside,
            )
        for p in kept:
            from app.collector.fill_from_url import _norm_url_key

            pk = f"{(p.title or '').strip()}|{_norm_url_key(p.apply_url)}"
            if pk in seen:
                continue
            seen.add(pk)
            extras = p.extras if isinstance(p.extras, dict) else {}
            extras["list_page_url"] = url
            p.extras = extras
            out.append(p)
            if len(out) >= max_posts:
                return out

        if _page_all_known(kept):
            break

        if stop_further:
            break
        if fetch_page is None:
            break
        next_url = find_next_page_url(url, page_html)
        if not next_url:
            break
        next_key = next_url.split("#")[0].rstrip("/").lower()
        if next_key in visited:
            break
        try:
            page_html = fetch_page(next_url)
        except Exception:
            break
        if not (page_html or "").strip():
            break
        url = next_url

    return out


def should_paginate_channel(channel: str | None) -> bool:
    """仅校园招聘 / 实习生招聘列表翻页；社招与未知频道由调用方决定。"""
    return channel in ("campus", "intern")
