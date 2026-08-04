"""岗位审核改链接后同步覆盖公司种子 URL。"""

from __future__ import annotations

import json
from pathlib import Path

from app.collector.pipeline import publish_review_item
from app.db.local import (
    LocalDB,
    _apply_seed_url_replacements,
    _dedupe_seed_urls,
)
from app.ui.job_editor import merge_job_fields


def test_dedupe_and_replace_helpers():
    urls = _dedupe_seed_urls(
        [
            "https://a.example/x",
            "https://a.example/x/",
            "https://b.example/y",
            "not-a-url",
        ]
    )
    assert urls == ["https://a.example/x", "https://b.example/y"]

    replaced = _apply_seed_url_replacements(
        ["https://bad.example/old", "https://keep.example/ok"],
        [("https://bad.example/old", "https://good.example/new")],
    )
    assert replaced == ["https://good.example/new", "https://keep.example/ok"]

    # 旧链不在列表：追加新链
    added = _apply_seed_url_replacements(
        ["https://keep.example/ok"],
        [("https://missing.example/x", "https://new.example/y")],
    )
    assert "https://new.example/y" in added
    assert "https://keep.example/ok" in added

    # 清空坏链
    cleared = _apply_seed_url_replacements(
        ["https://bad.example/old", "https://keep.example/ok"],
        [("https://bad.example/old", "")],
    )
    assert cleared == ["https://keep.example/ok"]


def test_sync_seed_urls_from_job_save_replaces_company_urls(tmp_path: Path):
    db = LocalDB(tmp_path / "seed_sync.db")
    cid = db.upsert_company(
        {
            "name": "测试科技",
            "hint_apply_urls": ["https://old.example/hint", "https://keep.example/h"],
            "career_urls": ["https://old.example/career"],
            "verify_status": "unverified",
        }
    )
    jid = db.upsert_job(
        {
            "company_id": cid,
            "company": "测试科技",
            "title": "后端开发",
            "source_url": "https://old.example/career",
            "apply_url": "https://old.example/hint",
            "status": "active",
        }
    )
    before = db.get_job(jid)
    assert before is not None

    edited = merge_job_fields(
        before,
        {
            "company": "测试科技",
            "title": "后端开发",
            "source_url": "https://app.mokahr.com/campus/fixed",
            "apply_url": "https://app.mokahr.com/campus/apply",
            "recruit_bucket": "校招",
            "work_location": "",
            "deadline": "",
            "graduation_batch": "",
            "recruit_project": "",
            "jd_text": "",
        },
    )
    edited["id"] = jid
    edited["company_id"] = cid
    db.upsert_job(edited)

    result = db.sync_seed_urls_from_job_fields(before, edited)
    assert result["synced"] is True
    assert result["company_id"] == cid

    company = db.get_company(cid)
    assert company is not None
    careers = json.loads(company["career_urls"])
    hints = json.loads(company["hint_apply_urls"])
    assert "https://old.example/career" not in careers
    assert "https://old.example/hint" not in hints
    assert "https://app.mokahr.com/campus/fixed" in careers
    assert "https://app.mokahr.com/campus/apply" in careers
    assert "https://app.mokahr.com/campus/fixed" in hints
    assert "https://app.mokahr.com/campus/apply" in hints
    assert "https://keep.example/h" in hints


def test_sync_by_company_name_when_no_company_id(tmp_path: Path):
    db = LocalDB(tmp_path / "by_name.db")
    cid = db.upsert_company(
        {
            "name": "甲乙股份有限公司",
            "hint_apply_urls": ["https://seed.example/old"],
            "career_urls": [],
        }
    )
    before = {
        "company": "甲乙股份有限公司",
        "title": "产品经理",
        "source_url": "https://seed.example/old",
        "apply_url": "",
    }
    after = {
        "company": "甲乙股份有限公司",
        "title": "产品经理",
        "source_url": "https://seed.example/new",
        "apply_url": "https://jobs.feishu.cn/campus/1",
    }
    result = db.sync_seed_urls_from_job_fields(before, after)
    assert result["synced"] is True
    assert result["company_id"] == cid
    company = db.get_company(cid)
    assert company is not None
    careers = json.loads(company["career_urls"])
    hints = json.loads(company["hint_apply_urls"])
    assert "https://seed.example/old" not in hints
    assert "https://seed.example/new" in hints
    assert "https://jobs.feishu.cn/campus/1" in careers


