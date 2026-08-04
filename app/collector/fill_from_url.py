"""从网申 / 原文链接识别岗位并生成表单回填字段。"""

from __future__ import annotations

import re
import copy
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from app.collector.adapters import feishu as feishu_adapter
from app.collector.adapters import hotjob as hotjob_adapter
from app.collector.adapters import moka as moka_adapter
from app.collector.adapters import zhiye as zhiye_adapter
from app.collector.adapters.base import ParseResult
from app.collector.adapters.generic import enumerate_job_table_rows
from app.collector.adapters.router import DEFAULT_HEADERS, parse_html, pick_parser
from app.collector.filters import (
    is_chrome_shell_title,
    is_closed_or_referral_jd,
    is_closed_or_referral_title,
    is_job_detail_url,
    is_job_portal_listing_url,
    is_noise_title,
    map_recruit_bucket,
    recover_location_from_jd,
    resolve_grad_batch,
)
from app.collector.label_fields import (
    extract_education_requirement,
    normalize_headcount,
    normalize_salary_range,
    summarize_jd_dedupe,
    strip_redundant_jd_meta,
)


# 表单可回填的字段（与 JobEditForm 对齐）
FILLABLE_KEYS = (
    "title",
    "graduation_batch",
    "recruit_project",
    "recruit_bucket",
    "work_location",
    "open_at",
    "deadline",
    "source_url",
    "apply_url",
    "jd_text",
    "raw_category",
    "education",
    "salary_range",
    "headcount",
    "group_name",
)


@dataclass
class FillCandidate:
    """单个可回填岗位。"""

    fields: dict[str, str]
    label: str
    summary: str
    needs_fetch: bool = False
    detail_url: str | None = None
    list_hint: ParseResult | None = None
    # 列表更新日期超出近半年窗（仍可勾选导入）
    outside_lookback: bool = False


@dataclass
class FillFromUrlResult:
    ok: bool
    error: str | None = None
    candidates: list[FillCandidate] = field(default_factory=list)
    is_list: bool = False
    total: int = 0
    adapter: str | None = None
    page_url: str = ""
    # True：页面已抓到但明确无招聘/岗位信息（可软删）；网络/配置错误保持 False
    no_job_posting: bool = False
    # 提示：如「识别到 N 个，其中 M 个更新日期超出近半年」
    notice: str | None = None
    outside_lookback_count: int = 0


def is_reidentify_transport_error(error: str | None) -> bool:
    """网络/链接配置类错误 → 失败，不软删。"""
    e = (error or "").strip()
    if not e:
        return False
    markers = (
        "请求超时",
        "网络连接",
        "网络请求失败",
        "错误状态码",
        "无可用网申",
        "链接格式无效",
        "请先填写",
        "识别异常",
        "识别失败",
        "所选岗位索引无效",
        "未能匹配识别结果",
        "保存失败",
    )
    return any(m in e for m in markers)


_NO_JOB_CHROME_RE = re.compile(
    r"(欢迎登录|请登录|个人中心|用户登录|登录注册|修改密码|"
    r"招聘系统|招贤纳才|招聘首页|招聘门户|微官网|"
    r"兼容模式|旧版\s*IE|IE\s*浏览器|温馨提示|"
    r"You need to enable JavaScript|"
    r"岗位已关闭|职位已关闭|网页已关停|页面已关闭)",
    re.I,
)
_JOB_ROLE_HINT_RE = re.compile(
    r"(工程师|开发|算法|产品|运营|设计|分析师|研究员|专员|经理|"
    r"管培|实习|顾问|会计|法务|销售|测试|前端|后端|数据)",
)


def fields_look_like_no_job_posting(fields: dict[str, str] | None) -> bool:
    """
    识别结果像「无真实岗位」：关停/内推壳、chrome 壳标题且无实质 JD、标题与 JD 皆空。
    有正常岗位名、仅 JD 偏短的列表行不算。
    """
    f = fields or {}
    title = (f.get("title") or "").strip()
    jd = (f.get("jd_text") or "").strip()
    if is_closed_or_referral_title(title) or is_closed_or_referral_jd(jd):
        return True
    if not title and not jd:
        return True
    shell = (
        not title
        or is_noise_title(title)
        or is_chrome_shell_title(title)
        or _is_shell_recruit_title(title)
    )
    if shell and len(jd) < 40:
        return True
    # 登录/门户壳被误当成标题（如「欢迎登录个人中心」）且无岗位语义
    blob = f"{title}\n{jd}"
    if _NO_JOB_CHROME_RE.search(blob) and not _JOB_ROLE_HINT_RE.search(title):
        if len(jd) < 80 or _NO_JOB_CHROME_RE.search(jd):
            return True
    return False

def resolve_result_is_no_job_posting(result: FillFromUrlResult) -> bool:
    """resolve_jobs_from_url 结果是否表示页面无招聘信息（非网络失败）。"""
    if result.no_job_posting:
        return True
    if result.ok and result.candidates:
        return False
    if is_reidentify_transport_error(result.error):
        return False
    err = (result.error or "").strip()
    if not err:
        # 抓取成功但无候选
        return True
    no_job_markers = (
        "未能识别为岗位页",
        "未能识别岗位信息",
        "页面内容为空",
        "列表为空",
        "无招聘",
        "岗位已关闭",
        "职位已关闭",
    )
    return any(m in err for m in no_job_markers)


def pick_fill_url(apply_url: str | None, source_url: str | None) -> str:
    """优先岗位申请/网申链接，其次校招/原文列表链接。"""
    for raw in (apply_url, source_url):
        u = (raw or "").strip()
        if u.startswith("http://") or u.startswith("https://"):
            return u
    return (apply_url or source_url or "").strip()


def normalize_job_link_fields(
    *,
    apply_or_detail: str | None,
    list_or_campus: str | None,
    page_url: str | None = None,
) -> tuple[str, str]:
    """
    统一岗位链接字段：(apply_url, source_url)。

    - 有独立申请/详情链 → apply_url 用申请链，source_url 用列表/校招页（缺则同申请链）
    - 只有列表/校招页 → 仅保留 source_url，apply_url 留空，避免误称具体网申
    """
    apply = (apply_or_detail or "").strip()
    source = (list_or_campus or "").strip()
    page = (page_url or "").strip()

    def _ok(u: str) -> bool:
        return u.startswith("http://") or u.startswith("https://")

    campus = source if _ok(source) else (page if _ok(page) else "")
    if _ok(apply) and "__job=" in apply:
        # 合成身份链不是可打开的真实岗位详情。
        src = campus or apply.split("?")[0]
        return "", src
    if _ok(apply):
        if is_job_portal_listing_url(apply) and not is_job_detail_url(apply):
            return "", (campus or apply)
        return apply, (campus or apply)
    # 无申请链：只保留校招/列表页作为内部来源。
    fallback = campus or (apply if _ok(apply) else "")
    return "", fallback


def _chinese_network_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "请求超时，请检查网络后重试"
    if isinstance(exc, httpx.ConnectError):
        return "网络连接失败，无法打开该链接"
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code if exc.response is not None else "?"
        return f"页面返回错误状态码 {code}"
    msg = str(exc).strip() or exc.__class__.__name__
    return f"网络请求失败：{msg}"


def _fetch_html(url: str, *, timeout: float = 45.0) -> str:
    """抓取页面 HTML；hotjob 在 wecruit WAF 405 时回退 www 同路径。"""
    candidates = (
        hotjob_adapter.page_url_candidates(url)
        if hotjob_adapter.can_handle(url)
        else [url]
    )
    last_exc: Exception | None = None
    with httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=timeout) as client:
        for cand in candidates:
            try:
                resp = client.get(cand)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                continue
            text = resp.text or ""
            if hotjob_adapter.can_handle(cand) and hotjob_adapter.is_waf_block_page(
                text, status_code=resp.status_code
            ):
                last_exc = httpx.HTTPStatusError(
                    f"Client error '{resp.status_code} WAF' for url '{cand}'",
                    request=resp.request,
                    response=resp,
                )
                continue
            try:
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                continue
            if text.strip():
                return text
            last_exc = ValueError("empty html")
    if last_exc:
        raise last_exc
    return ""


