"""信息时效性识别：近 6 个月窗 + 源码日期优先级。"""

from datetime import date

from app.collector.freshness_check import evaluate_timeliness, resolve_job_content_date
from app.collector.filters import list_date_cutoff


TODAY = date(2026, 8, 2)
CUTOFF = list_date_cutoff(months=6, today=TODAY)  # 2026-02-03


def test_list_cutoff_six_months():
    assert CUTOFF == date(2026, 2, 3)


def test_resolve_open_at_beats_page_mtime():
    html = """
    <script>var $bs_vars={'version':'2024.01.01.001'};</script>
    """
    job = {"open_at": "2026-06-01", "jd_text": "", "id": "a"}
    d, src, write = resolve_job_content_date(job, html=html)
    assert d == date(2026, 6, 1)
    assert src == "open_at"
    assert write is None


def test_resolve_page_mtime_when_no_open_at():
    html = """
    <script>var $bs_vars={'version':'2025.05.30.001'};</script>
    <img src="x_medias_20241226_20241226logo.png"/>
    """
    job = {"open_at": "", "jd_text": "", "id": "b"}
    d, src, write = resolve_job_content_date(job, html=html)
    assert d == date(2025, 5, 30)
    assert src == "platform_version"
    assert write == "2025-05-30"


def test_evaluate_keeps_in_window_deletes_out(monkeypatch):
    jobs = [
        {"id": "keep1", "title": "新岗", "open_at": "2026-04-01"},
        {"id": "del1", "title": "旧岗", "open_at": "2025-10-01"},
        {"id": "nodate", "title": "无日期", "open_at": "", "created_at": ""},
    ]
    report = evaluate_timeliness(
        jobs, months=6, today=TODAY, fetch_missing=False
    )
    assert report.cutoff == CUTOFF
    assert "keep1" in report.keep_ids
    assert "del1" in report.delete_ids
    assert "nodate" in report.keep_ids  # 无日期保留
    actions = {i.job_id: i.action for i in report.items}
    assert actions["nodate"] == "keep_no_date"
    summary = report.summary_zh()
    assert "保留" in summary and "删除" in summary


def test_labeled_update_in_jd():
    job = {
        "id": "c",
        "open_at": "",
        "jd_text": "岗位名\n更新日期：2026-03-15\n工作职责：开发",
    }
    d, src, write = resolve_job_content_date(job, html=None)
    assert d == date(2026, 3, 15)
    assert src == "labeled"


def test_timeliness_does_not_rewrite_grad_batch():
    """窗内保留不等于改成目标届；消息提示旧届，字段本身不改写。"""
    jobs = [
        {
            "id": "b25",
            "title": "助理上位机软件开发工程师(25届)",
            "open_at": "2026-04-01",
            "graduation_batch": "2025届",
        }
    ]
    report = evaluate_timeliness(jobs, months=6, today=TODAY, fetch_missing=False)
    assert "b25" in report.keep_ids
    item = next(i for i in report.items if i.job_id == "b25")
    assert "2025届" in item.message
    assert "未改写" in item.message
    # 输入对象届别未被副作用改写
    assert jobs[0]["graduation_batch"] == "2025届"
    assert item.open_at_write is None
