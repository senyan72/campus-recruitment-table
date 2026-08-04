"""可选 OCR 兜底：依赖未安装时安全跳过。

安装（可选）::
    pip install \"campus-jobs[ocr]\"
    # 或: pip install rapidocr-onnxruntime onnxruntime
"""

from __future__ import annotations

import base64
import logging
import re
from typing import Any
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

_DATA_IMG_RE = re.compile(
    r"data:image/(png|jpeg|jpg|webp|bmp);base64,([A-Za-z0-9+/=\s]+)",
    re.IGNORECASE,
)
_IMG_SRC_RE = re.compile(
    r"<img[^>]+src=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)


def ocr_available() -> bool:
    try:
        from rapidocr_onnxruntime import RapidOCR  # noqa: F401

        return True
    except Exception:
        return False


def _get_engine() -> Any | None:
    try:
        from rapidocr_onnxruntime import RapidOCR

        return RapidOCR()
    except Exception as exc:  # noqa: BLE001
        logger.info("OCR 引擎不可用: %s", exc)
        return None


def ocr_image_bytes(data: bytes) -> str | None:
    """对单张图片字节做 OCR，返回拼接文本。"""
    if not data or len(data) < 80:
        return None
    engine = _get_engine()
    if engine is None:
        return None
    try:
        result, _ = engine(data)
    except Exception as exc:  # noqa: BLE001
        logger.debug("OCR 识别失败: %s", exc)
        return None
    if not result:
        return None
    lines: list[str] = []
    for item in result:
        # RapidOCR: [box, text, score]
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            text = item[1]
            if isinstance(text, str) and text.strip():
                lines.append(text.strip())
    return "\n".join(lines) if lines else None


def _collect_image_payloads(
    html: str,
    *,
    base_url: str | None = None,
    limit: int = 3,
    fetch_remote: bool = False,
) -> list[bytes]:
    """优先 data: URL；可选拉取少量远程图（默认关，避免重网络）。"""
    payloads: list[bytes] = []
    for m in _DATA_IMG_RE.finditer(html or ""):
        raw = re.sub(r"\s+", "", m.group(2))
        try:
            data = base64.b64decode(raw, validate=False)
        except Exception:
            continue
        if len(data) >= 2000:  # 跳过小图标
            payloads.append(data)
        if len(payloads) >= limit:
            return payloads

    if not fetch_remote or len(payloads) >= limit:
        return payloads

    try:
        import httpx
    except ImportError:
        return payloads

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
    }
    for m in _IMG_SRC_RE.finditer(html or ""):
        src = (m.group(1) or "").strip()
        if not src or src.startswith("data:"):
            continue
        full = urljoin(base_url or "", src) if base_url else src
        if not full.startswith("http"):
            continue
        try:
            with httpx.Client(headers=headers, follow_redirects=True, timeout=12.0) as client:
                resp = client.get(full)
                if resp.status_code == 200 and len(resp.content) >= 2000:
                    payloads.append(resp.content)
        except Exception:
            continue
        if len(payloads) >= limit:
            break
    return payloads


def try_ocr_from_html(
    html: str,
    *,
    base_url: str | None = None,
    limit_images: int = 3,
    fetch_remote: bool = False,
) -> tuple[str | None, str]:
    """
    从页面内嵌/远程图片 OCR 抽取文本。

    返回 (text_or_none, note)。未安装依赖时 note 说明如何开启。
    """
    if not (html or "").strip():
        return None, "无 HTML，跳过 OCR"
    if not ocr_available():
        return (
            None,
            "未安装 OCR 依赖（pip install rapidocr-onnxruntime），已跳过",
        )

    payloads = _collect_image_payloads(
        html, base_url=base_url, limit=limit_images, fetch_remote=fetch_remote
    )
    if not payloads:
        return None, "未找到可 OCR 的图片（可后续用截图 OCR）；已用纯文本标签解析"

    chunks: list[str] = []
    for data in payloads:
        text = ocr_image_bytes(data)
        if text:
            chunks.append(text)
    if not chunks:
        return None, "OCR 未识别到有效文字"
    return "\n".join(chunks), f"OCR 已识别 {len(chunks)} 张图片"
