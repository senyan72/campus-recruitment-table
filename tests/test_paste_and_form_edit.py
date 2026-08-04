"""应急粘贴入库路径 + 识别后表单可继续编辑（逻辑层）。"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from app.db.local import LocalDB
from app.ui.job_editor import JobEditForm, merge_job_fields
from app.ui.job_review import JobReviewPanel


def test_on_select_skips_reload_for_same_job():
    src = inspect.getsource(JobReviewPanel._on_select)
    assert "item_id == self._current_id" in src
    assert "从链接识别填充" in src or "勿重载" in src


def test_job_edit_form_ensures_editable_after_fill():
    src = inspect.getsource(JobEditForm)
    assert "_ensure_entries_editable" in src
    assert "state=\"normal\"" in src or "state='normal'" in src
    fill_done = inspect.getsource(JobEditForm._on_enrich_done)
    assert "_ensure_entries_editable" in fill_done


def test_paste_save_pending_review_and_seed_sync(tmp_path: Path):
    db = LocalDB(tmp_path / "paste.db")
    cid = db.upsert_company(
        {
            "name": "测试集团",
            "career_urls": ["https://old.example/careers"],
            "hint_apply_urls": [],
            "verify_status": "official",
        }
    )
    before = {
        "company": "测试集团",
        "title": "旧标题",
        "source_url": "https://old.example/careers",
        "apply_url": "",
    }
    edited = merge_job_fields(
        before,
        {
            "group_name": "测试集团",
            "company": "测试子公司",
            "title": "校招研发工程师",
            "salary_range": "15-25K",
            "headcount": "5",
            "graduation_batch": "2027届",
            "recruit_project": "校园招聘",
            "recruit_bucket": "校招",
            "work_location": "上海",
            "education": "本科",
            "deadline": "",
            "source_url": "https://new.example/job/1",
            "apply_url": "https://new.example/apply/1",
            "jd_text": "岗位职责：开发\n任职要求：Python",
        },
    )
    edited["status"] = "pending_review"
    edited["company_id"] = cid
    jid = db.upsert_job(edited)
    job = db.get_job(jid)
    assert job is not None
    assert job["status"] == "pending_review"
    assert job["salary_range"] == "15-25K"
    assert job["headcount"] == "5"
    assert job["education"] == "本科"
    assert job["group_name"] == "测试集团"
    sync = db.sync_seed_urls_from_job_fields(before, job)
    assert sync.get("synced") is True
    co = db.get_company(cid)
    assert co is not None
    careers = json.loads(co["career_urls"] or "[]")
    assert any("new.example" in str(u) for u in careers)


def test_admin_paste_uses_resolve_path():
    import app.ui.admin as admin_mod

    src = inspect.getsource(admin_mod.AdminApp._build_paste)
    assert "JobEditForm" in src
    assert "从链接识别" in src
    recognize = inspect.getsource(admin_mod.AdminApp.recognize_paste_url)
    assert "discover_portal_jobs_from_url" in recognize
    assert "fetch=True" in recognize
    assert "scan_limit_for_batch" in recognize
    assert "per_company_batch_size" in recognize
    resolved = inspect.getsource(admin_mod.AdminApp._paste_on_resolved)
    assert "pick_job_candidates" in resolved
    assert "take_collect_batch" in resolved
    save = inspect.getsource(admin_mod.AdminApp.save_paste_job)
    assert "pending_review" in save
    assert "sync_seed_urls_from_job_fields" in save