def _merge_list_hint(result: ParseResult, list_hint: ParseResult | None) -> ParseResult:
    if list_hint is None:
        return result
    title = (result.title or "").strip()
    if is_noise_title(title) or not title:
        if list_hint.title and not is_noise_title(list_hint.title):
            result.title = list_hint.title
    if not result.work_location and list_hint.work_location:
        result.work_location = list_hint.work_location
    if not result.recruit_project and list_hint.recruit_project:
        result.recruit_project = list_hint.recruit_project
    if not result.recruit_bucket and list_hint.recruit_bucket:
        result.recruit_bucket = list_hint.recruit_bucket
    if not result.deadline and list_hint.deadline:
        result.deadline = list_hint.deadline
    if not result.raw_category and list_hint.raw_category:
        result.raw_category = list_hint.raw_category
    if list_hint.job_tags and not result.job_tags:
        result.job_tags = list_hint.job_tags
    return result


def parse_result_to_form_fields(
    result: ParseResult,
    *,
    page_url: str,
    keep_company: str | None = None,
) -> dict[str, str]:
    """ParseResult → 编辑表单字符串字段。"""
    title = (result.title or "").strip()
    extras = result.extras if isinstance(result.extras, dict) else {}
    batch = resolve_grad_batch(
        title=title,
        jd_text=result.jd_text,
        existing=extras.get("graduation_batch") if isinstance(extras.get("graduation_batch"), str) else None,
    )
    project = (result.recruit_project or "").strip() or None
    bucket = (result.recruit_bucket or "").strip() or None
    if project and any(k in project for k in ("社会招聘", "社招")):
        bucket = None
    elif not bucket:
        bucket = map_recruit_bucket(project, title)

    list_page = ""
    if isinstance(extras.get("list_page_url"), str):
        list_page = extras["list_page_url"].strip()
    apply, source = normalize_job_link_fields(
        apply_or_detail=(result.apply_url or "").strip() or None,
        list_or_campus=list_page or page_url or None,
        page_url=page_url,
    )
    if hotjob_adapter.can_handle(apply or source or page_url):
        ch = extras.get("portal_channel") if isinstance(extras.get("portal_channel"), str) else None
        hint = {
            "recruit_project": project or "",
            "recruit_bucket": bucket or "",
            "title": title,
            "portal_channel": ch or "",
        }
        if apply:
            apply = hotjob_adapter.ensure_posdetail_post_type(apply, channel=ch, fields=hint)
        if source:
            source = hotjob_adapter.ensure_posdetail_post_type(source, channel=ch, fields=hint)

    from app.collector.filters import (
        resolve_group_and_company,
        strip_recruit_channel_company_suffix,
    )

    page_company = strip_recruit_channel_company_suffix(
        str(extras.get("company") or "").strip()
    )
    seed = strip_recruit_channel_company_suffix(keep_company)
    group_name, company = resolve_group_and_company(
        seed_company_name=seed or None,
        page_company=page_company or None,
    )
    if not company and seed:
        company = seed
    labeled = extras.get("labeled_fields") if isinstance(extras.get("labeled_fields"), dict) else {}
    edu = (getattr(result, "education", None) or "").strip()
    if not edu:
        edu = (
            extract_education_requirement(
                result.jd_text, labels=labeled, title=title
            )
            or ""
        )
    salary = (getattr(result, "salary_range", None) or "").strip()
    if not salary:
        salary = normalize_salary_range(labeled.get("salary_range")) or ""
    headcount = (getattr(result, "headcount", None) or "").strip()
    if not headcount:
        headcount = normalize_headcount(labeled.get("headcount")) or ""
    jd = (result.jd_text or "").strip()
    if jd:
        jd = (
            strip_redundant_jd_meta(
                jd,
                title=title,
                company=company or None,
                recruit_project=project,
                recruit_bucket=bucket,
                work_location=(result.work_location or "").strip() or None,
                raw_category=(result.raw_category or "").strip() or None,
                education=edu or None,
                salary_range=salary or None,
                headcount=headcount or None,
                extra_values=[
                    str(labeled.get("recruit_type") or ""),
                    str(labeled.get("work_type") or ""),
                ],
            )
            or jd
        )
    work_location = (result.work_location or "").strip()
    if not work_location:
        work_location = recover_location_from_jd(result.jd_text) or ""
    fields: dict[str, str] = {
        "title": title,
        "graduation_batch": batch or "",
        "recruit_project": project or "",
        "recruit_bucket": bucket or "",
        "work_location": work_location,
        "deadline": (result.deadline or "").strip(),
        "source_url": source,
        "apply_url": apply,
        "jd_text": jd,
        "raw_category": (result.raw_category or "").strip(),
        "education": edu,
        "salary_range": salary,
        "headcount": headcount,
        "group_name": group_name,
    }
    # 页面发布/更新日 → open_at（供审核「岗位发布时间」列；勿用本地 DB 戳）
    page_date = (
        str(extras.get("published_at") or "").strip()
        or str(extras.get("list_updated_at") or "").strip()
        or str(extras.get("open_at") or "").strip()
        or str(labeled.get("open_at") or "").strip()
    )
    if page_date:
        fields["open_at"] = page_date
    if company:
        fields["company"] = company
    elif keep_company is not None:
        fields["company"] = keep_company
    return fields


def _candidate_summary(fields: dict[str, str], *, adapter: str | None = None) -> str:
    lines = [
        f"岗位名称：{fields.get('title') or '（空）'}",
        f"类型：{fields.get('recruit_bucket') or fields.get('recruit_project') or '（空）'}",
        f"届别：{fields.get('graduation_batch') or '（空）'}",
        f"地点：{fields.get('work_location') or '（空）'}",
        f"截止：{fields.get('deadline') or '（空）'}",
        f"网申：{fields.get('apply_url') or '（空）'}",
        f"原文：{fields.get('source_url') or '（空）'}",
    ]
    if adapter:
        lines.insert(0, f"适配器：{adapter}")
    jd = fields.get("jd_text") or ""
    if jd:
        preview = jd if len(jd) <= 800 else jd[:800] + "…"
        lines.append("")
        lines.append("JD 预览：")
        lines.append(preview)
    else:
        lines.append("")
        lines.append("JD：（空）")
    return "\n".join(lines)


def _candidate_from_result(
    result: ParseResult,
    *,
    page_url: str,
    adapter: str | None = None,
    keep_company: str | None = None,
) -> FillCandidate:
    fields = parse_result_to_form_fields(result, page_url=page_url, keep_company=keep_company)
    extras = result.extras if isinstance(result.extras, dict) else {}
    outside = bool(extras.get("outside_lookback"))
    label = fields.get("title") or fields.get("apply_url") or "未命名岗位"
    loc = fields.get("work_location") or ""
    if loc:
        label = f"{label}  ·  {loc}"
    if outside:
        when = (
            str(extras.get("list_updated_at") or extras.get("published_at") or "").strip()
        )
        label = f"【超出近半年】{label}" + (f"  ·  {when}" if when else "")
        fields = dict(fields)
        fields["outside_lookback"] = "1"
    needs = bool(extras.get("needs_fetch"))
    detail = (result.apply_url or "").strip() or None
    if detail and "__job=" in detail:
        needs = False
    return FillCandidate(
        fields=fields,
        label=label,
        summary=_candidate_summary(fields, adapter=adapter),
        needs_fetch=needs,
        detail_url=detail if needs else None,
        list_hint=result if result.title else None,
        outside_lookback=outside,
    )


def _is_social_list_post(p: ParseResult) -> bool:
    """门户发现时排除社招列表项。"""
    extras = p.extras if isinstance(p.extras, dict) else {}
    if extras.get("portal_channel") == "social":
        return True
    rp = (p.recruit_project or "").strip()
    if rp in ("社会招聘", "社招") or "社会招聘" in rp:
        return True
    return False


