"""局部重采入口 discover_portal_jobs_from_url。"""

from __future__ import annotations
from pathlib import Path

from app.collector.fill_from_url import (
    discover_portal_jobs_from_url,
    resolve_jobs_from_url,
)
from app.collector.filters import is_noise_title
from app.ui.job_editor import selected_fill_candidates
from app.collector.fill_from_url import FillCandidate


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_discover_portal_alias_forces_portal_flag():
    """discover_portal_jobs_from_url 等价于 resolve(..., discover_portal=True)。"""
    html = (FIXTURES / "atl_hotjob_interns_list.html").read_text(encoding="utf-8")
    url = "https://wecruit.hotjob.cn/demo/pb/interns.html"
    a = discover_portal_jobs_from_url(url, html=html, fetch=False, list_limit=50)
    b = resolve_jobs_from_url(
        url, html=html, fetch=False, discover_portal=True, list_limit=50
    )
    assert a.ok and b.ok
    assert len(a.candidates) == len(b.candidates)
    assert len(a.candidates) >= 10


def test_atl_interns_no_schedule_or_template_noise():
    html = (FIXTURES / "atl_hotjob_interns_list.html").read_text(encoding="utf-8")
    url = "https://wecruit.hotjob.cn/demo/pb/interns.html"
    result = discover_portal_jobs_from_url(url, html=html, fetch=False)
    assert result.ok
    titles = [c.fields.get("title") or c.label for c in result.candidates]
    assert titles
    for t in titles:
        assert not is_noise_title(t)
        assert "校招行程" not in (t or "")
        assert "{{" not in (t or "")


def test_selected_fill_never_imports_all_when_subset():
    cands = [
        FillCandidate(fields={"title": f"岗{i}"}, label=f"岗{i}", summary="")
        for i in range(5)
    ]
    subset = selected_fill_candidates(cands, [0, 2])
    assert len(subset) == 2
    assert subset[0].fields["title"] == "岗0"
    assert subset[1].fields["title"] == "岗2"


def test_noise_titles_rejected():
    assert is_noise_title("校招行程")
    assert is_noise_title("{{item.postName}} · {{item.workPlace}}")
    assert is_noise_title("应聘指南")
