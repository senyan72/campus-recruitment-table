"""按域名路由到岗位 URL 适配器。"""

from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

import httpx

from app.collector.adapters import feishu, hotjob, moka, wechat, zhiye
from app.collector.adapters.base import ParseResult
from app.collector.adapters.generic import parse_generic

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}


class AccessRestrictedError(RuntimeError):
    """The site explicitly denied automated access; collection must stop for it."""


_ACCESS_RESTRICTED_STATUS_CODES = frozenset({401, 403, 405, 429, 503})
_ACCESS_RESTRICTED_MARKERS = (
    "captcha",
    "verify you are human",
    "access denied",
    "bot detection",
    "security verification",
    "安全验证",
    "访问受限",
    "访问被拒绝",
    "请求过于频繁",
    "验证码",
    "人机验证",
    "反爬",
)
_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_AT: dict[str, float] = {}
_DEFAULT_MIN_HOST_INTERVAL_SECONDS = 1.5


def _min_host_interval_seconds() -> float:
    try:
        from app.config import load_config

        return max(0.0, float(load_config().get("collector_min_host_interval_seconds", 1.5)))
    except (TypeError, ValueError, OSError):
        return _DEFAULT_MIN_HOST_INTERVAL_SECONDS


def _wait_for_host(url: str) -> None:
    """Keep requests to one host deliberately sparse and sequential."""
    host = (urlparse(url).netloc or "").lower()
    if not host:
        return
    with _REQUEST_LOCK:
        now = time.monotonic()
        previous = _LAST_REQUEST_AT.get(host, 0.0)
        delay = _min_host_interval_seconds() - (now - previous)
        if delay > 0:
            time.sleep(delay)
        _LAST_REQUEST_AT[host] = time.monotonic()


def access_restricted_reason(status_code: int | None, text: str = "") -> str | None:
    """Return a user-facing reason only for clear access-control signals."""
    if status_code in _ACCESS_RESTRICTED_STATUS_CODES:
        return f"HTTP {status_code}"
    sample = (text or "")[:20_000].lower()
    if any(marker in sample for marker in _ACCESS_RESTRICTED_MARKERS):
        return "页面包含安全验证或反爬提示"
    return None


def pick_parser(url: str):
    if moka.can_handle(url):
        return moka.parse_moka, "moka"
    if zhiye.can_handle(url):
        return zhiye.parse_zhiye, "zhiye"
    if feishu.can_handle(url):
        return feishu.parse_feishu, "feishu"
    if hotjob.can_handle(url):
        return hotjob.parse_hotjob, "hotjob"
    if wechat.can_handle(url):
        return wechat.parse_wechat, "wechat"
    return parse_generic, "generic"


def _ocr_enabled_from_config() -> bool:
    try:
        from app.config import load_config

        return bool(load_config().get("ocr_enabled"))
    except Exception:
        return False


def parse_html(url: str, html: str, *, ocr_enabled: bool | None = None) -> ParseResult:
    """适配器解析 + 标签字段抽取（可选 OCR 兜底）。"""
    from app.collector.label_fields import enrich_with_label_extraction

    parser, name = pick_parser(url)
    # 自定义域名重定向到 Moka 门户时，URL 可能尚无 /campus-recruitment/ 路径
    if name == "generic" and html and moka.load_init_data(html):
        parser, name = moka.parse_moka, "moka"
    result = parser(url, html)
    result.extras["adapter"] = name
    enabled = _ocr_enabled_from_config() if ocr_enabled is None else bool(ocr_enabled)
    return enrich_with_label_extraction(
        result,
        html,
        ocr_enabled=enabled,
        source_url=url,
    )


def fetch_html(url: str, timeout: float = 20.0) -> str:
    """Fetch HTML conservatively and stop on explicit access restrictions."""
    candidates = hotjob.page_url_candidates(url) if hotjob.can_handle(url) else [url]
    last_exc: Exception | None = None
    with httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=timeout) as client:
        for cand in candidates:
            try:
                _wait_for_host(cand)
                resp = client.get(cand)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                continue
            text = resp.text or ""
            restricted = access_restricted_reason(resp.status_code, text)
            if restricted:
                raise AccessRestrictedError(f"疑似反爬/访问受限（{restricted}）：{cand}")
            if hotjob.can_handle(cand) and hotjob.is_waf_block_page(
                text, status_code=resp.status_code
            ):
                raise AccessRestrictedError(f"疑似反爬/访问受限（WAF 拦截）：{cand}")
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


def parse_job_url(
    url: str,
    *,
    html: str | None = None,
    fetch: bool = True,
    ocr_enabled: bool | None = None,
) -> ParseResult:
    """解析岗位/公告 URL → 结构化字段。"""
    url = (url or "").strip()
    if not url:
        return ParseResult(parse_status="error", confidence=0.0)
    try:
        content = html if html is not None else (fetch_html(url) if fetch else "")
    except Exception as exc:  # noqa: BLE001 — 采集容错
        return ParseResult(
            parse_status="needs_browser",
            confidence=0.0,
            apply_url=url,
            extras={"error": str(exc)},
        )
    if not content:
        return ParseResult(parse_status="needs_browser", confidence=0.0, apply_url=url)
    return parse_html(url, content, ocr_enabled=ocr_enabled)