def enumerate_list_posts(
    url: str,
    html: str,
    *,
    limit: int = 40,
    fetch_page=None,
    list_collect_months: int | None = None,
    discover_portal: bool = False,
    timeout: float = 60.0,
) -> tuple[str, list[ParseResult]]:
    """列表页枚举多岗位，返回 (adapter_name, posts)。校园/实习表格与卡片均可按日期窗翻页。"""
    from app.collector.portal_nav import (
        detect_recruit_channel,
        enumerate_detail_link_jobs,
        enumerate_job_cards,
        enumerate_list_with_pagination,
        should_paginate_channel,
    )

    _, adapter = pick_parser(url)
    # 自定义域名 HTML 已是 Moka #init-data 时强制走 moka（避免误判 generic）
    if adapter == "generic" and html and moka_adapter.load_init_data(html):
        adapter = "moka"
    _ch, ch_project, ch_bucket = detect_recruit_channel(url, html)
    # 门户发现遇到社招频道：不采当前社招列表（改由 sibling 拉校招/实习）
    skip_social_page = bool(discover_portal and _ch == "social")
    # 门户发现 / 校招实习：翻页；窗外岗位仍保留供勾选（不静默变「识别失败」）
    paginate = (not skip_social_page) and (
        should_paginate_channel(_ch) or (discover_portal and _ch != "social")
    )
    keep_outside = bool(discover_portal)
    # 大数据量门户（远景 250+ / Moka 126）：按 limit 拉满，勿静默卡在 40
    max_posts = max(int(limit or 40), 200) if fetch_page else max(int(limit or 40), 1)
    eff_timeout = float(timeout) if timeout is not None else 60.0

    table_posts: list[ParseResult] = []
    if not skip_social_page:
        probed_table = enumerate_job_table_rows(
            url,
            html,
            limit=limit,
            channel_project=ch_project,
            channel_bucket=ch_bucket,
        )
        if probed_table and paginate:
            table_posts = enumerate_list_with_pagination(
                url,
                html,
                fetch_page=fetch_page,
                enumerate_page=lambda u, h, lim=limit: enumerate_job_table_rows(
                    u,
                    h,
                    limit=lim,
                    channel_project=ch_project,
                    channel_bucket=ch_bucket,
                ),
                lookback_months=list_collect_months,
                per_page_limit=limit,
                max_posts=max_posts,
                max_pages=40,
                channel_project=ch_project,
                channel_bucket=ch_bucket,
                keep_outside=keep_outside,
            )
        else:
            table_posts = probed_table
    card_posts: list[ParseResult] = []
    if not table_posts and not skip_social_page:
        # 五矿/联储/精进类：卡片网格；有卡片则不再回退到无日期的「一链一行」
        probed_cards = enumerate_job_cards(
            url, html, limit=limit, channel_project=ch_project, channel_bucket=ch_bucket
        )
        if probed_cards:
            if paginate:
                card_posts = enumerate_list_with_pagination(
                    url,
                    html,
                    fetch_page=fetch_page,
                    lookback_months=list_collect_months,
                    channel_project=ch_project,
                    channel_bucket=ch_bucket,
                    per_page_limit=limit,
                    max_posts=max_posts,
                    max_pages=40,
                    keep_outside=keep_outside,
                )
            else:
                card_posts = probed_cards
        else:
            table_posts = enumerate_detail_link_jobs(
                url,
                html,
                limit=limit,
                channel_project=ch_project,
                channel_bucket=ch_bucket,
            )
    # 表格优先；无表格时用卡片 / 详情链（页面源码为主）
    if not table_posts and card_posts:
        table_posts = card_posts
    elif table_posts and card_posts:
        # 卡片补充表格未覆盖的标题
        seen_t = {(p.title or "") for p in table_posts}
        for cp in card_posts:
            if (cp.title or "") not in seen_t:
                table_posts.append(cp)

    posts: list[ParseResult] = []
    # 飞书等多页 API：门户发现时拉满，不为速度截断
    feishu_pages = 12 if discover_portal or limit >= 100 else 8
    if adapter == "feishu":
        posts = feishu_adapter.enumerate_positions(
            url, html, max_pages=feishu_pages, limit=limit
        )
    elif adapter == "hotjob":
        # HTML 表/卡/链已有岗位时绝不改走 API；仅 SPA 空壳再补充 listPosition
        if not table_posts and (
            hotjob_adapter.is_spa_list_shell(html) or bool(hotjob_adapter.extract_tenant(url))
        ):
            api_pages = 20 if discover_portal or limit >= 100 else 8
            posts = hotjob_adapter.enumerate_positions(
                url, html, max_pages=api_pages, limit=limit
            )
    elif adapter == "moka":
        # HTML #init-data 常只嵌前 ~15 条；门户发现时用 jobs/v2 翻页补全
        # 256 岗约需 6 页；limit≥200 时再放宽页数与超时
        api_pages = 30 if discover_portal or limit >= 200 else (20 if limit >= 100 else 8)
        posts = moka_adapter.enumerate_positions(
            url,
            html,
            limit=limit,
            max_pages=api_pages,
            list_collect_months=list_collect_months,
            timeout=max(eff_timeout, 45.0),
        )
    elif adapter == "zhiye":
        api_pages = 12 if discover_portal or limit >= 100 else 8
        posts = zhiye_adapter.enumerate_positions(
            url,
            html,
            limit=limit,
            max_pages=api_pages,
            timeout=max(eff_timeout, 45.0),
        )

    if table_posts:
        by_url = {(p.apply_url or "").split("?")[0]: p for p in posts if p.apply_url}
        merged: list[ParseResult] = []
        seen: set[str] = set()
        for tp in table_posts:
            key = (tp.title or "") + "|" + (tp.apply_url or "")
            seen.add(key)
            link = tp.apply_url or ""
            if link and "__job=" not in link:
                tp.extras["needs_fetch"] = True
            merged.append(tp)
        for p in posts:
            key = (p.title or "") + "|" + (p.apply_url or "")
            if key in seen:
                continue
            base = (p.apply_url or "").split("?")[0]
            if base and base in by_url:
                continue
            merged.append(p)
            if len(merged) >= limit:
                break
        posts = merged
    return adapter, posts


def _looks_like_list(result: ParseResult) -> bool:
    extras = result.extras or {}
    if extras.get("is_list") or extras.get("list_page"):
        return True
    if extras.get("source") in {
        "moka_list",
        "zhiye_list",
        "feishu_list",
        "feishu_list_shell",
        "hotjob_list_shell",
        "hotjob_list_api",
    }:
        return True
    if int(extras.get("detail_links") or 0) > 1:
        return True
    title = (result.title or "").strip()
    if not title or is_noise_title(title):
        # 无有效标题时尝试按列表枚举
        return True
    return False


def _is_shell_recruit_title(title: str | None) -> bool:
    """门户/频道壳标题，不宜当作具体岗位。"""
    t = (title or "").strip()
    if not t:
        return True
    if is_chrome_shell_title(t) or is_noise_title(t):
        return True
    compact = "".join(t.split())
    if compact in {
        "校园招聘",
        "社会招聘",
        "实习生招聘",
        "校招",
        "社招",
        "招聘",
        "欢迎",
        "首页",
        "招聘首页",
        "招聘官网",
        "招聘门户",
    }:
        return True
    if compact.endswith("校园招聘") and len(compact) <= 12:
        return True
    if compact.endswith("实习生招聘") and len(compact) <= 14:
        return True
    if compact.endswith("招聘官网") or compact.endswith("招聘门户"):
        return True
    if re.search(r"招聘官网$", compact) or re.search(r"集团招聘官网$", compact):
        return True
    return False


def _has_usable_detail(result: ParseResult) -> bool:
    title = (result.title or "").strip()
    if not title or is_noise_title(title) or _is_shell_recruit_title(title):
        return False
    # 有标题即视为可用详情；JD 可空（列表行）
    return True


