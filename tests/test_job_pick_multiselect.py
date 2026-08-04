"""勾选导入：仅处理所选索引，绝不回退为全部候选。"""

from __future__ import annotations

from pathlib import Path

from app.collector.fill_from_url import (
    FillCandidate,
    find_matching_fill_candidate,
    resolve_jobs_from_url,
)
from app.ui.job_editor import selected_fill_candidates

FIXTURES = Path(__file__).parent / "fixtures"


def _cand(title: str, url: str = "") -> FillCandidate:
    return FillCandidate(
        fields={"title": title, "apply_url": url or f"https://x.example/{title}"},
        label=title,
        summary=title,
    )


def test_selected_fill_candidates_only_checked_indices():
    cands = [_cand("A"), _cand("B"), _cand("C"), _cand("D")]
    # 只勾选 0 和 2 → 不得带回 B/D
    picked = selected_fill_candidates(cands, [0, 2])
    assert [c.fields["title"] for c in picked] == ["A", "C"]
    assert selected_fill_candidates(cands, []) == []
    assert selected_fill_candidates(cands, None) == []
    # 非法索引忽略
    assert [c.fields["title"] for c in selected_fill_candidates(cands, [1, 99, -1, 1])] == [
        "B"
    ]


def test_selected_never_returns_all_when_partial():
    cands = [_cand(f"J{i}") for i in range(5)]
    picked = selected_fill_candidates(cands, [3])
    assert len(picked) == 1
    assert picked[0].fields["title"] == "J3"
    assert len(picked) != len(cands)


def test_find_matching_no_fallback_to_first():
    cands = [
        _cand("营销岗", "https://x.example/a"),
        _cand("后端", "https://x.example/b"),
    ]
    assert (
        find_matching_fill_candidate(cands, title="不存在的岗位", apply_url="") is None
    )
    # 子串标题不算匹配（避免宽匹配导致全变成「更新原行」）
    assert find_matching_fill_candidate(cands, title="营销", apply_url="") is None
    hit = find_matching_fill_candidate(cands, apply_url="https://x.example/b?x=1")
    assert hit is not None
    assert hit.fields["title"] == "后端"


def test_discover_portal_merges_campus_and_intern(monkeypatch):
    """详情页 + discover_portal：合并校园/实习列表，不含社招。"""
    detail = (FIXTURES / "minmetals_portal_detail.html").read_text(encoding="utf-8")
    campus = (FIXTURES / "minmetals_portal_list.html").read_text(encoding="utf-8")
    intern = (FIXTURES / "minmetals_intern_list.html").read_text(encoding="utf-8")

    def fake_fetch(u: str, timeout: float = 20.0) -> str:  # noqa: ARG001
        u = (u or "").lower()
        if "/intern" in u and "detail" not in u:
            return intern
        if "/campus" in u and "detail" not in u:
            return campus
        if "social" in u:
            return "<html><body>社招</body></html>"
        return detail

    result = resolve_jobs_from_url(
        "https://zhaopin.example.com/campus/detail/101",
        html=detail,
        fetch=False,
        ocr_enabled=False,
        discover_portal=True,
        list_collect_months=6,
        list_limit=200,
        fetch_html=fake_fetch,
    )
    assert result.ok, result.error
    titles = [c.fields.get("title") for c in result.candidates]
    assert "科技研发岗" in titles
    assert "投资经理助理实习生" in titles or "投行业务实习生" in titles
    assert result.total >= 4
    # 调用方若只勾选索引 [0]，不得导入其余
    only_first = selected_fill_candidates(result.candidates, [0])
    assert len(only_first) == 1
    assert len(only_first) < result.total


def test_lc_style_discover_from_detail_and_list():
    """联储证券式：详情上卷校招+实习；列表页带更新日期/人数；仅勾选导入。"""
    detail = (FIXTURES / "lc_style_career_detail.html").read_text(encoding="utf-8")
    campus = (FIXTURES / "lc_style_career_list.html").read_text(encoding="utf-8")
    intern = (FIXTURES / "lc_style_intern_list.html").read_text(encoding="utf-8")

    def fake_fetch(u: str, timeout: float = 45.0) -> str:  # noqa: ARG001
        u = (u or "").lower()
        if "/intern" in u and "detail" not in u:
            return intern
        if "/campus" in u and "detail" not in u:
            return campus
        if "social" in u:
            return "<html><body><div>社招壳</div></body></html>"
        return detail

    from_detail = resolve_jobs_from_url(
        "https://career.example.com/campus/detail/501",
        html=detail,
        fetch=False,
        ocr_enabled=False,
        discover_portal=True,
        list_collect_months=6,
        list_limit=200,
        fetch_html=fake_fetch,
    )
    assert from_detail.ok, from_detail.error
    titles = [c.fields.get("title") for c in from_detail.candidates]
    assert "销售交易岗" in titles
    assert "资金交易岗" in titles
    assert "投行业务实习生" in titles
    assert not any("社招" in (t or "") for t in titles)

    from_list = resolve_jobs_from_url(
        "https://career.example.com/campus",
        html=campus,
        fetch=False,
        ocr_enabled=False,
        discover_portal=True,
        list_collect_months=6,
        list_limit=200,
        fetch_html=fake_fetch,
    )
    assert from_list.ok, from_list.error
    by_title = {c.fields.get("title"): c.fields for c in from_list.candidates}
    assert by_title["销售交易岗"].get("headcount") == "3"
    assert "2026-06-10" in (by_title["销售交易岗"].get("open_at") or "")
    assert "投行业务实习生" in by_title

    only = selected_fill_candidates(from_list.candidates, [0, 2])
    assert len(only) == 2
    assert len(only) < from_list.total
