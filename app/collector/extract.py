"""HTML/文本字段抽取与置信度判定。"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.collector.filters import (
    extract_job_tags,
    is_aggregator_text,
    is_company_plus_recruit_title,
    is_job_posting,
    is_noise_title,
    map_recruit_bucket,
    title_company_mismatch,
)


def soup_from_html(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "lxml")


def extract_og_title(soup: BeautifulSoup) -> str | None:
    tag = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "og:title"})
    if tag and tag.get("content"):
        return str(tag["content"]).strip()
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(" ", strip=True)
    return None


def extract_longest_text_block(soup: BeautifulSoup, min_len: int = 80) -> str | None:
    best = ""
    for el in soup.find_all(["article", "div", "section", "main", "p"]):
        text = el.get_text("\n", strip=True)
        if len(text) > len(best):
            best = text
    if len(best) >= min_len:
        return best[:20000]
    return best or None


def find_embedded_json(html: str) -> list[dict[str, Any]]:
    """尝试抽取页面内嵌 JSON 对象。"""
    blobs: list[dict[str, Any]] = []
    patterns = [
        r"<script[^>]*id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>",
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});",
        r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});",
    ]
    for pat in patterns:
        for m in re.finditer(pat, html or "", re.DOTALL | re.IGNORECASE):
            raw = m.group(1).strip()
            try:
                blobs.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return blobs


def dig_title_jd(obj: Any, depth: int = 0) -> tuple[str | None, str | None]:
    if depth > 6 or obj is None:
        return None, None
    title = None
    jd = None
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower()
            if title is None and lk in ("title", "name", "jobtitle", "job_name", "postname") and isinstance(v, str):
                if 2 <= len(v) <= 200:
                    title = v.strip()
            if jd is None and lk in (
                "description",
                "jobdescription",
                "job_description",
                "jd",
                "detail",
                "content",
            ) and isinstance(v, str) and len(v) > 40:
                jd = v.strip()
            t2, d2 = dig_title_jd(v, depth + 1)
            title = title or t2
            jd = jd or d2
            if title and jd:
                return title, jd
    elif isinstance(obj, list):
        for item in obj[:50]:
            t2, d2 = dig_title_jd(item, depth + 1)
            title = title or t2
            jd = jd or d2
            if title and jd:
                return title, jd
    return title, jd


def classify_from_text(title: str | None, body: str | None = None) -> tuple[str | None, str | None, list[str]]:
    recruit_project = None
    text = f"{title or ''} {body or ''}"
    for kw in (
        "日常实习",
        "暑假实习",
        "寒假实习",
        "暑期实习",
        "社会招聘",  # 先于校园招聘，避免笼统「招聘」语境误伤；标签字段路径另有覆盖
        "秋招",
        "春招",
        "校园招聘",
        "正式批",
        "提前批",
    ):
        if kw in text:
            recruit_project = kw
            break
    bucket = map_recruit_bucket(recruit_project, title)
    tags = extract_job_tags(title)
    return recruit_project, bucket, tags


def compute_confidence(
    *,
    title: str | None,
    source_url: str | None,
    jd_text: str | None,
    recruit_bucket: str | None,
    official_source: bool,
    aggregator: bool,
    company: str | None = None,
) -> float:
    if aggregator:
        return 0.1
    if is_noise_title(title):
        return 0.05
    if company and title_company_mismatch(company, title):
        return 0.15
    score = 0.0
    if title:
        score += 0.35
    if source_url and source_url.startswith("http"):
        score += 0.25
    if recruit_bucket:
        score += 0.15
    if jd_text and len(jd_text) > 40:
        score += 0.15
    if official_source:
        score += 0.2
    return min(score, 1.0)


def should_auto_publish(
    *,
    verify_status: str,
    title: str | None,
    source_url: str | None,
    recruit_project: str | None,
    confidence: float,
    aggregator: bool,
    min_confidence: float = 0.55,
    trusted_ats: bool = False,
    company: str | None = None,
    jd_text: str | None = None,
    require_job_posting: bool = False,
) -> tuple[bool, str]:
    """
    自动发布判定。
    异常口径以「最新发布时间是否超过约3个月」为主（见 classify_freshness），
    默认不再因「未列出具体岗位名」拒绝发布。
    仍拦截：汇总号、关停/内推噪声、标题与公司严重不符。
    """
    if aggregator:
        return False, "命中汇总黑名单"
    # 仅硬噪声（导航/关停/内推）；门户校招入口可作正常信息发布
    from app.collector.filters import is_closed_or_referral_title, is_closed_or_referral_jd

    if is_closed_or_referral_title(title) or is_closed_or_referral_jd(jd_text):
        return False, "关停或内部推荐页，不发布"
    if is_noise_title(title) and not is_company_plus_recruit_title(company, title):
        # 登录/个人中心等导航噪声仍拒绝；公司名+校园招聘允许
        return False, "噪声标题（导航/登录等）"
    if company and title_company_mismatch(company, title):
        return False, "标题公司名与种子严重不符"
    if require_job_posting:
        ok_job, job_conf, job_reason = is_job_posting(
            title=title,
            source_url=source_url,
            jd_text=jd_text,
            company=company,
        )
        if not ok_job:
            return False, job_reason
        confidence = min(confidence, 1.0) if confidence else job_conf
        if job_conf < 0.55:
            return False, f"岗位置信不足 ({job_conf:.2f})"
    # official 或可信 ATS 网申均可自动发布（避免种子永远卡在源验证）
    if verify_status != "official" and not trusted_ats:
        return False, "源未验证为官方"
    if not title or not source_url or not recruit_project:
        return False, "缺少硬字段（单位侧标题/原文/招聘项目）"
    if confidence < min_confidence:
        return False, f"置信度不足 ({confidence:.2f})"
    return True, "ok"


def is_source_trust_reject(reason: str | None) -> bool:
    """是否属于「官网/源信任」问题（应进源验证，而非异常队列）。"""
    r = reason or ""
    return "源未验证" in r


def is_publish_noise_skip(reason: str | None) -> bool:
    """噪声/关停类：直接跳过，不进任一人工队列。"""
    r = reason or ""
    return any(k in r for k in ("关停", "内部推荐", "噪声标题"))


def review_kind_for_publish_reason(reason: str | None) -> str:
    """岗位内容/规则失败 → 异常队列 kind。"""
    r = reason or ""
    if "置信度" in r or "岗位置信" in r:
        return "low_confidence"
    if "汇总" in r:
        return "aggregator"
    if "不符" in r:
        return "title_mismatch"
    if "硬字段" in r:
        return "missing_fields"
    return "needs_review"


def route_auto_publish_failure(
    db: Any,
    company: dict[str, Any],
    job: dict[str, Any],
    reason: str,
    stats: dict[str, int],
) -> None:
    """
    自动发布失败分流：
    - 源未验证为官方 → 源验证（source_verify_queue）
    - 关停/噪声 → 跳过
    - 解析/字段/规则/内容问题 → 异常队列（review_queue）
    """
    if is_source_trust_reject(reason):
        cid = company.get("id")
        url = str(job.get("source_url") or job.get("apply_url") or "").strip()
        if cid is not None:
            db.enqueue_source_verify(cid, "url", url, reason)
        stats["skipped"] = stats.get("skipped", 0) + 1
        return
    if is_publish_noise_skip(reason):
        stats["skipped"] = stats.get("skipped", 0) + 1
        return
    db.enqueue_review(review_kind_for_publish_reason(reason), job, reason)
    stats["queued"] = stats.get("queued", 0) + 1


def host_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def text_is_aggregator(title: str | None, body: str | None, patterns: list[str] | None = None) -> bool:
    return is_aggregator_text(f"{title or ''}\n{body or ''}", patterns)
