"""官网招聘页采集：列表识别职位链接 → 详情抽取四字段。"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.collector.adapters import feishu as feishu_adapter
from app.collector.adapters import moka as moka_adapter
from app.collector.adapters import zhiye as zhiye_adapter
from app.collector.adapters.base import ParseResult
from app.collector.adapters.generic import enumerate_job_table_rows
from app.collector.adapters.router import parse_html
from app.collector.extract import (
    compute_confidence,
    route_auto_publish_failure,
    should_auto_publish,
    text_is_aggregator,
)
from app.collector.filters import (
    DEFAULT_LOOKBACK_DAYS,
    classify_freshness,
    is_closed_or_referral_jd,
    is_closed_or_referral_title,
    is_job_detail_link,
    is_noise_location,
    is_noise_nav_url,
    is_noise_title,
    is_portal_shell_record,
    is_social_hiring,
    is_trusted_ats_url,
    map_recruit_bucket,
    parse_url_list,
    portal_entry_title,
    prefer_apply_urls,
    is_stale_grad_batch,
    is_target_campus_or_intern,
    prefer_current_grad_batch,
    recover_location_from_jd,
    recover_title_from_jd,
    resolve_grad_batch,
    resolve_list_lookback_months,
    sanitize_job_title,
    title_company_mismatch,
)
from app.db.local import LocalDB

ProgressCb = Callable[[str], None]


def fetch(url: str, timeout: float = 20.0) -> str:
    # All webpage requests go through the conservative shared fetch policy.
    from app.collector.adapters.router import fetch_html as _router_fetch_html

    return _router_fetch_html(url, timeout=timeout)


def _enqueue_access_restricted(
    db: LocalDB,
    company: dict[str, Any],
    url: str,
    exc: BaseException,
) -> None:
    """Record the compliant skip in the existing exception queue."""
    db.enqueue_review(
        "access_restricted",
        {
            "company_id": company["id"],
            "company": company.get("name") or "",
            "url": url,
            "error": str(exc),
        },
        "疑似反爬/访问受限，已跳过该企业",
    )


def extract_job_links(base_url: str, html: str, limit: int = 40) -> list[str]:
    """只抽取像职位/公告详情的链接。"""
    soup = BeautifulSoup(html or "", "lxml")
    links: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ", strip=True)
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        full = urljoin(base_url, href)
        if urlparse(full).scheme not in ("http", "https"):
            continue
        if full in seen:
            continue
        if is_noise_nav_url(full, text):
            continue
        if not is_job_detail_link(full, text):
            continue
        seen.add(full)
        links.append(full)
        if len(links) >= limit:
            break
    return links


def company_career_targets(company: dict[str, Any]) -> list[str]:
    """合并 career_urls + hint_apply_urls，优先 ATS/详情页。"""
    careers = parse_url_list(company.get("career_urls"))
    hints = parse_url_list(company.get("hint_apply_urls"))
    merged = prefer_apply_urls(careers + hints, limit=12)
    ats = [u for u in merged if is_trusted_ats_url(u)]
    if ats:
        return prefer_apply_urls(ats, limit=8)
    return [u for u in merged if "mp.weixin.qq.com" not in u.lower()][:8] or merged[:8]


def _empty_stats() -> dict[str, int]:
    return {
        "parsed": 0,
        "published": 0,
        "updated": 0,
        "unchanged": 0,
        "queued": 0,
        "errors": 0,
        "access_restricted": 0,
        "skipped": 0,
        "stale": 0,
        # 本轮新处理的未入库岗（受 per_company_collect_batch 限制）
        "batch_new": 0,
        # 列表/详情枚举时已入库跳过（便于深度采集快速换下一企）
        "skipped_known": 0,
        # 本轮列表枚举到的岗位总数（供深度采集进度展示）
        "enum_total": 0,
    }


def _company_collect_budget(max_new_jobs: int | None = None) -> int:
    """单企业本轮最多新采岗位数；0 表示不限制。"""
    if max_new_jobs is not None:
        return max(0, int(max_new_jobs))
    from app.collector.fill_from_url import per_company_batch_size
    from app.config import load_config

    return per_company_batch_size(load_config())


def _publish_or_queue(
    db: LocalDB,
    company: dict[str, Any],
    job: dict[str, Any],
    *,
    trusted_url: bool,
    aggregator: bool,
    stats: dict[str, int],
    require_real_jobs: bool = False,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> None:
    """
    发布或进队列。
    正常：日常实习，或校招且届别=目标届（公历年+1）。
    异常：过旧届 / 社招 / 届别不符 → wrong_grad_batch / not_target_hiring；
    补充：标题裸年份非目标届、发布时间早于目标届招聘季窗口 → not_current_campus；
    另有超 3 个月未更新 → stale_over_3m。以上规则并存叠加。
    """
    # 关停 / 内推：直接丢弃，不进异常
    if is_closed_or_referral_title(job.get("title")) or is_closed_or_referral_jd(job.get("jd_text")):
        stats["skipped"] += 1
        return
    if is_noise_title(job.get("title")) and not (
        company.get("name") and job.get("title") and company.get("name") in (job.get("title") or "")
    ):
        # 登录/个人中心等硬噪声跳过；公司名+校园招聘允许往下走
        from app.collector.filters import is_company_plus_recruit_title

        if not is_company_plus_recruit_title(company.get("name"), job.get("title")):
            stats["skipped"] += 1
            return

    # 正常 vs 异常：实习 或 目标应届校招（含非当年校招补充信号）
    ok_target, kind, target_reason = is_target_campus_or_intern(
        title=job.get("title"),
        jd_text=job.get("jd_text"),
        recruit_bucket=job.get("recruit_bucket"),
        recruit_project=job.get("recruit_project"),
        graduation_batch=job.get("graduation_batch"),
        open_at=job.get("open_at"),
        published_at=job.get("published_at"),
    )
    if not ok_target:
        db.enqueue_review(kind or "not_target_hiring", job, target_reason)
        stats["queued"] += 1
        return

    status, freshness_reason = classify_freshness(
        deadline=job.get("deadline"),
        published_at=job.get("open_at") or job.get("published_at"),
        open_at=job.get("open_at"),
        updated_hint=job.get("updated_at"),
        title=job.get("title"),
        jd_text=job.get("jd_text"),
        recruit_project=job.get("recruit_project"),
        recruit_bucket=job.get("recruit_bucket"),
        lookback_days=lookback_days,
    )
    if status == "deadline_passed":
        stats["stale"] += 1
        stats["skipped"] += 1
        return
    if status == "stale_anomaly":
        db.enqueue_review("stale_over_3m", job, freshness_reason)
        stats["queued"] += 1
        stats["stale"] += 1
        return

    ok, reason = should_auto_publish(
        verify_status=company.get("verify_status") or "unverified",
        title=job.get("title"),
        source_url=job.get("source_url"),
        recruit_project=job.get("recruit_project"),
        confidence=float(job.get("confidence") or 0),
        aggregator=aggregator,
        trusted_ats=trusted_url,
        company=company.get("name"),
        jd_text=job.get("jd_text"),
        require_job_posting=require_real_jobs,
    )
    if ok:
        _jid, action = db.upsert_job_with_action(job)
        if action == "unchanged":
            stats["unchanged"] = stats.get("unchanged", 0) + 1
        elif action == "updated":
            stats["updated"] = stats.get("updated", 0) + 1
            stats["published"] += 1
        else:
            stats["published"] += 1
    else:
        # 源未验证 → 源验证；岗位内容/规则问题 → 异常队列；噪声 → 跳过
        route_auto_publish_failure(db, company, job, reason, stats)


def _result_to_job(
    company: dict[str, Any],
    *,
    url: str,
    title: str,
    result: Any,
    conf: float,
    project: str,
    bucket: str,
) -> dict[str, Any]:
    extras = getattr(result, "extras", None)
    extras_dict = extras if isinstance(extras, dict) else {}
    existing_batch = extras_dict.get("graduation_batch")
    batch = resolve_grad_batch(
        title=title,
        jd_text=getattr(result, "jd_text", None),
        existing=existing_batch if isinstance(existing_batch, str) else None,
    )
    # 门户详情「公司名称」：与种子不同 → 种子为集团、页面名为子公司
    from app.collector.filters import resolve_group_and_company

    page_company = str(extras_dict.get("company") or "").strip()
    group_name, hiring_company = resolve_group_and_company(
        seed_company_name=str(company.get("name") or "") or None,
        page_company=page_company or None,
    )
    return {
        "company_id": company["id"],
        "group_name": group_name or None,
        "company": hiring_company,
        "company_nature": company.get("company_nature"),
        "industry": company.get("industry"),
        "title": title,
        "source_url": url,
        "apply_url": getattr(result, "apply_url", None) or url,
        "recruit_project": project,
        "recruit_bucket": bucket,
        "work_location": getattr(result, "work_location", None),
        "deadline": getattr(result, "deadline", None),
        "open_at": (
            extras_dict.get("published_at")
            or extras_dict.get("list_updated_at")
            or extras_dict.get("open_at")
            or None
        ),
        "graduation_batch": batch,
        "education": getattr(result, "education", None)
        or extras_dict.get("education"),
        "salary_range": getattr(result, "salary_range", None)
        or extras_dict.get("salary_range"),
        "headcount": getattr(result, "headcount", None)
        or extras_dict.get("headcount"),
        "jd_text": getattr(result, "jd_text", None),
        "job_tags": getattr(result, "job_tags", None) or [],
        "raw_category": getattr(result, "raw_category", None),
        "parse_status": getattr(result, "parse_status", None) or "ok",
        "confidence": conf,
        # 新采岗位先进「岗位审核」；已审核 active 由 upsert 保底不被降级
        "status": "pending_review",
    }


def _merge_list_hint(result: Any, list_hint: ParseResult | None) -> Any:
    """详情壳标题时保留列表行已解析的岗位名/地点/类型。"""
    if list_hint is None:
        return result
    title = (getattr(result, "title", None) or "").strip()
    if is_noise_title(title) or not title:
        if list_hint.title and not is_noise_title(list_hint.title):
            result.title = list_hint.title
    if not getattr(result, "work_location", None) and list_hint.work_location:
        result.work_location = list_hint.work_location
    if not getattr(result, "recruit_project", None) and list_hint.recruit_project:
        result.recruit_project = list_hint.recruit_project
    if not getattr(result, "recruit_bucket", None) and list_hint.recruit_bucket:
        result.recruit_bucket = list_hint.recruit_bucket
    if not getattr(result, "raw_category", None) and list_hint.raw_category:
        result.raw_category = list_hint.raw_category
    if list_hint.job_tags and not getattr(result, "job_tags", None):
        result.job_tags = list_hint.job_tags
    hint_extras = list_hint.extras if isinstance(list_hint.extras, dict) else {}
    extras = result.extras if isinstance(result.extras, dict) else {}
    if hint_extras.get("company") and not extras.get("company"):
        extras["company"] = hint_extras["company"]
        result.extras = extras
    return result


def _handle_parsed_detail(
    db: LocalDB,
    company: dict[str, Any],
    *,
    url: str,
    result: Any,
    trusted: bool,
    block_patterns: list[str] | None,
    stats: dict[str, int],
    require_real_jobs: bool,
    lookback_days: int,
    list_hint: ParseResult | None = None,
) -> None:
    from app.collector.filters import is_company_plus_recruit_title

    company_name = company.get("name") or "未知企业"
    stats["parsed"] += 1
    result = _merge_list_hint(result, list_hint)
    page_title = (result.title or "").strip()
    jd_text = getattr(result, "jd_text", None)
    # 拒绝 Search Jobs / 招贤纳才 等 chrome；优先正文岗位码/标签
    raw_title = sanitize_job_title(page_title)
    if not raw_title or is_noise_title(page_title):
        recovered = sanitize_job_title(recover_title_from_jd(jd_text))
        if recovered:
            raw_title = recovered
    result.title = raw_title
    if page_title and is_noise_title(page_title) and jd_text and page_title not in (jd_text or "")[:240]:
        # 壳标题并入 JD，不占用岗位名称
        result.jd_text = f"{page_title}\n{jd_text}"
        jd_text = result.jd_text
    if not getattr(result, "work_location", None):
        loc = recover_location_from_jd(jd_text)
        if loc and not is_noise_location(loc):
            result.work_location = loc
    if is_noise_location(getattr(result, "work_location", None)):
        result.work_location = None

    if is_closed_or_referral_title(raw_title) or is_closed_or_referral_jd(jd_text):
        stats["skipped"] += 1
        return
    # 仅 chrome 标题且无法回填 → 进审核，勿发布壳标题
    if not raw_title or (
        is_noise_title(raw_title) and not is_company_plus_recruit_title(company_name, raw_title)
    ):
        if page_title and is_noise_title(page_title):
            db.enqueue_review(
                "chrome_title",
                {
                    "company_id": company["id"],
                    "company": company_name,
                    "title": "",
                    "source_url": url,
                    "apply_url": getattr(result, "apply_url", None) or url,
                    "jd_text": result.jd_text,
                    "work_location": result.work_location,
                    "confidence": 0.2,
                    "raw_page_title": page_title,
                },
                "仅解析到页面壳标题，岗位名称留空待审",
            )
            stats["queued"] += 1
        else:
            stats["skipped"] += 1
        return
    if title_company_mismatch(company_name, raw_title):
        db.enqueue_review(
            "company_mismatch",
            {
                "company_id": company["id"],
                "company": company_name,
                "title": raw_title,
                "source_url": url,
                "apply_url": getattr(result, "apply_url", None) or url,
                "jd_text": result.jd_text,
                "work_location": result.work_location,
                "confidence": 0.15,
            },
            "标题公司名与种子严重不符",
        )
        stats["queued"] += 1
        return

    title = raw_title
    if not title:
        stats["skipped"] += 1
        return

    # 社招不得默认落成校园招聘/校招
    if is_social_hiring(
        title=title,
        jd_text=result.jd_text,
        recruit_project=result.recruit_project,
    ):
        project = result.recruit_project or "社会招聘"
        bucket = result.recruit_bucket
        if bucket == "校招":
            bucket = None
    else:
        project = result.recruit_project or "校园招聘"
        bucket = result.recruit_bucket or map_recruit_bucket(project, title) or "校招"
    aggregator = text_is_aggregator(title, result.jd_text, block_patterns)
    conf = compute_confidence(
        title=title,
        source_url=url,
        jd_text=result.jd_text,
        recruit_bucket=bucket,
        official_source=trusted,
        aggregator=aggregator,
        company=company_name,
    )
    if title and result.jd_text and (result.work_location or project):
        conf = min(max(conf, 0.75), 1.0)
    elif is_company_plus_recruit_title(company_name, title):
        conf = max(conf, 0.6)
    # 目标届别加分；过旧届别降权（实习岗不降）
    if prefer_current_grad_batch(title, result.jd_text):
        conf = min(conf + 0.05, 1.0)
    job = _result_to_job(
        company, url=url, title=title, result=result, conf=conf, project=project, bucket=bucket
    )
    if is_stale_grad_batch(
        job.get("graduation_batch"),
        recruit_bucket=bucket,
        title=title,
    ):
        job["confidence"] = max(float(job.get("confidence") or 0) - 0.15, 0.2)
        note = f"届别偏旧（目标 {resolve_grad_batch(title=None, jd_text=None)}）"
        tags = list(job.get("job_tags") or [])
        if "届别偏旧" not in tags:
            tags.append("届别偏旧")
        job["job_tags"] = tags
        if not job.get("raw_category"):
            job["raw_category"] = note
    # 门户壳不再因「非具体岗位」跳过；新鲜度异常在 _publish_or_queue 处理
    _publish_or_queue(
        db,
        company,
        job,
        trusted_url=trusted,
        aggregator=aggregator,
        stats=stats,
        require_real_jobs=require_real_jobs,
        lookback_days=lookback_days,
    )


def _collect_from_list_posts(
    db: LocalDB,
    company: dict[str, Any],
    career_url: str,
    posts: list[ParseResult],
    *,
    html: str,
    trusted: bool,
    block_patterns: list[str] | None,
    require_real_jobs: bool,
    lookback_days: int,
    stats: dict[str, int],
    adapter_name: str = "generic",
    max_new_jobs: int | None = None,
    known_urls: set[str] | None = None,
    known_titles: set[str] | None = None,
) -> None:
    """逐条处理列表枚举结果：有详情链则进详情补 JD，否则用列表字段入库。

    max_new_jobs：本入口最多新处理未入库岗（公司级预算由调用方下发）；0=不采。
    known_urls/titles：公司级已入库键（调用方只查一次并传入，禁止在此反复 list_jobs）。
    """
    from app.collector.fill_from_url import (
        _norm_title_key,
        _norm_url_key,
        candidate_is_known,
    )

    if max_new_jobs is not None and int(max_new_jobs) <= 0:
        return
    batch_cap = _company_collect_budget(max_new_jobs)
    if batch_cap <= 0:
        return
    if known_urls is None or known_titles is None:
        co_name = (company.get("name") or "").strip()
        ku, kt = (
            db.job_identity_keys_for_company(co_name, company.get("id"))
            if co_name
            else (set(), set())
        )
        urls = known_urls if known_urls is not None else ku
        titles = known_titles if known_titles is not None else kt
    else:
        urls, titles = known_urls, known_titles
    handled_new = 0
    skipped_known = 0
    stats["enum_total"] = max(int(stats.get("enum_total", 0) or 0), len(posts))

    for post in posts:
        if handled_new >= batch_cap:
            break
        url = (post.apply_url or career_url).strip()
        uk = _norm_url_key(url)
        tk = _norm_title_key(post.title)
        if candidate_is_known(
            title=post.title,
            apply_url=url,
            source_url=career_url,
            known_urls=urls,
            known_titles=titles,
        ):
            skipped_known += 1
            continue
        synthetic = "__job=" in url
        needs_fetch = bool(post.extras.get("needs_fetch")) and not synthetic
        # 合成链指向列表页，无需再抓；有独立详情链则抓
        if not synthetic and url.rstrip("/") == career_url.rstrip("/"):
            needs_fetch = False
        result = post
        if needs_fetch:
            try:
                page = fetch(url)
                # 统一走 parse_html：适配器路由 + 标签抽取 / 可选 OCR
                result = parse_html(url, page)
                # 保留列表 apply_url（含 __job）以便多岗不去重冲突；真实详情链优先
                if result.apply_url and "__job=" not in (result.apply_url or ""):
                    pass
                elif post.apply_url:
                    result.apply_url = post.apply_url
            except Exception as exc:  # noqa: BLE001
                from app.collector.adapters.router import AccessRestrictedError

                if isinstance(exc, AccessRestrictedError):
                    _enqueue_access_restricted(db, company, url, exc)
                    stats["errors"] += 1
                    stats["access_restricted"] += 1
                    return
                # 详情失败仍可用列表行字段
                if post.title and not is_noise_title(post.title):
                    result = post
                else:
                    db.enqueue_review(
                        "parse_error",
                        {"company_id": company["id"], "url": url, "error": str(exc)},
                        f"{adapter_name} 详情拉取失败",
                    )
                    stats["errors"] += 1
                    continue

        # 入库 source_url：合成链用列表页；真实详情用详情 URL
        if synthetic:
            source_url = career_url
            if not result.apply_url:
                result.apply_url = url
        else:
            source_url = url
        before_pub = int(stats.get("published", 0) or 0)
        before_q = int(stats.get("queued", 0) or 0)
        _handle_parsed_detail(
            db,
            company,
            url=source_url,
            result=result,
            trusted=trusted,
            block_patterns=block_patterns,
            stats=stats,
            require_real_jobs=require_real_jobs,
            lookback_days=lookback_days,
            list_hint=post if post.title else None,
        )
        # 新岗：发布或进审核队列都占本批名额（跳过/更新不计）
        became_new = (
            int(stats.get("published", 0) or 0) > before_pub
            or int(stats.get("queued", 0) or 0) > before_q
        )
        if became_new:
            handled_new += 1
            stats["batch_new"] = int(stats.get("batch_new", 0) or 0) + 1
            if uk:
                urls.add(uk)
            if tk:
                titles.add(tk)
    if skipped_known:
        stats["skipped_known"] = int(stats.get("skipped_known", 0) or 0) + skipped_known


def _list_pass_fully_known(stats: dict[str, int], posts_scanned: int) -> bool:
    """本入口枚举到的岗已全部入库、且无新采。"""
    if posts_scanned <= 0:
        return False
    if int(stats.get("batch_new", 0) or 0) > 0:
        return False
    return int(stats.get("skipped_known", 0) or 0) >= posts_scanned


def collect_adapter_list(
    db: LocalDB,
    company: dict[str, Any],
    career_url: str,
    html: str,
    *,
    adapter_name: str,
    block_patterns: list[str] | None = None,
    require_real_jobs: bool = False,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    detail_limit: int | None = None,
    max_new_jobs: int | None = None,
    known_urls: set[str] | None = None,
    known_titles: set[str] | None = None,
) -> dict[str, int]:
    """飞书/Moka/智联：列表枚举 → 详情四字段。"""
    from app.collector.fill_from_url import scan_limit_for_batch

    stats = _empty_stats()
    company_name = company.get("name") or "未知企业"
    trusted = (company.get("verify_status") == "official") or is_trusted_ats_url(career_url)
    budget = _company_collect_budget(max_new_jobs)
    if max_new_jobs is not None and budget <= 0:
        return stats
    # 枚举略多于本批，便于跳过已入库后仍能凑满 50
    enum_limit = int(detail_limit) if detail_limit is not None else scan_limit_for_batch(
        budget if budget > 0 else 50
    )

    # 优先 HTML 职位表（电子工业出版社等自建/北森壳页）
    table_posts = enumerate_job_table_rows(career_url, html, limit=enum_limit)

    if adapter_name == "feishu":
        posts = feishu_adapter.enumerate_positions(
            career_url, html, max_pages=8, limit=enum_limit
        )
    elif adapter_name == "moka":
        posts = moka_adapter.enumerate_positions(
            career_url, html, limit=enum_limit, max_pages=20
        )
    elif adapter_name == "zhiye":
        posts = zhiye_adapter.enumerate_positions(career_url, html, limit=enum_limit)
    else:
        posts = []

    # 表格行有真实岗位名时优先；链接枚举作补充
    if table_posts:
        by_url = {(p.apply_url or "").split("?")[0]: p for p in posts if p.apply_url}
        merged: list[ParseResult] = []
        seen: set[str] = set()
        for tp in table_posts:
            key = (tp.title or "") + "|" + (tp.apply_url or "")
            seen.add(key)
            # 若表格行链能对应到 adapter 链接，标记需抓详情
            link = (tp.apply_url or "")
            if link and "__job=" not in link:
                tp.extras["needs_fetch"] = True
            merged.append(tp)
        for p in posts:
            key = (p.title or "") + "|" + (p.apply_url or "")
            if key in seen:
                continue
            # 已有表格覆盖的同链跳过
            base = (p.apply_url or "").split("?")[0]
            if base and base in by_url:
                continue
            merged.append(p)
            if len(merged) >= enum_limit:
                break
        posts = merged

    if not posts:
        db.enqueue_review(
            "portal_only",
            {
                "company_id": company["id"],
                "company": company_name,
                "title": portal_entry_title(company_name),
                "source_url": career_url,
                "apply_url": career_url,
                "recruit_project": "校园招聘",
                "recruit_bucket": "校招",
                "confidence": 0.35,
            },
            f"{adapter_name} 列表未枚举到具体岗位",
        )
        stats["queued"] += 1
        return stats

    _collect_from_list_posts(
        db,
        company,
        career_url,
        posts,
        html=html,
        trusted=trusted,
        block_patterns=block_patterns,
        require_real_jobs=require_real_jobs,
        lookback_days=lookback_days,
        stats=stats,
        adapter_name=adapter_name,
        max_new_jobs=budget if max_new_jobs is not None or budget > 0 else None,
        known_urls=known_urls,
        known_titles=known_titles,
    )
    return stats


def collect_from_career_page(
    db: LocalDB,
    company: dict[str, Any],
    career_url: str,
    *,
    block_patterns: list[str] | None = None,
    require_real_jobs: bool = False,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    list_collect_months: int | None = None,
    max_new_jobs: int | None = None,
    known_urls: set[str] | None = None,
    known_titles: set[str] | None = None,
    _channel_visited: set[str] | None = None,
) -> dict[str, int]:
    stats = _empty_stats()
    visited = _channel_visited if _channel_visited is not None else set()
    url_key = (career_url or "").split("?")[0].rstrip("/").lower()
    if url_key and url_key in visited:
        return stats
    if url_key:
        visited.add(url_key)
    list_months = resolve_list_lookback_months(list_collect_months)
    if known_urls is None or known_titles is None:
        co = (company.get("name") or "").strip()
        ku, kt = (
            db.job_identity_keys_for_company(co, company.get("id"))
            if co
            else (set(), set())
        )
        if known_urls is None:
            known_urls = ku
        if known_titles is None:
            known_titles = kt

    try:
        html = fetch(career_url)
    except Exception as exc:  # noqa: BLE001
        from app.collector.adapters.router import AccessRestrictedError

        if isinstance(exc, AccessRestrictedError):
            _enqueue_access_restricted(db, company, career_url, exc)
            stats["errors"] += 1
            stats["access_restricted"] += 1
            return stats
        db.enqueue_review(
            "collect_error",
            {"company_id": company["id"], "url": career_url, "error": str(exc)},
            "官网拉取失败",
        )
        stats["errors"] += 1
        return stats

    if text_is_aggregator(company.get("name"), html[:2000], block_patterns):
        db.enqueue_review(
            "aggregator",
            {"company_id": company["id"], "url": career_url},
            "疑似汇总页",
        )
        stats["queued"] += 1
        return stats

    from app.collector.fill_from_url import scan_limit_for_batch

    budget = _company_collect_budget(max_new_jobs)
    if max_new_jobs is not None and budget <= 0:
        return stats
    enum_limit = scan_limit_for_batch(budget if budget > 0 else 50)

    # 飞书 / Moka / 智联等 ATS：按适配器列表采，不展开五矿式顶栏频道
    if feishu_adapter.can_handle(career_url):
        return collect_adapter_list(
            db,
            company,
            career_url,
            html,
            adapter_name="feishu",
            block_patterns=block_patterns,
            require_real_jobs=require_real_jobs,
            lookback_days=lookback_days,
            max_new_jobs=budget,
            known_urls=known_urls,
            known_titles=known_titles,
        )
    if moka_adapter.can_handle(career_url):
        return collect_adapter_list(
            db,
            company,
            career_url,
            html,
            adapter_name="moka",
            block_patterns=block_patterns,
            require_real_jobs=require_real_jobs,
            lookback_days=lookback_days,
            max_new_jobs=budget,
            known_urls=known_urls,
            known_titles=known_titles,
        )
    if zhiye_adapter.can_handle(career_url):
        return collect_adapter_list(
            db,
            company,
            career_url,
            html,
            adapter_name="zhiye",
            block_patterns=block_patterns,
            require_real_jobs=require_real_jobs,
            lookback_days=lookback_days,
            max_new_jobs=budget,
            known_urls=known_urls,
            known_titles=known_titles,
        )

    from app.collector.filters import is_company_plus_recruit_title

    trusted_company = (company.get("verify_status") == "official") or is_trusted_ats_url(career_url)
    company_name = company.get("name") or "未知企业"

    # 1) 优先：列表 HTML 表格多行；其次卡片列表（五矿类门户，校园/实习可翻页）
    from app.collector.portal_nav import (
        detect_recruit_channel,
        enumerate_detail_link_jobs,
        enumerate_job_cards,
        enumerate_list_with_pagination,
        should_paginate_channel,
    )

    _ch, ch_project, ch_bucket = detect_recruit_channel(career_url, html)
    page_lim = min(40, enum_limit)
    # 与局部重采对齐：先探表格；校园/实习则按表翻页，否则卡片翻页
    probed_table = enumerate_job_table_rows(
        career_url,
        html,
        limit=page_lim,
        channel_project=ch_project,
        channel_bucket=ch_bucket,
    )
    table_posts: list = []
    if probed_table and should_paginate_channel(_ch):
        table_posts = enumerate_list_with_pagination(
            career_url,
            html,
            fetch_page=fetch,
            enumerate_page=lambda u, h: enumerate_job_table_rows(
                u,
                h,
                limit=page_lim,
                channel_project=ch_project,
                channel_bucket=ch_bucket,
            ),
            lookback_months=list_months,
            channel_project=ch_project,
            channel_bucket=ch_bucket,
            per_page_limit=page_lim,
            max_posts=enum_limit,
            known_urls=known_urls,
            known_titles=known_titles,
        )
    elif probed_table:
        table_posts = probed_table
    if not table_posts:
        # 五矿类卡片优先；无卡片时再枚举 Apple /details/（避免整页 chrome 一条）
        if should_paginate_channel(_ch):
            table_posts = enumerate_list_with_pagination(
                career_url,
                html,
                fetch_page=fetch,
                lookback_months=list_months,
                channel_project=ch_project,
                channel_bucket=ch_bucket,
                per_page_limit=page_lim,
                max_posts=enum_limit,
                known_urls=known_urls,
                known_titles=known_titles,
            )
        else:
            table_posts = enumerate_job_cards(
                career_url,
                html,
                limit=page_lim,
                channel_project=ch_project,
                channel_bucket=ch_bucket,
            )
        if not table_posts:
            table_posts = enumerate_detail_link_jobs(
                career_url,
                html,
                limit=page_lim,
                channel_project=ch_project,
                channel_bucket=ch_bucket,
            )
    elif ch_project:
        for p in table_posts:
            if ch_project == "社会招聘":
                p.recruit_project = "社会招聘"
                if p.recruit_bucket == "校招":
                    p.recruit_bucket = None
            elif not p.recruit_project:
                p.recruit_project = ch_project
                p.recruit_bucket = p.recruit_bucket or ch_bucket
    # hotjob SPA：HTML 表/卡/链皆空时，再补充 listPosition（不替代页面源码主路径）
    if not table_posts:
        from app.collector.adapters import hotjob as hotjob_adapter

        if hotjob_adapter.can_handle(career_url) and (
            hotjob_adapter.is_spa_list_shell(html) or hotjob_adapter.extract_tenant(career_url)
        ):
            table_posts = hotjob_adapter.enumerate_positions(
                career_url, html, max_pages=8, limit=enum_limit
            )

    list_ok = False
    if table_posts:
        _collect_from_list_posts(
            db,
            company,
            career_url,
            table_posts,
            html=html,
            trusted=trusted_company,
            block_patterns=block_patterns,
            require_real_jobs=require_real_jobs,
            lookback_days=lookback_days,
            stats=stats,
            adapter_name="generic",
            max_new_jobs=budget,
            known_urls=known_urls,
            known_titles=known_titles,
        )
        list_ok = stats["published"] > 0 or stats["queued"] > 0

    if _list_pass_fully_known(stats, len(table_posts)):
        return stats

    remain = budget - int(stats.get("batch_new", 0) or 0)
    if not list_ok and remain > 0:
        # 2) 通用：职位详情链接 → 逐条进详情
        detail_links = extract_job_links(career_url, html, limit=enum_limit)
        if detail_links:
            targets = prefer_apply_urls(detail_links, limit=min(25, remain))
        else:
            targets = [career_url]

        detail_count = 0

        for url in targets:
            if int(stats.get("batch_new", 0) or 0) >= budget:
                break
            try:
                page = fetch(url) if url != career_url else html
                result = parse_html(url, page)
            except Exception as exc:  # noqa: BLE001
                from app.collector.adapters.router import AccessRestrictedError

                if isinstance(exc, AccessRestrictedError):
                    _enqueue_access_restricted(db, company, url, exc)
                    stats["errors"] += 1
                    stats["access_restricted"] += 1
                    return stats
                db.enqueue_review(
                    "parse_error",
                    {"company_id": company["id"], "url": url, "error": str(exc)},
                    "解析失败",
                )
                stats["errors"] += 1
                continue

            raw_title = (result.title or "").strip()
            # 列表入口本身若无真实岗位则跳过，不写「公司名+校园招聘」
            if url == career_url and (
                is_noise_title(raw_title)
                or is_company_plus_recruit_title(company_name, raw_title)
                or not result.jd_text
                or is_portal_shell_record(title=raw_title, jd_text=result.jd_text, source_url=url)
            ):
                if detail_links:
                    continue
                db.enqueue_review(
                    "portal_only",
                    {
                        "company_id": company["id"],
                        "company": company_name,
                        "title": portal_entry_title(company_name),
                        "source_url": career_url,
                        "apply_url": career_url,
                        "recruit_project": "校园招聘",
                        "recruit_bucket": "校招",
                        "confidence": 0.35,
                    },
                    "仅解析到校招入口、无具体岗位",
                )
                stats["queued"] += 1
                continue

            before_pub = stats["published"]
            before_q = stats["queued"]
            _handle_parsed_detail(
                db,
                company,
                url=url,
                result=result,
                trusted=trusted_company or is_trusted_ats_url(url),
                block_patterns=block_patterns,
                stats=stats,
                require_real_jobs=require_real_jobs,
                lookback_days=lookback_days,
            )
            if stats["published"] > before_pub or stats["queued"] > before_q:
                detail_count += 1
                stats["batch_new"] = int(stats.get("batch_new", 0) or 0) + 1

        if detail_count == 0 and not detail_links and stats["queued"] == 0 and stats["published"] == 0:
            db.enqueue_review(
                "portal_only",
                {
                    "company_id": company["id"],
                    "company": company_name,
                    "title": portal_entry_title(company_name),
                    "source_url": career_url,
                    "apply_url": career_url,
                    "recruit_project": "校园招聘",
                    "recruit_bucket": "校招",
                    "confidence": 0.35,
                },
                "仅解析到校招入口、无具体岗位",
            )
            stats["queued"] += 1

    # 3) 同站展开「校园招聘 / 实习生招聘」频道（不含社招）；名额未满才继续
    remain = budget - int(stats.get("batch_new", 0) or 0)
    if remain <= 0 or _list_pass_fully_known(stats, len(table_posts)):
        return stats
    return _collect_sibling_portal_channels(
        db,
        company,
        career_url,
        html,
        stats=stats,
        block_patterns=block_patterns,
        require_real_jobs=require_real_jobs,
        lookback_days=lookback_days,
        list_collect_months=list_months,
        visited=visited,
        max_new_jobs=remain,
        known_urls=known_urls,
        known_titles=known_titles,
    )


def _collect_sibling_portal_channels(
    db: LocalDB,
    company: dict[str, Any],
    career_url: str,
    html: str,
    *,
    stats: dict[str, int],
    block_patterns: list[str] | None,
    require_real_jobs: bool,
    lookback_days: int,
    visited: set[str],
    list_collect_months: int | None = None,
    max_new_jobs: int | None = None,
    known_urls: set[str] | None = None,
    known_titles: set[str] | None = None,
) -> dict[str, int]:
    """从顶栏找到实习生招聘/校园招聘入口并继续采集。"""
    from app.collector.portal_nav import expand_career_channel_urls

    remain = _company_collect_budget(max_new_jobs)
    if max_new_jobs is not None and remain <= 0:
        return stats
    for other in expand_career_channel_urls(career_url, html, include=("campus", "intern")):
        if remain <= 0:
            break
        ok = (other or "").split("?")[0].rstrip("/").lower()
        if not ok or ok in visited:
            continue
        sub = collect_from_career_page(
            db,
            company,
            other,
            block_patterns=block_patterns,
            require_real_jobs=require_real_jobs,
            lookback_days=lookback_days,
            list_collect_months=list_collect_months,
            max_new_jobs=remain,
            known_urls=known_urls,
            known_titles=known_titles,
            _channel_visited=visited,
        )
        for k, v in sub.items():
            stats[k] = stats.get(k, 0) + v
        if int(sub.get("access_restricted", 0) or 0):
            return stats
        remain = max(0, remain - int(sub.get("batch_new", 0) or 0))
    return stats


def collect_company_careers(
    db: LocalDB,
    company: dict[str, Any],
    *,
    block_patterns: list[str] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    list_collect_months: int | None = None,
    max_career_urls: int = 3,
    require_real_jobs: bool = False,
    stop_when_published: bool = True,
    max_new_jobs: int | None = None,
    on_stats: Callable[[dict[str, int]], None] | None = None,
) -> dict[str, int]:
    """采集一家公司的 career/hint 入口；每个入口会展开校园/实习频道（不含社招）。

    单企业本轮最多新采 per_company_collect_batch（默认 50）个未入库岗；
    200 岗约需 4 轮深度/批量采集。stop_when_published=False 时仍受该批上限约束，
    但会在名额未满时继续扫其它入口（夜间复检）。
    """
    total = _empty_stats()
    targets = company_career_targets(company)
    if not targets:
        return total
    block = block_patterns if block_patterns is not None else db.list_blocklist()
    channel_visited: set[str] = set()
    list_months = resolve_list_lookback_months(list_collect_months)
    url_cap = len(targets) if max_career_urls <= 0 else max(1, max_career_urls)
    remain = _company_collect_budget(max_new_jobs)
    if max_new_jobs is not None and remain <= 0:
        return total
    # 每家公司只查一次已入库键，避免深度采集时反复扫全表卡死
    co_name = (company.get("name") or "").strip()
    known_urls, known_titles = db.job_identity_keys_for_company(
        co_name, company.get("id")
    )
    for career_url in targets[:url_cap]:
        if remain <= 0:
            break
        stats = collect_from_career_page(
            db,
            company,
            career_url,
            block_patterns=block,
            require_real_jobs=require_real_jobs,
            lookback_days=lookback_days,
            list_collect_months=list_months,
            max_new_jobs=remain,
            known_urls=known_urls,
            known_titles=known_titles,
            _channel_visited=channel_visited,
        )
        for k in total:
            total[k] = total.get(k, 0) + stats.get(k, 0)
        used = int(stats.get("batch_new", 0) or 0)
        remain = max(0, remain - used)
        if int(stats.get("access_restricted", 0) or 0):
            break
        # 快速模式：本批名额已用尽则停；未满则继续其它入口凑满 50
        if stop_when_published and remain <= 0:
            break
        # 本入口列表岗均已入库：勿再扫其它 career URL / 频道（深度采集换下一企）
        if stop_when_published and used == 0 and int(stats.get("skipped_known", 0) or 0) > 0:
            break
        if on_stats:
            on_stats(total)
    return total


def run_website_collect(
    db: LocalDB,
    *,
    limit_companies: int = 30,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    list_collect_months: int | None = None,
    require_real_jobs: bool = False,
    max_career_urls: int = 2,
    stop_when_published: bool = True,
    progress: ProgressCb | None = None,
) -> dict[str, int]:
    """
    官网采集（校园/实习频道；不含社招）。
    limit_companies<=0：全量带 career/hint URL 的公司（夜间复检）。
    深度分片队列请用 deep.run_deep_collect。
    """
    unlimited = not limit_companies or limit_companies <= 0
    if unlimited:
        companies = db.list_companies_with_urls(
            limit=0, prefer_ats=True, verify_status=None
        )
    else:
        official = db.list_companies_with_urls(
            limit=limit_companies, prefer_ats=True, verify_status="official"
        )
        companies = list(official)
        if len(companies) < limit_companies:
            have = {c["id"] for c in companies}
            extra = [
                c
                for c in db.list_companies_with_urls(
                    limit=limit_companies * 2, prefer_ats=True, verify_status=None
                )
                if c["id"] not in have
            ]
            companies.extend(extra[: max(0, limit_companies - len(companies))])

    block = db.list_blocklist()
    list_months = resolve_list_lookback_months(list_collect_months)
    total = _empty_stats()
    for i, company in enumerate(companies, 1):
        if progress:
            progress(f"采集官网 ({i}/{len(companies)}): {company['name']}")
        stats = collect_company_careers(
            db,
            company,
            block_patterns=block,
            lookback_days=lookback_days,
            list_collect_months=list_months,
            max_career_urls=max_career_urls,
            require_real_jobs=require_real_jobs,
            stop_when_published=stop_when_published,
        )
        for k in total:
            total[k] = total.get(k, 0) + stats.get(k, 0)
    return total