def _enrich_moka_via_job_api(
    candidate: FillCandidate,
    *,
    detail_url: str,
    page_url: str,
    timeout: float,
    keep_company: str | None,
) -> FillCandidate | None:
    """Moka 自定义域名/SPA：website/job 拉 JD；失败返回 None 走 HTML 回退。"""
    hint = candidate.list_hint
    extras = hint.extras if hint and isinstance(hint.extras, dict) else {}
    job_id = str(extras.get("job_id") or "").strip()
    if not job_id:
        m = re.search(
            r"(?:[#&?]|/jobs?/|jobadid=)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            detail_url or "",
            re.I,
        )
        job_id = m.group(1) if m else ""
    org_id = str(extras.get("org_id") or "").strip()
    site_id = extras.get("site_id")
    aes_iv = str(extras.get("aes_iv") or "").strip()
    list_url = (
        str(extras.get("list_url") or extras.get("list_page_url") or "").strip()
        or (page_url or "").split("#")[0]
        or (detail_url or "").split("#")[0]
    )
    if not org_id or site_id is None:
        url_org, url_site, _ = moka_adapter.parse_site_from_url(list_url or detail_url)
        org_id = org_id or (url_org or "")
        if site_id is None:
            site_id = url_site
    if not (org_id and site_id is not None and job_id):
        return None
    if not aes_iv and list_url:
        try:
            list_html = _fetch_html(list_url, timeout=min(timeout, 60.0))
            init = moka_adapter.load_init_data(list_html) or {}
            aes_iv = str(init.get("aesIv") or "").strip()
            if not extras.get("company"):
                co = moka_adapter.company_from_init_data(list_html)
                if co and hint is not None:
                    hint.extras = dict(hint.extras or {})
                    hint.extras["company"] = co
        except Exception:  # noqa: BLE001
            aes_iv = ""
    if not aes_iv:
        return None
    detail_obj = moka_adapter.fetch_job_detail(
        org_id,
        site_id,
        job_id,
        aes_iv=aes_iv,
        list_url=list_url,
        timeout=timeout,
    )
    if not detail_obj:
        return None
    company = None
    if hint and isinstance(hint.extras, dict):
        company = str(hint.extras.get("company") or "").strip() or None
    api_pr = moka_adapter.parse_job_detail_dict(
        detail_obj, list_url=list_url or detail_url, company=company
    )
    if not api_pr or not api_pr.title:
        return None
    api_pr = _merge_list_hint(api_pr, hint)
    if not api_pr.apply_url and candidate.fields.get("apply_url"):
        api_pr.apply_url = candidate.fields.get("apply_url")
    source_page = detail_url if _has_usable_detail(api_pr) else page_url
    return _candidate_from_result(
        api_pr,
        page_url=source_page,
        adapter="moka",
        keep_company=keep_company,
    )


def enrich_candidate_detail(
    candidate: FillCandidate,
    *,
    page_url: str,
    ocr_enabled: bool | None = None,
    keep_company: str | None = None,
    timeout: float = 45.0,
) -> FillCandidate:
    """对列表项再抓详情页补全 JD 等字段（慢亦可，不跳过）。"""
    detail_url = (candidate.detail_url or candidate.fields.get("apply_url") or "").strip()
    if not detail_url or "__job=" in detail_url:
        return candidate
    jd_len = len((candidate.fields.get("jd_text") or "").strip())
    # 列表标记需抓详情，或 JD 过短且详情链不同于当前页时补抓
    page_key = (page_url or "").split("?")[0].rstrip("/").lower()
    detail_key = detail_url.split("?")[0].rstrip("/").lower()
    should_fetch = bool(candidate.needs_fetch) or (
        jd_len < 80 and bool(detail_key) and detail_key != page_key
    )
    if not should_fetch:
        return candidate
    # hotjob posDetail：HTML 是 SPA 壳，用 listPositionDetail 补 JD
    if hotjob_adapter.can_handle(detail_url) and re.search(
        r"/pb/(?:posDetail|position)\.html", detail_url, re.I
    ):
        api_pr = hotjob_adapter.parse_posdetail_from_api(detail_url, timeout=timeout)
        if api_pr and api_pr.title:
            api_pr = _merge_list_hint(api_pr, candidate.list_hint)
            if not api_pr.apply_url and candidate.fields.get("apply_url"):
                api_pr.apply_url = candidate.fields.get("apply_url")
            api_pr.apply_url = hotjob_adapter.ensure_posdetail_post_type(
                api_pr.apply_url or detail_url,
                fields=candidate.fields,
            )
            source_page = detail_url if _has_usable_detail(api_pr) else page_url
            return _candidate_from_result(
                api_pr,
                page_url=source_page,
                adapter="hotjob",
                keep_company=keep_company,
            )
    # Moka SPA：#/job/{id} 的 HTML 仍是列表壳，用 website/job 接口补 JD
    if moka_adapter.can_handle(detail_url) or moka_adapter.can_handle(page_url):
        moka_pr = _enrich_moka_via_job_api(
            candidate,
            detail_url=detail_url,
            page_url=page_url,
            timeout=timeout,
            keep_company=keep_company,
        )
        if moka_pr is not None:
            return moka_pr
    try:
        html = _fetch_html(detail_url, timeout=timeout)
    except Exception:
        # 详情失败则保留列表字段
        return candidate
    result = parse_html(detail_url, html, ocr_enabled=ocr_enabled)
    result = _merge_list_hint(result, candidate.list_hint)
    if result.apply_url and "__job=" not in (result.apply_url or ""):
        pass
    elif candidate.fields.get("apply_url"):
        result.apply_url = candidate.fields.get("apply_url")
    if hotjob_adapter.can_handle(detail_url):
        result.apply_url = hotjob_adapter.ensure_posdetail_post_type(
            result.apply_url or detail_url,
            fields=candidate.fields,
        )
    # 详情页 URL 作为原文；列表页保留在 page_url 语义下
    source_page = detail_url if _has_usable_detail(result) else page_url
    return _candidate_from_result(
        result,
        page_url=source_page,
        adapter=(result.extras or {}).get("adapter"),
        keep_company=keep_company,
    )


def _post_dedupe_key(p: ParseResult) -> str:
    return f"{(p.title or '').strip()}|{(p.apply_url or '').split('?')[0]}"


def _merge_sibling_channel_posts(
    url: str,
    html: str,
    posts: list[ParseResult],
    *,
    list_limit: int,
    timeout: float,
    ocr_enabled: bool | None,
    list_collect_months: int | None = None,
    fetch_html=None,
    discover_portal: bool = True,
) -> list[ParseResult]:
    """打开同站「实习生招聘 / 校园招聘」频道列表，合并岗位（不含社招）。"""
    from app.collector.portal_nav import expand_career_channel_urls

    getter = fetch_html or (lambda u, t=timeout: _fetch_html(u, timeout=t))
    # 门户模式先丢掉已混入的社招项
    merged = [p for p in posts if not _is_social_list_post(p)]
    seen = {_post_dedupe_key(p) for p in merged}
    base_key = (url or "").split("?")[0].rstrip("/").lower()
    for other in expand_career_channel_urls(url, html, include=("campus", "intern")):
        other_key = (other or "").split("?")[0].rstrip("/").lower()
        if not other_key or other_key == base_key:
            continue
        try:
            other_html = getter(other)
        except Exception:
            continue
        _ad, other_posts = enumerate_list_posts(
            other,
            other_html,
            limit=list_limit,
            fetch_page=lambda u, g=getter: g(u),
            list_collect_months=list_collect_months,
            discover_portal=discover_portal,
        )
        for p in other_posts:
            if _is_social_list_post(p):
                continue
            key = _post_dedupe_key(p)
            if key in seen:
                continue
            seen.add(key)
            merged.append(p)
            if len(merged) >= list_limit:
                return merged
    return merged


def _detail_usable_for_candidate(result: ParseResult) -> bool:
    """当前页是否可当作一条具体岗位候选。"""
    if (result.extras or {}).get("is_list") or _looks_like_list(result):
        return False
    if not _has_usable_detail(result):
        return False
    return bool(
        (result.jd_text and len(result.jd_text) > 40)
        or (
            result.title
            and not is_noise_title(result.title)
            and not _is_shell_recruit_title(result.title)
        )
    )


