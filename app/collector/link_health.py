"""Small, bounded URL helpers used by the Viewer before opening an application link."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import httpx

_ZHIYE_UUID_RE = re.compile(
    r"^/(?P<channel>campus|intern|social)/jobs/(?P<uuid>[0-9a-f-]{16,})/?$", re.I
)


def resolve_zhiye_apply_url(url: str, *, timeout: float = 6.0) -> str:
    """Upgrade an old UUID route to the current numeric JobAdId route.

    The endpoint is queried only for legacy links and is deliberately bounded;
    failures return the original URL so opening a job never becomes blocked by
    a transient network issue.
    """
    parsed = urlparse((url or "").strip())
    if "zhiye.com" not in parsed.netloc.lower():
        return url
    match = _ZHIYE_UUID_RE.match(parsed.path or "")
    if not match:
        return url
    origin = f"{parsed.scheme}://{parsed.netloc}"
    api = f"{origin}/api/JobAd/GetJobAdPageList"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": origin,
        "Referer": f"{origin}/{match.group('channel')}/jobs",
    }
    try:
        with httpx.Client(headers=headers, timeout=timeout) as client:
            for page in range(4):
                response = client.post(
                    api,
                    json={"PageIndex": page, "PageSize": 50, "KeyWords": "", "SpecialType": 0},
                )
                response.raise_for_status()
                data: Any = response.json()
                for row in data.get("Data") or []:
                    if str(row.get("Id") or "").lower() == match.group("uuid").lower():
                        job_ad_id = str(row.get("JobAdId") or "").strip()
                        if job_ad_id.isdigit():
                            return f"{origin}/{match.group('channel')}/detail?jobAdId={job_ad_id}"
                rows = data.get("Data") or []
                if len(rows) < 50:
                    break
    except Exception:  # noqa: BLE001
        return url
    return url


def looks_like_legacy_zhiye_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    return "zhiye.com" in parsed.netloc.lower() and bool(_ZHIYE_UUID_RE.match(parsed.path or ""))
