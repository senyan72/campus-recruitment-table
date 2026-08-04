"""应届届别（年+1）与正常/异常筛选口径。"""

from datetime import date
from pathlib import Path

from app.collector.adapters.generic import enumerate_job_table_rows
from app.collector.filters import (
    campus_publish_cutoff,
    current_grad_batch,
    extract_grad_batch,
    is_outdated_campus_publish,
    is_target_campus_or_intern,
    parse_grad_batch,
    resolve_grad_batch,
)

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 8, 1)


def test_current_grad_batch_year_plus_one():
    assert current_grad_batch(TODAY) == "2027届"
    assert current_grad_batch(date(2025, 1, 1)) == "2026届"


def test_parse_and_resolve_grad_batch():
    assert parse_grad_batch("后端开发（2027届）") == "2027届"
    assert parse_grad_batch("营销岗（2026应届生）") == "2026届"
    assert parse_grad_batch("无届别信息") is None
    assert resolve_grad_batch(title="算法工程师", today=TODAY) == "2027届"
    assert resolve_grad_batch(title="产品（2025应届）", today=TODAY) == "2025届"


def test_short_year_title_batch_not_stomped_to_2027():
    """标题 (25届)/(26届) 必须解析为 2025/2026，不得默认成目标 2027届。"""
    title_25 = "助理上位机软件开发工程师(25届) - 加入望汭 (上海) 自动化技术有限公司"
    title_26 = "算法工程师（26届）"
    assert parse_grad_batch(title_25) == "2025届"
    assert parse_grad_batch(title_26) == "2026届"
    assert parse_grad_batch("嵌入式开发(25应届生)") == "2025届"
    assert resolve_grad_batch(title=title_25, today=TODAY) == "2025届"
    assert resolve_grad_batch(title=title_26, today=TODAY) == "2026届"
    # 已有错误默认 2027 时，标题真值仍胜出
    assert (
        resolve_grad_batch(title=title_25, existing="2027届", today=TODAY) == "2025届"
    )
    ok, kind, reason = is_target_campus_or_intern(
        title=title_25,
        recruit_bucket="校招",
        recruit_project="校园招聘",
        today=TODAY,
    )
    assert not ok
    assert kind == "wrong_grad_batch"
    assert "2025届" in reason and "2027届" in reason


def test_target_campus_2027_normal():
    ok, kind, reason = is_target_campus_or_intern(
        title="软件开发工程师（2027届）",
        recruit_bucket="校招",
        recruit_project="校园招聘",
        today=TODAY,
    )
    assert ok and kind == ""
    assert reason == ""


def test_old_campus_2025_abnormal():
    ok, kind, reason = is_target_campus_or_intern(
        title="管培生（2025届校招）",
        recruit_bucket="校招",
        recruit_project="校园招聘",
        today=TODAY,
    )
    assert not ok
    assert kind == "wrong_grad_batch"
    assert "2025届" in reason and "2027届" in reason


def test_intern_always_normal():
    ok, kind, _ = is_target_campus_or_intern(
        title="日常实习生",
        recruit_bucket="日常实习",
        today=TODAY,
    )
    assert ok and kind == ""
    ok2, kind2, _ = is_target_campus_or_intern(
        title="数据分析实习生（可转正）",
        recruit_project="暑期实习",
        today=TODAY,
    )
    assert ok2 and kind2 == ""


def test_social_hiring_abnormal():
    ok, kind, reason = is_target_campus_or_intern(
        title="Java开发（社会招聘）",
        recruit_project="社会招聘",
        today=TODAY,
    )
    assert not ok
    assert kind == "not_target_hiring"
    assert "社招" in reason or "非目标" in reason


def test_job_table_still_three_titles():
    html = (FIXTURES / "job_list_table.html").read_text(encoding="utf-8")
    posts = enumerate_job_table_rows("https://campus.example.com/school", html)
    assert [p.title for p in posts] == [
        "编辑岗（2026应届生）",
        "研究岗（2026应届生）",
        "营销岗（2026应届生）",
    ]
    # 在 2026-08-01，标题含 2026应届 → 非目标届，应判异常
    for p in posts:
        ok, kind, _ = is_target_campus_or_intern(
            title=p.title,
            recruit_bucket="校招",
            recruit_project=p.recruit_project,
            today=TODAY,
        )
        assert not ok
        assert kind == "wrong_grad_batch"


def test_extract_bare_year_in_title():
    assert extract_grad_batch("2025环保工程师", allow_bare_year=True) == "2025届"
    assert extract_grad_batch("2025环保工程师", allow_bare_year=False) is None
    assert extract_grad_batch("后端（2027届）", allow_bare_year=True) == "2027届"
    # 日期形态不应当成届别
    assert extract_grad_batch("公告 2025-09-18 发布", allow_bare_year=True) is None


def test_title_bare_year_2025_campus_abnormal():
    """标题含 2025 + 校招 → 非当年（wrong_grad_batch）。"""
    ok, kind, reason = is_target_campus_or_intern(
        title="2025环保工程师",
        recruit_bucket="校招",
        recruit_project="校园招聘",
        today=TODAY,
    )
    assert not ok
    assert kind == "wrong_grad_batch"
    assert "2025届" in reason and "2027届" in reason


def test_publish_2024_campus_abnormal():
    """发布时间 2024 + 校园招聘 → not_current_campus。"""
    assert campus_publish_cutoff(TODAY) == date(2025, 1, 1)
    hit, msg = is_outdated_campus_publish(open_at="2024-09-18", today=TODAY)
    assert hit and "2024-09-18" in msg

    ok, kind, reason = is_target_campus_or_intern(
        title="软件开发工程师",
        recruit_bucket="校招",
        recruit_project="校园招聘",
        open_at="2024-09-18",
        jd_text="招聘类型：校园招聘\n发布时间：2024-09-18\n岗位职责：开发",
        today=TODAY,
    )
    assert not ok
    assert kind == "not_current_campus"
    assert "非当年" in reason or "2024-09-18" in reason


def test_title_2027_not_abnormal_by_supplement():
    """2027届标题 → 不因本补充规则（裸年份/旧发布窗口标题侧）异常。"""
    ok, kind, reason = is_target_campus_or_intern(
        title="软件开发工程师（2027届）",
        recruit_bucket="校招",
        recruit_project="校园招聘",
        open_at="2025-10-01",
        today=TODAY,
    )
    assert ok and kind == ""
    assert reason == ""


def test_intern_not_killed_by_old_publish():
    """实习岗不因旧发布时间误杀。"""
    ok, kind, _ = is_target_campus_or_intern(
        title="数据分析实习生",
        recruit_bucket="日常实习",
        recruit_project="日常实习",
        open_at="2024-09-18",
        jd_text="发布时间：2024-09-18\n岗位职责：数据分析",
        today=TODAY,
    )
    assert ok and kind == ""

    # 标题明确旧届校招的实习仍异常
    ok2, kind2, _ = is_target_campus_or_intern(
        title="2025届校招管培生实习",
        recruit_bucket="日常实习",
        today=TODAY,
    )
    assert not ok2
    assert kind2 == "wrong_grad_batch"