# 单企业单次采集默认批大小（可用 config.per_company_collect_batch 覆盖）
DEFAULT_PER_COMPANY_COLLECT_BATCH = 50


def discover_portal_jobs_from_url(
    url: str,
    *,
    html: str | None = None,
    ocr_enabled: bool | None = None,
    keep_company: str | None = None,
    list_limit: int | None = None,
    timeout: float | None = None,
    fetch: bool = True,
    list_collect_months: int | None = None,
    fetch_html=None,
    progress=None,
) -> FillFromUrlResult:
    """
    局部重新采集入口（岗位审核「重新识别」/ 应急粘贴 / 从链接识别）。

    以单 URL 为入口：展开同站校园招聘 + 实习生招聘（排除社招），
    按 list_collect_months（默认约 6 个月）列表翻页合并候选，供人工勾选后写入审核。
    默认 list_limit=50（单企业单批），避免一次加载数百岗卡死；剩余下次再采。
    """
    lim = int(list_limit) if list_limit is not None else DEFAULT_PER_COMPANY_COLLECT_BATCH
    return resolve_jobs_from_url(
        url,
        html=html,
        ocr_enabled=ocr_enabled,
        keep_company=keep_company,
        list_limit=max(lim, 1),
        timeout=timeout if timeout is not None else 90.0,
        fetch=fetch,
        list_collect_months=list_collect_months,
        discover_portal=True,
        fetch_html=fetch_html,
        progress=progress,
    )


def resolve_jobs_from_url(
    url: str,
    *,
    html: str | None = None,
    ocr_enabled: bool | None = None,
    keep_company: str | None = None,
    list_limit: int = 40,
    timeout: float | None = None,
    fetch: bool = True,
    list_collect_months: int | None = None,
    discover_portal: bool = False,
    fetch_html=None,
    progress=None,
) -> FillFromUrlResult:
    """
    打开链接并识别岗位。
    - 详情页：默认返回 1 个候选
    - 列表页：返回多个候选（列表字段；选中后再 enrich_candidate_detail）
    - discover_portal=True：展开同站校园+实习频道，按 list_collect_months（默认约 6 个月）
      翻页合并；即使当前是详情页也优先返回门户列表候选（供勾选导入）
    局部重采请优先调用 discover_portal_jobs_from_url。
    慢亦可：不因性能跳过门户展开 / 翻页 / 详情 enrich。
    progress：可选回调 str → None，供 UI 心跳日志（勿在回调里碰 Tk 控件，由调用方 after）。
    """
    url = (url or "").strip()
    if not url:
        return FillFromUrlResult(ok=False, error="请先填写网申链接或原文链接")
    if not (url.startswith("http://") or url.startswith("https://")):
        return FillFromUrlResult(ok=False, error="链接格式无效，需以 http:// 或 https:// 开头")

    def _progress(msg: str) -> None:
        if progress is None:
            return
        try:
            progress(msg)
        except Exception:  # noqa: BLE001
            pass

    # 门户发现默认更长超时；慢亦可，勿为速度截断
    eff_timeout = float(timeout) if timeout is not None else (90.0 if discover_portal else 45.0)
    from app.collector.filters import strip_recruit_channel_company_suffix

    keep_company = strip_recruit_channel_company_suffix(keep_company) or None

    getter = fetch_html or (lambda u, t=eff_timeout: _fetch_html(u, timeout=t))
    fetch_error: str | None = None
    _progress("正在打开链接…")
    try:
        if html is not None:
            content = html
        elif fetch:
            content = getter(url)
        else:
            content = ""
    except Exception as exc:  # noqa: BLE001
        # hotjob：HTML 被 WAF 拦时仍可用 listPosition（tenant 在 URL 内）
        if hotjob_adapter.can_handle(url) and hotjob_adapter.extract_tenant(url) and (
            fetch or fetch_html is not None
        ):
            content = ""
            fetch_error = _chinese_network_error(exc)
        else:
            return FillFromUrlResult(ok=False, error=_chinese_network_error(exc), page_url=url)

    if not (content or "").strip():
        if not (
            hotjob_adapter.can_handle(url)
            and hotjob_adapter.extract_tenant(url)
            and (fetch or fetch_html is not None)
        ):
            return FillFromUrlResult(
                ok=False,
                error=fetch_error or "页面内容为空，无法识别岗位",
                page_url=url,
                no_job_posting=True,
            )

    result = parse_html(url, content, ocr_enabled=ocr_enabled)
    adapter = (result.extras or {}).get("adapter") or pick_parser(url)[1]
    _progress(f"正在枚举岗位列表（适配器 {adapter or 'generic'}）…")

    # 先枚举列表：多岗表格/链接优先于壳标题（如「校园招聘」）；可联网时校园/实习翻页
    can_fetch_pages = bool(fetch or fetch_html is not None)
    fetch_page = (lambda u, g=getter: g(u)) if can_fetch_pages else None
    # 门户局部重采：尊重调用方 list_limit（默认单批 50，扫描时可略放大）
    portal_limit = max(int(list_limit or 40), 1) if discover_portal else int(list_limit or 40)
    _adapter_name, posts = enumerate_list_posts(
        url,
        content,
        limit=portal_limit,
        fetch_page=fetch_page,
        list_collect_months=list_collect_months,
        discover_portal=discover_portal,
        timeout=eff_timeout,
    )
    adapter = _adapter_name or adapter

    _progress(f"列表已解析 {len(posts)} 条，正在合并校招/实习频道…")
    # 联网或显式 portal 发现：从顶栏/详情上卷进入「实习生招聘 / 校园招聘」并合并列表岗
    if can_fetch_pages and (fetch or discover_portal):
        posts = _merge_sibling_channel_posts(
            url,
            content,
            posts,
            list_limit=portal_limit,
            timeout=eff_timeout,
            ocr_enabled=ocr_enabled,
            list_collect_months=list_collect_months,
            fetch_html=getter,
            discover_portal=discover_portal,
        )

    usable = [
        p
        for p in posts
        if (
            (p.title and not is_noise_title(p.title) and not _is_shell_recruit_title(p.title))
            or (
                p.apply_url
                and (p.extras or {}).get("needs_fetch")
                and p.title
                and not _is_shell_recruit_title(p.title)
            )
        )
        and not (discover_portal and _is_social_list_post(p))
    ]

    def _pack_candidates(items: list[ParseResult]) -> FillFromUrlResult:
        candidates = [
            _candidate_from_result(p, page_url=url, adapter=adapter, keep_company=keep_company)
            for p in items
        ]
        outside_n = sum(1 for c in candidates if c.outside_lookback)
        inside_n = len(candidates) - outside_n
        notice = None
        if outside_n and inside_n == 0:
            notice = (
                f"近半年未发现可导入岗位；列出 {outside_n} 个超出近半年的真实岗位，"
                "已标注「超出近半年」，默认不勾选，仍可手动勾选导入。"
            )
        elif outside_n:
            notice = (
                f"识别到 {len(candidates)} 个岗位，其中 {outside_n} 个更新日期超出近约半年；"
                "已列出并标注「超出近半年」，默认不勾选，仍可手动勾选导入。"
            )
        elif discover_portal and len(candidates) > 1:
            notice = f"识别到 {len(candidates)} 个校招/实习岗位（近约半年优先，已排除社招）。"
        return FillFromUrlResult(
            ok=True,
            candidates=candidates,
            is_list=len(candidates) > 1,
            total=len(candidates),
            adapter=adapter,
            page_url=url,
            notice=notice,
            outside_lookback_count=outside_n,
        )

    # 门户发现：有列表岗则优先返回全部列表候选；详情页岗位若不在列表中则追加
    if discover_portal and usable:
        if _detail_usable_for_candidate(result) and not _is_social_list_post(result):
            detail_key = _post_dedupe_key(result)
            if detail_key not in {_post_dedupe_key(p) for p in usable}:
                # 详情 URL 常比列表链完整，补进候选供勾选
                if not result.apply_url:
                    result.apply_url = url
                usable = [result, *usable]
        return _pack_candidates(usable)

    if len(usable) > 1 or (usable and (_looks_like_list(result) or not _has_usable_detail(result))):
        return _pack_candidates(usable)

    # 详情页：需有实质 JD 或非壳标题
    if _detail_usable_for_candidate(result):
        cand = _candidate_from_result(
            result, page_url=url, adapter=adapter, keep_company=keep_company
        )
        return FillFromUrlResult(
            ok=True,
            candidates=[cand],
            is_list=False,
            total=1,
            adapter=adapter,
            page_url=url,
        )

    if len(usable) == 1:
        cand = _candidate_from_result(
            usable[0], page_url=url, adapter=adapter, keep_company=keep_company
        )
        return FillFromUrlResult(
            ok=True,
            candidates=[cand],
            is_list=False,
            total=1,
            adapter=adapter,
            page_url=url,
        )

    host = urlparse(url).netloc or url
    # 页面抓到但无可用岗位（壳页/关停/空列表/仅行程导航噪声）
    # 注意：不因 listPosition「维护」单独短路——HTML 主路径已穷尽后才附带说明
    closed_hint = ""
    if is_closed_or_referral_title(result.title) or is_closed_or_referral_jd(result.jd_text):
        closed_hint = "岗位已关闭或仅内推；"
    elif fields_look_like_no_job_posting(
        {"title": result.title or "", "jd_text": (result.jd_text or "")[:200]}
    ):
        closed_hint = "页面无有效岗位；"
    api_hint = ""
    if adapter == "hotjob" and can_fetch_pages and hotjob_adapter.is_spa_list_shell(content):
        from app.collector.portal_nav import channel_from_url as _ch_from_url

        tid = hotjob_adapter.extract_tenant(url)
        ch = _ch_from_url(url) or "intern"
        if tid:
            probe = hotjob_adapter.fetch_position_list(
                tid,
                hotjob_adapter.recruit_type_for_channel(ch),
                page=1,
                page_size=1,
                list_url=hotjob_adapter.list_page_url(tid, ch),
                timeout=min(eff_timeout, 20.0),
            )
            if probe.get("_hotjob_maintenance"):
                api_hint = (
                    "页面源码为 SPA 空壳且无表/卡岗位；"
                    "补充接口 listPosition 亦返回维护提示，暂无法枚举。"
                )
    # 过滤掉行程/模板后为空：明确近半年无可导入岗（勿用导航凑数）
    if api_hint:
        empty_msg = api_hint
        no_posting = False
    elif discover_portal:
        empty_msg = (
            "近半年未发现可导入岗位（已排除校招行程、应聘指南及模板占位等非岗位内容）。"
        )
        no_posting = True
    else:
        empty_msg = (
            f"未能识别为岗位页（{host}）。{closed_hint}"
            "可能是门户首页、登录页或列表为空。"
        )
        no_posting = True
    return FillFromUrlResult(
        ok=False,
        error=empty_msg,
        adapter=adapter,
        page_url=url,
        no_job_posting=no_posting,
    )


