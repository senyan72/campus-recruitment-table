"""重新识别勾选写入：不匹配原行则新增 pending_review；壳岗不入库。"""

from __future__ import annotations

from pathlib import Path

from app.collector.fill_from_url import (
    FillCandidate,
    apply_reidentify_fields,
    fields_look_like_no_job_posting,
    find_matching_fill_candidate,
)
from app.db.local import LocalDB


def _cand(title: str, apply: str, source: str | None = None) -> FillCandidate:
    return FillCandidate(
        fields={
            "title": title,
            "apply_url": apply,
            "source_url": source or apply,
            "company": "测试集团",
            "recruit_bucket": "校招",
            "jd_text": f"岗位职责：{title}相关工作内容说明不少于四十字以保证非空壳。",
        },
        label=title,
        summary=title,
    )


def _write_chosen(
    db: LocalDB,
    *,
    item_id: str,
    before: dict,
    chosen: list[FillCandidate],
) -> tuple[int, int, int]:
    """复刻审核台：匹配原行则更新，其余新增；返回 (inserted, updated, skipped_shell)。"""
    original_match = find_matching_fill_candidate(
        chosen,
        title=before.get("title"),
        source_url=before.get("source_url"),
        apply_url=before.get("apply_url"),
    )
    inserted = 0
    updated = 0
    skipped = 0
    updated_original = False
    for cand in chosen:
        fields = cand.fields or {}
        if fields_look_like_no_job_posting(fields):
            skipped += 1
            continue
        if original_match is cand and not updated_original:
            merged = apply_reidentify_fields(before, fields)
            merged["id"] = item_id
            merged["status"] = "pending_review"
            db.upsert_job(merged)
            updated = 1
            updated_original = True
        else:
            base = {
                "company": fields.get("company") or before.get("company") or "未知企业",
                "group_name": before.get("group_name"),
                "status": "pending_review",
            }
            merged = apply_reidentify_fields(base, fields)
            merged.pop("id", None)
            merged["status"] = "pending_review"
            db.upsert_job(merged)
            inserted += 1
    return inserted, updated, skipped


def test_two_unmatched_checked_insert_two_pending(tmp_path: Path):
    db = LocalDB(tmp_path / "re_ins.db")
    oid = db.upsert_job(
        {
            "company": "测试集团",
            "title": "原壳岗位",
            "source_url": "https://campus.example.com/school",
            "apply_url": "https://campus.example.com/school",
            "status": "pending_review",
            "jd_text": "旧",
        }
    )
    before = db.get_job(oid)
    assert before is not None
    chosen = [
        _cand("营销岗（2027应届）", "https://campus.example.com/job/m"),
        _cand("研发岗（2027应届）", "https://campus.example.com/job/r"),
    ]
    assert find_matching_fill_candidate(
        chosen,
        title=before.get("title"),
        source_url=before.get("source_url"),
        apply_url=before.get("apply_url"),
    ) is None

    inserted, updated, skipped = _write_chosen(
        db, item_id=oid, before=before, chosen=chosen
    )
    assert inserted == 2
    assert updated == 0
    assert skipped == 0
    orig = db.get_job(oid)
    assert orig is not None
    assert orig["title"] == "原壳岗位"
    pending = db.list_jobs(status="pending_review", limit=50)
    titles = {j["title"] for j in pending}
    assert "原壳岗位" in titles
    assert "营销岗（2027应届）" in titles
    assert "研发岗（2027应届）" in titles
    assert db.count_jobs(status="pending_review") == 3


def test_matched_one_updates_rest_insert(tmp_path: Path):
    db = LocalDB(tmp_path / "re_upd.db")
    oid = db.upsert_job(
        {
            "company": "测试集团",
            "title": "营销岗（2027应届）",
            "source_url": "https://campus.example.com/job/m",
            "apply_url": "https://campus.example.com/job/m",
            "status": "pending_review",
            "jd_text": "旧JD",
        }
    )
    before = db.get_job(oid)
    assert before is not None
    chosen = [
        _cand("营销岗（2027应届）", "https://campus.example.com/job/m"),
        _cand("研发岗（2027应届）", "https://campus.example.com/job/r"),
    ]
    inserted, updated, skipped = _write_chosen(
        db, item_id=oid, before=before, chosen=chosen
    )
    assert updated == 1
    assert inserted == 1
    assert skipped == 0
    assert db.count_jobs(status="pending_review") == 2


def test_shell_and_itinerary_not_inserted(tmp_path: Path):
    db = LocalDB(tmp_path / "re_shell.db")
    oid = db.upsert_job(
        {
            "company": "测试集团",
            "title": "占位",
            "source_url": "https://campus.example.com/school",
            "apply_url": "https://campus.example.com/school",
            "status": "pending_review",
        }
    )
    before = db.get_job(oid)
    assert before is not None
    shells = [
        FillCandidate(
            fields={
                "title": "招聘官网",
                "apply_url": "https://campus.example.com/",
                "source_url": "https://campus.example.com/",
                "jd_text": "",
            },
            label="招聘官网",
            summary="壳",
        ),
        FillCandidate(
            fields={
                "title": "2027届校招行程",
                "apply_url": "https://campus.example.com/tour",
                "source_url": "https://campus.example.com/tour",
                "jd_text": "宣讲会安排",
            },
            label="行程",
            summary="行程",
        ),
        FillCandidate(
            fields={
                "title": "{{item.postName}}",
                "apply_url": "https://campus.example.com/t",
                "source_url": "https://campus.example.com/t",
                "jd_text": "",
            },
            label="模板",
            summary="模板",
        ),
    ]
    for s in shells:
        assert fields_look_like_no_job_posting(s.fields), s.fields.get("title")

    inserted, updated, skipped = _write_chosen(
        db, item_id=oid, before=before, chosen=shells
    )
    assert inserted == 0
    assert updated == 0
    assert skipped == 3
    assert db.count_jobs(status="pending_review") == 1
    assert db.get_job(oid)["title"] == "占位"
