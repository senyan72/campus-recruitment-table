"""局部重采：网络发现与解析必须在独立进程，Tk 仅轮询消息。"""

from __future__ import annotations

import inspect

from app.collector.fill_from_url import discover_portal_jobs_from_url
from app.ui.job_review import JobReviewPanel


def test_reidentify_selected_starts_background_process():
    src = inspect.getsource(JobReviewPanel.reidentify_selected)
    assert "_start_reidentify_process" in src
    assert "threading.Thread" not in src
    assert '"mode": "discover"' in src
    assert '"mode": "batch"' in src
    assert "reidentify_collect_batch" in inspect.getsource(JobReviewPanel._reidentify_limits)
    # 主线程快照表单，禁止 worker 读 Tk
    assert "self.form.values()" in src
    assert "snapshots" in src
    # 不在主线程 join 阻塞
    assert ".join(" not in src


def test_reidentify_poll_applies_committed_items_incrementally():
    poll_src = inspect.getsource(JobReviewPanel._poll_reidentify_process)
    assert 'kind == "item"' in poll_src
    assert "_apply_reidentify_item_events" in poll_src
    apply_src = inspect.getsource(JobReviewPanel._apply_reidentify_item_events)
    assert "self.db.get_job" in apply_src
    assert "self.tree.insert" in apply_src
    assert "self.refresh()" not in apply_src


def test_reidentify_finish_clears_busy_via_helper():
    src = inspect.getsource(JobReviewPanel._reidentify_finish)
    assert "_set_reidentify_busy(False)" in src
    btn_src = inspect.getsource(JobReviewPanel._set_reidentify_busy)
    assert "disabled" in btn_src
    assert "重新识别" in btn_src


def test_discover_portal_default_limit_and_timeout():
    sig = inspect.signature(discover_portal_jobs_from_url)
    # 默认 None → 函数内用 DEFAULT_PER_COMPANY_COLLECT_BATCH(50)
    assert sig.parameters["list_limit"].default is None
    assert "progress" in sig.parameters
    from app.collector.fill_from_url import DEFAULT_PER_COMPANY_COLLECT_BATCH

    assert DEFAULT_PER_COMPANY_COLLECT_BATCH == 50