def merge_fill_into_form(
    current: dict[str, Any] | None,
    filled: dict[str, str],
    *,
    overwrite_empty_only: bool = False,
) -> dict[str, str]:
    """
    把识别结果合并进当前表单值。
    识别结果含招聘单位时写入 company；含集团时写入 group_name（可为空字符串表示非集团）。
    """
    out = {k: str((current or {}).get(k) or "") for k in (*FILLABLE_KEYS, "company")}
    for key in FILLABLE_KEYS:
        if key == "group_name":
            # 允许显式清空集团；键缺失则不动
            if "group_name" not in (filled or {}):
                continue
            new_val = str((filled or {}).get("group_name") or "").strip()
            old = (out.get(key) or "").strip()
            if overwrite_empty_only and old and not new_val:
                continue
            out[key] = new_val
            continue
        new_val = (filled.get(key) or "").strip()
        if not new_val:
            continue
        old = (out.get(key) or "").strip()
        if overwrite_empty_only and old:
            continue
        out[key] = new_val
    # 识别到的页面招聘单位优先；否则保留原表单公司
    filled_co = str((filled or {}).get("company") or "").strip()
    if filled_co and filled_co != "未知企业":
        if not (overwrite_empty_only and (out.get("company") or "").strip()):
            out["company"] = filled_co
    elif not (out.get("company") or "").strip() and filled_co:
        out["company"] = filled_co
    return out


def _norm_url_key(url: str | None) -> str:
    from app.collector.filters import normalize_job_identity_url

    return normalize_job_identity_url(url)


def _norm_title_key(title: str | None) -> str:
    return (title or "").strip().lower()


def candidate_is_known(
    *,
    title: str | None,
    apply_url: str | None = None,
    source_url: str | None = None,
    detail_url: str | None = None,
    known_urls: set[str] | None = None,
    known_titles: set[str] | None = None,
) -> bool:
    """Match a collected row without collapsing same-title jobs at distinct URLs."""
    urls = known_urls or set()
    titles = known_titles or set()
    source_key = _norm_url_key(source_url)
    specific_urls: set[str] = set()
    for raw in (apply_url, detail_url):
        key = _norm_url_key(raw)
        if not key:
            continue
        if "__job=" in str(raw or "") or not source_key or key != source_key:
            specific_urls.add(key)
    if specific_urls:
        if urls:
            return bool(specific_urls & urls)
        title_key = _norm_title_key(title)
        return bool(title_key and title_key in titles)

    title_key = _norm_title_key(title)
    if source_key:
        return source_key in urls and bool(title_key and title_key in titles)
    return bool(title_key and title_key in titles)


def known_keys_from_jobs(
    jobs: list[dict[str, Any]] | None,
) -> tuple[set[str], set[str]]:
    """从已入库岗位提取 URL / 标题键，供分批采集跳过。"""
    urls: set[str] = set()
    titles: set[str] = set()
    for job in jobs or []:
        for key in ("apply_url", "source_url"):
            uk = _norm_url_key(job.get(key) if isinstance(job, dict) else None)
            if uk:
                urls.add(uk)
        tk = _norm_title_key(job.get("title") if isinstance(job, dict) else None)
        if tk:
            titles.add(tk)
    urls.discard("")
    titles.discard("")
    return urls, titles


def take_collect_batch(
    candidates: list[FillCandidate],
    *,
    known_urls: set[str] | None = None,
    known_titles: set[str] | None = None,
    batch_size: int = DEFAULT_PER_COMPANY_COLLECT_BATCH,
) -> tuple[list[FillCandidate], dict[str, int]]:
    """
    单企业单次最多取出 batch_size 个「尚未入库」候选。

    已按 URL（或标题）命中库内的跳过，便于 200 岗分 4 次采完。
    返回 (本批候选, {skipped_known, batch, remaining_after_batch, scanned}).
    """
    size = max(int(batch_size or DEFAULT_PER_COMPANY_COLLECT_BATCH), 1)
    urls = set(known_urls or ())
    titles = set(known_titles or ())
    batch: list[FillCandidate] = []
    skipped = 0
    for cand in candidates or []:
        fields = cand.fields or {}
        if candidate_is_known(
            title=fields.get("title"),
            apply_url=fields.get("apply_url"),
            source_url=fields.get("source_url"),
            detail_url=cand.detail_url,
            known_urls=urls,
            known_titles=titles,
        ):
            skipped += 1
            continue
        batch.append(cand)
        if len(batch) >= size:
            break
    # 粗算：扫描列表中尚未进入本批、也未计为 known 跳过的剩余
    seen_in_batch = {id(c) for c in batch}
    remaining = 0
    for cand in candidates or []:
        if id(cand) in seen_in_batch:
            continue
        fields = cand.fields or {}
        if candidate_is_known(
            title=fields.get("title"),
            apply_url=fields.get("apply_url"),
            source_url=fields.get("source_url"),
            detail_url=cand.detail_url,
            known_urls=urls,
            known_titles=titles,
        ):
            continue
        remaining += 1
    stats = {
        "skipped_known": skipped,
        "batch": len(batch),
        "remaining_after_batch": remaining,
        "scanned": len(candidates or []),
    }
    return batch, stats


