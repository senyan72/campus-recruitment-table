"""微信公众号文章适配器。"""

from __future__ import annotations

import re

from app.collector.adapters.base import ParseResult
from app.collector.extract import classify_from_text, soup_from_html


def can_handle(url: str) -> bool:
    return "mp.weixin.qq.com" in (url or "").lower()


def parse_wechat(url: str, html: str) -> ParseResult:
    soup = soup_from_html(html)
    title = None
    meta = soup.find("meta", property="og:title")
    if meta and meta.get("content"):
        title = str(meta["content"]).strip()
    if not title:
        t = soup.find("h1", id="activity-name") or soup.find("h1")
        if t:
            title = t.get_text(" ", strip=True)

    content = soup.find(id="js_content") or soup.find("div", class_=re.compile("rich_media"))
    jd = content.get_text("\n", strip=True) if content else None
    if jd and len(jd) > 20000:
        jd = jd[:20000]

    project, bucket, tags = classify_from_text(title, jd)
    conf = 0.65 if title and jd else 0.4
    return ParseResult(
        title=title,
        jd_text=jd,
        recruit_project=project or ("公众号公告" if title else None),
        recruit_bucket=bucket,
        job_tags=tags,
        parse_status="ok" if title else "needs_browser",
        confidence=conf,
        apply_url=url,
        extras={"multi_job_hint": bool(jd and jd.count("岗位") >= 3)},
    )