def test_sync_noop_when_urls_unchanged(tmp_path: Path):
    db = LocalDB(tmp_path / "noop.db")
    cid = db.upsert_company(
        {
            "name": "无变更公司",
            "hint_apply_urls": ["https://same.example/a"],
            "career_urls": ["https://same.example/a"],
        }
    )
    job = {
        "company_id": cid,
        "company": "无变更公司",
        "source_url": "https://same.example/a",
        "apply_url": "https://same.example/a",
    }
    result = db.sync_seed_urls_from_job_fields(job, dict(job))
    assert result["synced"] is False
    assert result.get("reason") == "no_url_change"


def test_publish_review_syncs_seed_urls(tmp_path: Path):
    db = LocalDB(tmp_path / "review_sync.db")
    cid = db.upsert_company(
        {
            "name": "异常发布公司",
            "hint_apply_urls": ["https://broken.example/page"],
            "career_urls": ["https://broken.example/page"],
        }
    )
    rid = db.enqueue_review(
        "low_confidence",
        {
            "company_id": cid,
            "company": "异常发布公司",
            "title": "旧标题",
            "source_url": "https://broken.example/page",
            "apply_url": "",
        },
        "链接失效",
    )
    ok = publish_review_item(
        db,
        rid,
        payload_override={
            "company_id": cid,
            "company": "异常发布公司",
            "title": "正式岗位",
            "source_url": "https://careers.example.com/job/1",
            "apply_url": "https://app.mokahr.com/campus/co",
            "recruit_bucket": "校招",
        },
    )
    assert ok is True
    company = db.get_company(cid)
    assert company is not None
    careers = json.loads(company["career_urls"])
    hints = json.loads(company["hint_apply_urls"])
    assert "https://broken.example/page" not in careers
    assert "https://broken.example/page" not in hints
    assert "https://careers.example.com/job/1" in hints
    assert "https://app.mokahr.com/campus/co" in careers


def test_source_verify_official_merges_seed_urls(tmp_path: Path):
    db = LocalDB(tmp_path / "src_official.db")
    cid = db.upsert_company(
        {
            "name": "源验证公司",
            "hint_apply_urls": ["https://existing.example/hint"],
            "career_urls": ["https://existing.example/career"],
            "verify_status": "unverified",
        }
    )
    result = db.sync_company_seed_urls(
        cid,
        add_urls=["https://app.mokahr.com/campus/official"],
        set_official=True,
    )
    assert result["synced"] is True
    company = db.get_company(cid)
    assert company is not None
    assert company["verify_status"] == "official"
    careers = json.loads(company["career_urls"])
    hints = json.loads(company["hint_apply_urls"])
    assert "https://existing.example/career" in careers
    assert "https://app.mokahr.com/campus/official" in careers
    assert "https://existing.example/hint" in hints
    assert "https://app.mokahr.com/campus/official" in hints


def test_source_verify_save_replaces_old_source_value(tmp_path: Path):
    db = LocalDB(tmp_path / "src_save.db")
    cid = db.upsert_company(
        {
            "name": "改源公司",
            "hint_apply_urls": ["https://old-source.example/x"],
            "career_urls": ["https://old-source.example/x"],
        }
    )
    result = db.sync_company_seed_urls(
        cid,
        replacements=[
            ("https://old-source.example/x", "https://new-source.example/y"),
        ],
    )
    assert result["synced"] is True
    company = db.get_company(cid)
    assert company is not None
    careers = json.loads(company["career_urls"])
    hints = json.loads(company["hint_apply_urls"])
    assert careers == ["https://new-source.example/y"]
    assert "https://new-source.example/y" in hints
    assert "https://old-source.example/x" not in hints


def test_no_duplicate_company_rows_on_sync(tmp_path: Path):
    db = LocalDB(tmp_path / "no_dup.db")
    cid = db.upsert_company(
        {
            "name": "唯一公司",
            "hint_apply_urls": ["https://a.example/1"],
            "career_urls": [],
        }
    )
    db.sync_seed_urls_from_job_fields(
        {"company_id": cid, "company": "唯一公司", "source_url": "https://a.example/1"},
        {
            "company_id": cid,
            "company": "唯一公司",
            "source_url": "https://b.example/2",
            "apply_url": "https://b.example/2",
        },
    )
    assert db.count_companies() == 1
    assert db.get_company(cid) is not None