def per_company_batch_size(cfg: dict[str, Any] | None = None) -> int:
    """读取配置中的单企业单批上限，默认 50。"""
    raw = (cfg or {}).get("per_company_collect_batch")
    try:
        n = int(raw) if raw is not None else DEFAULT_PER_COMPANY_COLLECT_BATCH
    except (TypeError, ValueError):
        n = DEFAULT_PER_COMPANY_COLLECT_BATCH
    return max(n, 1)


def scan_limit_for_batch(batch_size: int) -> int:
    """
    为找到「下一批未入库」岗位，枚举扫描上限略大于单批。
    例：batch=50 → 扫描最多 200，便于已入库 50 后仍能再取 50。
    """
    b = max(int(batch_size or DEFAULT_PER_COMPANY_COLLECT_BATCH), 1)
    return min(max(b * 4, b), 400)


def find_matching_fill_candidate(
    candidates: list[FillCandidate],
    *,
    title: str | None = None,
    source_url: str | None = None,
    apply_url: str | None = None,
) -> FillCandidate | None:
    """严格对齐原岗位：URL 归一化命中，或标题完全一致；对不上返回 None（不回退第一条）。"""
    if not candidates:
        return None
    want_urls = {
        _norm_url_key(u)
        for u in (apply_url, source_url)
        if (u or "").strip()
    }
    want_urls.discard("")
    want_title = (title or "").strip().lower()

    def _specific_url(raw: str | None) -> bool:
        value = (raw or "").strip()
        return bool(
            value
            and (
                "#/job/" in value.lower()
                or "__job=" in value
                or is_job_detail_url(value)
            )
        )

    specific_want = _specific_url(apply_url) or _specific_url(source_url)
    # A detail URL is authoritative. Do not let a shared Moka list-page
    # source_url match the first candidate when apply_url points elsewhere.
    for cand in candidates:
        fields = cand.fields or {}
        for key in ("apply_url", "detail_url"):
            raw = fields.get(key) if key != "detail_url" else cand.detail_url
            cu = _norm_url_key(raw if isinstance(raw, str) else None)
            if cu and cu == _norm_url_key(apply_url):
                return cand
    if not specific_want:
        source_key = _norm_url_key(source_url)
        if source_key:
            for cand in candidates:
                candidate_source = _norm_url_key((cand.fields or {}).get("source_url"))
                if candidate_source and candidate_source == source_key:
                    return cand
    if want_title:
        for cand in candidates:
            ct = ((cand.fields or {}).get("title") or "").strip().lower()
            if ct and ct == want_title and not specific_want:
                return cand
    return None


def _moka_direct_candidate_from_context(
    candidates: list[FillCandidate],
    detail_url: str,
) -> FillCandidate | None:
    """Build a direct Moka detail candidate when a paged list omitted the UUID."""
    if not moka_adapter.can_handle(detail_url):
        return None
    match = re.search(
        r"(?:#/job/|/jobs?/|jobadid=)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        detail_url,
        re.I,
    )
    if not match:
        return None
    context = next(
        (
            candidate
            for candidate in candidates
            if candidate.list_hint is not None
            and isinstance(candidate.list_hint.extras, dict)
            and candidate.list_hint.extras.get("aes_iv")
            and candidate.list_hint.extras.get("org_id")
            and candidate.list_hint.extras.get("site_id") is not None
        ),
        None,
    )
    if context is None or context.list_hint is None:
        return None

    base_url = detail_url.split("#", 1)[0].rstrip("/")
    hint = copy.deepcopy(context.list_hint)
    hint.title = None
    hint.extras = dict(hint.extras or {})
    hint.extras.update(
        {
            "job_id": match.group(1),
            "list_url": base_url,
            "list_page_url": base_url,
        }
    )
    fields = dict(context.fields or {})
    fields["title"] = ""
    fields["source_url"] = base_url
    fields["apply_url"] = detail_url
    return FillCandidate(
        fields=fields,
        label="Moka 详情直取",
        summary="列表首批未包含该 UUID，改用详情接口",
        needs_fetch=True,
        detail_url=detail_url,
        list_hint=hint,
    )


def match_fill_candidate(
    candidates: list[FillCandidate],
    *,
    title: str | None = None,
    source_url: str | None = None,
    apply_url: str | None = None,
) -> FillCandidate | None:
    """多候选时按 URL / 标题尽量对齐原岗位；对不上则取第一条。"""
    if not candidates:
        return None
    hit = find_matching_fill_candidate(
        candidates, title=title, source_url=source_url, apply_url=apply_url
    )
    if hit is not None:
        return hit
    return candidates[0]


_FIELD_LABEL_ZH = {
    "title": "岗位名称",
    "graduation_batch": "毕业批次",
    "recruit_project": "招聘项目",
    "recruit_bucket": "类型",
    "work_location": "工作地点",
    "deadline": "截止时间",
    "source_url": "原文链接",
    "apply_url": "网申链接",
    "jd_text": "JD",
    "raw_category": "岗位类型",
    "education": "学历要求",
    "salary_range": "薪资范围",
    "headcount": "招聘人数",
    "open_at": "岗位发布时间",
}


