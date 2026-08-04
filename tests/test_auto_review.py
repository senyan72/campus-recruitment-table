from datetime import date

from app.collector.auto_review import (
    assess_job_for_auto_review,
    assess_jobs_for_auto_review,
)


TODAY = date(2026, 8, 3)


def _job(**overrides):
    job = {
        "id": "j1",
        "status": "pending_review",
        "company": "测试科技有限公司",
        "title": "后端开发工程师",
        "open_at": "2026-06-01",
        "jd_text": (
            "岗位职责：\n负责服务端功能设计、开发、测试和线上维护。\n\n"
            "任职要求：\n本科及以上学历，熟悉 Python 和数据库开发。"
        ),
    }
    job.update(overrides)
    return job


def test_complete_recent_job_is_eligible():
    result = assess_job_for_auto_review(_job(), today=TODAY)
    assert result.eligible
    assert result.reasons == ()
    assert result.duties_length >= 10
    assert result.requirements_length >= 10


def test_missing_or_incomplete_fields_stay_pending():
    result = assess_job_for_auto_review(
        _job(company="", jd_text="岗位职责：写代码", open_at=""),
        today=TODAY,
    )
    assert not result.eligible
    assert "公司缺失" in result.reasons
    assert "岗位要求不完整" in result.reasons
    assert "任职要求不完整" in result.reasons
    assert "岗位发布时间缺失" in result.reasons


def test_old_and_future_dates_are_rejected():
    old = assess_job_for_auto_review(_job(open_at="2026-04-30"), today=TODAY)
    future = assess_job_for_auto_review(_job(open_at="2026-08-04"), today=TODAY)
    assert not old.eligible
    assert "岗位发布时间超过3个月" in old.reasons
    assert not future.eligible
    assert "岗位发布时间晚于今天" in future.reasons


def test_english_sections_are_supported_and_report_counts_reasons():
    english = _job(
        id="en",
        jd_text=(
            "Responsibilities:\nBuild and maintain reliable data services.\n\n"
            "Requirements:\nBachelor degree and strong Python skills."
        ),
    )
    missing = _job(id="bad", company="待确认")
    report = assess_jobs_for_auto_review([english, missing], today=TODAY)
    assert report.eligible_ids == ["en"]
    assert len(report.skipped) == 1
    assert "公司缺失 1 条" in report.summary_zh()
