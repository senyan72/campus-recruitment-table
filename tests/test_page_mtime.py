"""北森/源码页面更新时间抽取。"""

from app.collector.adapters.base import ParseResult
from app.collector.label_fields import enrich_with_label_extraction
from app.collector.page_mtime import (
    extract_banner_media_date,
    extract_inline_latest_publish_date,
    extract_logo_media_date,
    extract_page_mtime,
    extract_platform_version_date,
    parse_compact_banner_date,
    parse_platform_version_token,
)


BS_HTML = """
<html><head>
<script>
var $bs_vars={'version':'2025.05.30.001','tenant':'demo'};
</script>
<script src="//stc.beisen.com/2025.05.30.001/scripts/app.js"></script>
</head><body>
<img src="https://cdn.example.com/107964_medias_20241226_20241226logo.png?v=638708217148270000"/>
<img src="https://cdn.example.com/site_2022817_home_banner.jpg"/>
<div>后端开发工程师</div>
</body></html>
"""

LABELED_HTML = """
<html><body>
<div>更新日期：2026-06-26</div>
<script>var $bs_vars={'version':'2024.01.01.001'};</script>
<img src="https://cdn.example.com/medias_20241226_20241226logo.png"/>
</body></html>
"""


def test_parse_platform_version_token():
    assert parse_platform_version_token("2025.05.30.001").isoformat() == "2025-05-30"
    assert parse_platform_version_token("bad") is None


def test_parse_compact_banner_date():
    assert parse_compact_banner_date("20220817").isoformat() == "2022-08-17"
    assert parse_compact_banner_date("2022817").isoformat() == "2022-08-17"


def test_extract_platform_from_bs_vars_and_stc():
    hit = extract_platform_version_date(BS_HTML)
    assert hit is not None
    assert hit.date == "2025-05-30"
    assert hit.source == "platform_version"


def test_extract_logo_and_banner():
    logo = extract_logo_media_date(BS_HTML)
    assert logo is not None
    assert logo.date == "2024-12-26"
    banner = extract_banner_media_date(BS_HTML)
    assert banner is not None
    assert banner.date == "2022-08-17"


def test_priority_platform_over_logo_banner():
    hit = extract_page_mtime(BS_HTML, prefer_labeled=False)
    assert hit is not None
    assert hit.source == "platform_version"
    assert hit.date == "2025-05-30"


def test_labeled_beats_platform_version():
    hit = extract_page_mtime(LABELED_HTML, prefer_labeled=True)
    assert hit is not None
    assert hit.source == "labeled"
    assert hit.date == "2026-06-26"


def test_enrich_fills_published_at_from_page_mtime():
    result = ParseResult(title="后端", parse_status="ok", confidence=0.5, extras={})
    out = enrich_with_label_extraction(result, BS_HTML)
    assert out.extras.get("published_at") == "2025-05-30"
    assert out.extras.get("page_mtime_source") == "platform_version"


def test_enrich_does_not_override_explicit_label():
    result = ParseResult(title="后端", parse_status="ok", confidence=0.5, extras={})
    out = enrich_with_label_extraction(result, LABELED_HTML)
    assert out.extras.get("published_at") == "2026-06-26"


HOTJOB_INLINE_HTML = """
<html><body>
<h1>Color &amp; Graphic Design Intern</h1>
<div class="meta">上海市 | 2026-07-31 最新</div>
<div>工作职责：设计相关工作</div>
</body></html>
"""


def test_extract_inline_latest_hotjob_style():
    hit = extract_inline_latest_publish_date("上海市 | 2026-07-31 最新")
    assert hit is not None
    assert hit.date == "2026-07-31"
    assert hit.source == "inline_latest"

    hit2 = extract_inline_latest_publish_date("2026年7月31日最新发布")
    assert hit2 is not None
    assert hit2.date == "2026-07-31"

    hit3 = extract_page_mtime(HOTJOB_INLINE_HTML, prefer_labeled=True)
    assert hit3 is not None
    assert hit3.date == "2026-07-31"
    assert hit3.source == "inline_latest"


def test_enrich_fills_published_at_from_inline_latest():
    result = ParseResult(title="设计实习", parse_status="ok", confidence=0.5, extras={})
    out = enrich_with_label_extraction(result, HOTJOB_INLINE_HTML)
    assert out.extras.get("published_at") == "2026-07-31"
    assert out.extras.get("page_mtime_source") == "inline_latest"