def summarize_reidentify_changes(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> list[str]:
    """列出重新识别相对原岗位有实质变化的字段（中文）。"""
    src = before or {}
    dst = after or {}
    changed: list[str] = []
    for key in FILLABLE_KEYS:
        old = str(src.get(key) or "").strip()
        new = str(dst.get(key) or "").strip()
        if not new or old == new:
            continue
        label = _FIELD_LABEL_ZH.get(key, key)
        if key == "jd_text":
            strip_note = summarize_jd_dedupe(old, new)
            if old:
                changed.append(f"{label}（{len(old)}→{len(new)} 字；{strip_note}）")
            else:
                changed.append(f"{label}（新写入 {len(new)} 字）")
        else:
            preview = new if len(new) <= 24 else new[:23] + "…"
            changed.append(f"{label}→{preview}")
    return changed


def apply_reidentify_fields(
    job: dict[str, Any],
    filled_fields: dict[str, str] | None,
    *,
    seed_company_name: str | None = None,
) -> dict[str, Any]:
    """
    将识别字段合并进岗位 dict，并剥 JD 重复元信息 / 补学历。
    页面公司名与种子公司不同时写入 group_name（集团）与子公司 company。
    """
    from app.collector.filters import resolve_group_and_company

    src = dict(job or {})
    form_vals = {k: str(src.get(k) or "") for k in (*FILLABLE_KEYS, "company")}
    filled = merge_fill_into_form(form_vals, filled_fields or {})
    # merge_fill 未覆盖的附加键
    for key in (
        "education",
        "salary_range",
        "headcount",
        "raw_category",
        "group_name",
        "company",
    ):
        new_val = str((filled_fields or {}).get(key) or "").strip()
        if new_val:
            filled[key] = new_val

    # 种子=门户集团名；勿把子公司 company 当成种子
    seed = (
        (seed_company_name or "").strip()
        or str(src.get("group_name") or "").strip()
        or str((filled_fields or {}).get("group_name") or "").strip()
    )
    # 页面招聘单位：识别结果优先，其次原岗位公司（若原岗位公司=种子则仍用识别值）
    page_co = str((filled_fields or {}).get("company") or "").strip()
    if not page_co or page_co == "未知企业":
        page_co = str(filled.get("company") or "").strip()
    if (not page_co or page_co == "未知企业") and seed:
        # 原 company 若就是种子本身，不要当成「页面单位」
        src_co = str(src.get("company") or "").strip()
        from app.collector.filters import companies_same

        if src_co and not companies_same(seed, src_co):
            page_co = src_co
    group_name, company = resolve_group_and_company(
        seed_company_name=seed or None,
        page_company=page_co or None,
    )
    # 始终按解析结果写回集团/公司，避免侧栏缺 group_name、公司被写成种子
    filled["group_name"] = group_name or ""
    if company and company != "未知企业":
        filled["company"] = company
    elif str(src.get("company") or "").strip():
        filled["company"] = str(src.get("company")).strip()

    # 再剥一次 JD（识别结果可能仍带页眉）
    jd = (filled.get("jd_text") or "").strip()
    title_for_meta = filled.get("title") or src.get("title")
    if jd:
        filled["jd_text"] = (
            strip_redundant_jd_meta(
                jd,
                title=title_for_meta,
                company=filled.get("company") or src.get("company"),
                recruit_project=filled.get("recruit_project") or src.get("recruit_project"),
                recruit_bucket=filled.get("recruit_bucket") or src.get("recruit_bucket"),
                work_location=filled.get("work_location") or src.get("work_location"),
                raw_category=filled.get("raw_category") or src.get("raw_category"),
                education=filled.get("education") or src.get("education"),
                salary_range=filled.get("salary_range") or src.get("salary_range"),
                headcount=filled.get("headcount") or src.get("headcount"),
            )
            or jd
        )
    if not (filled.get("education") or "").strip():
        edu = extract_education_requirement(
            filled.get("jd_text") or src.get("jd_text"),
            title=str(title_for_meta or "") or None,
        )
        if edu:
            filled["education"] = edu
    if not (filled.get("salary_range") or "").strip():
        sal = normalize_salary_range(
            str((filled_fields or {}).get("salary_range") or "")
        )
        if sal:
            filled["salary_range"] = sal
    if not (filled.get("headcount") or "").strip():
        hc = normalize_headcount(str((filled_fields or {}).get("headcount") or ""))
        if hc:
            filled["headcount"] = hc

    # 链接优先级：申请链优先，无申请链则申请/原文同为列表/校招页
    apply_n, source_n = normalize_job_link_fields(
        apply_or_detail=(filled.get("apply_url") or "").strip() or None,
        list_or_campus=(filled.get("source_url") or "").strip() or None,
        page_url=(
            (filled.get("source_url") or "").strip()
            or str(src.get("source_url") or "").strip()
            or None
        ),
    )
    if apply_n:
        filled["apply_url"] = apply_n
    if source_n:
        filled["source_url"] = source_n

    merged = dict(src)
    for key in FILLABLE_KEYS:
        val = (filled.get(key) or "").strip()
        if key in ("title", "source_url"):
            if val:
                merged[key] = val
        elif key == "group_name":
            merged[key] = val or None
        elif val:
            merged[key] = val
    # 岗位发布时间：FILLABLE 已含 open_at；仍回落 list/published 别名
    page_date = (
        str(merged.get("open_at") or "").strip()
        or str((filled_fields or {}).get("open_at") or "").strip()
        or str(filled.get("open_at") or "").strip()
        or str((filled_fields or {}).get("list_updated_at") or "").strip()
        or str((filled_fields or {}).get("published_at") or "").strip()
    )
    if page_date:
        merged["open_at"] = page_date
    filled_co = (filled.get("company") or "").strip()
    if filled_co and filled_co != "未知企业":
        merged["company"] = filled_co
    elif str(src.get("company") or "").strip():
        merged["company"] = str(src.get("company")).strip()
    elif filled_co:
        merged["company"] = filled_co
    return merged


def reidentify_job_fields(
    job: dict[str, Any],
    *,
    html: str | None = None,
    fetch: bool = True,
    candidate_index: int | None = None,
    ocr_enabled: bool | None = None,
    seed_company_name: str | None = None,
    discover_portal: bool = False,
    list_collect_months: int | None = None,
    list_limit: int = 40,
    timeout: float = 45.0,
) -> tuple[str, dict[str, Any] | None, str, FillFromUrlResult]:
    """
    按岗位链接重新识别并合并字段（供「重新识别」批量/单条）。

    返回 (action, merged_job|None, message, resolve_result)。
    action: "update" | "delete" | "fail"
      - update：合并后的岗位 dict
      - delete：页面无招聘信息（调用方应 soft_delete）
      - fail：网络/链接等错误（不删）
    多候选时：candidate_index 优先；否则 match_fill_candidate 自动对齐。
    默认 fetch=True 会真实联网抓取；仅单测可传 html + fetch=False。
    seed_company_name：集团门户种子名（companies.name），勿传子公司名。
    discover_portal：展开校招+实习近半年列表（批量无交互时仍只自动对齐一条）。
    """
    src = dict(job or {})
    url = pick_fill_url(src.get("apply_url"), src.get("source_url"))
    if not url:
        empty = FillFromUrlResult(ok=False, error="无可用网申/原文链接")
        return "fail", None, "无可用网申/原文链接", empty
    if not (url.startswith("http://") or url.startswith("https://")):
        empty = FillFromUrlResult(ok=False, error="链接格式无效", page_url=url)
        return "fail", None, f"链接格式无效：{url}", empty

    seed = (
        (seed_company_name or "").strip()
        or str(src.get("group_name") or "").strip()
        or None
    )
    # 无集团种子时用现公司名保底，避免表单被填成「未知企业」
    keep_for_parse = seed or str(src.get("company") or "").strip() or None
    # UI 路径必须联网：fetch=True 时忽略预置 html，避免走离线空壳
    use_html = None if fetch else html
    if discover_portal:
        result = discover_portal_jobs_from_url(
            url,
            html=use_html,
            fetch=fetch,
            keep_company=keep_for_parse,
            ocr_enabled=ocr_enabled,
            list_collect_months=list_collect_months,
            list_limit=max(
                int(list_limit or DEFAULT_PER_COMPANY_COLLECT_BATCH),
                1,
            ),
            timeout=max(float(timeout), 1.0),
        )
    else:
        result = resolve_jobs_from_url(
            url,
            html=use_html,
            fetch=fetch,
            keep_company=keep_for_parse,
            ocr_enabled=ocr_enabled,
            discover_portal=False,
            list_collect_months=list_collect_months,
            list_limit=list_limit,
            timeout=max(float(timeout), 1.0),
        )
    if not result.ok or not result.candidates:
        err = result.error or "未能识别岗位信息"
        if resolve_result_is_no_job_posting(result):
            result.no_job_posting = True
            return "delete", None, "页面无招聘信息", result
        return "fail", None, err, result

    if candidate_index is not None:
        if candidate_index < 0 or candidate_index >= len(result.candidates):
            return "fail", None, "所选岗位索引无效", result
        chosen = result.candidates[candidate_index]
    else:
        chosen = find_matching_fill_candidate(
            result.candidates,
            title=src.get("title"),
            source_url=src.get("source_url"),
            apply_url=src.get("apply_url"),
        )
        if chosen is None:
            original_url = pick_fill_url(src.get("apply_url"), src.get("source_url"))
            chosen = _moka_direct_candidate_from_context(result.candidates, original_url)
        if chosen is None:
            # Listing pages without a stable detail URL may still use the first
            # candidate; detail URLs must never silently fall back to another job.
            chosen = match_fill_candidate(
                result.candidates,
                title=src.get("title"),
                source_url=src.get("source_url"),
                apply_url=src.get("apply_url"),
            )
    if chosen is None:
        return "fail", None, "未能匹配识别结果", result

    try:
        enriched = enrich_candidate_detail(
            chosen,
            page_url=result.page_url or url,
            keep_company=keep_for_parse,
            ocr_enabled=ocr_enabled,
            timeout=max(float(timeout), 1.0),
        )
    except Exception:
        enriched = chosen

    fields = enriched.fields or {}
    if fields_look_like_no_job_posting(fields):
        result.no_job_posting = True
        return "delete", None, "页面无招聘信息", result

    merged = apply_reidentify_fields(src, fields, seed_company_name=seed)
    changes = summarize_reidentify_changes(src, merged)
    hint = ""
    if result.total > 1:
        hint = f"（列表共 {result.total} 个，已自动对齐）"
    if changes:
        detail = "；".join(changes[:6])
        if len(changes) > 6:
            detail += f" 等共 {len(changes)} 项"
        return "update", merged, f"重新识别成功{hint}：已更新 {detail}", result
    return "update", merged, f"重新识别完成{hint}：字段与原值一致，无实质变更", result
