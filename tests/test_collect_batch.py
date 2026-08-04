"""单企业单次采集批大小：最多 50 个未入库，已入库跳过。"""

from __future__ import annotations

from app.collector.fill_from_url import (
    FillCandidate,
    DEFAULT_PER_COMPANY_COLLECT_BATCH,
    _norm_url_key,
    known_keys_from_jobs,
    per_company_batch_size,
    scan_limit_for_batch,
    take_collect_batch,
)


def _cand(title: str, url: str) -> FillCandidate:
    return FillCandidate(
        fields={"title": title, "apply_url": url, "source_url": url},
        label=title,
        summary=title,
    )


def test_default_batch_is_50():
    assert DEFAULT_PER_COMPANY_COLLECT_BATCH == 50
    assert per_company_batch_size({}) == 50
    assert per_company_batch_size({"per_company_collect_batch": 50}) == 50
    assert per_company_batch_size({"per_company_collect_batch": 30}) == 30
    assert scan_limit_for_batch(50) == 200


def test_take_batch_skips_known_and_caps():
    cands = [_cand(f"岗{i}", f"https://x.example/j/{i}") for i in range(120)]
    known_urls, known_titles = known_keys_from_jobs(
        [
            {"title": "岗0", "apply_url": "https://x.example/j/0"},
            {"title": "岗1", "apply_url": "https://x.example/j/1"},
        ]
    )
    batch, stats = take_collect_batch(
        cands, known_urls=known_urls, known_titles=known_titles, batch_size=50
    )
    assert len(batch) == 50
    assert stats["skipped_known"] == 2
    assert batch[0].fields["title"] == "岗2"
    assert batch[-1].fields["title"] == "岗51"
    assert stats["remaining_after_batch"] == 120 - 2 - 50


def test_same_title_at_distinct_detail_url_is_not_skipped():
    candidates = [_cand("算法工程师", "https://x.example/jobs/shanghai")]
    batch, stats = take_collect_batch(
        candidates,
        known_urls={"https://x.example/jobs/hefei"},
        known_titles={"算法工程师"},
        batch_size=50,
    )
    assert len(batch) == 1
    assert stats["skipped_known"] == 0


def test_synthetic_job_query_is_part_of_identity():
    from app.collector.adapters.generic import synthetic_job_url

    hefei = synthetic_job_url(
        "https://x.example/campus", "算法工程师", "合肥", "研发类"
    )
    shanghai = synthetic_job_url(
        "https://x.example/campus", "算法工程师", "上海", "研发类"
    )
    assert _norm_url_key(hefei) != _norm_url_key(shanghai)
    assert "__job=" in _norm_url_key(hefei)


def test_four_batches_cover_200():
    """200 岗分 4 批，每批 50。"""
    cands = [_cand(f"J{i}", f"https://x.example/{i}") for i in range(200)]
    known_urls: set[str] = set()
    known_titles: set[str] = set()
    total = 0
    for _ in range(4):
        batch, stats = take_collect_batch(
            cands, known_urls=known_urls, known_titles=known_titles, batch_size=50
        )
        assert len(batch) == 50
        total += len(batch)
        for c in batch:
            u = (c.fields.get("apply_url") or "").split("?")[0].rstrip("/").lower()
            known_urls.add(u)
            known_titles.add((c.fields.get("title") or "").lower())
    assert total == 200
    empty, stats = take_collect_batch(
        cands, known_urls=known_urls, known_titles=known_titles, batch_size=50
    )
    assert empty == []
    assert stats["skipped_known"] == 200


def test_collect_company_careers_accepts_max_new_jobs():
    import inspect

    from app.collector.website import _company_collect_budget, collect_company_careers

    assert _company_collect_budget(50) == 50
    assert _company_collect_budget(0) == 0
    sig = inspect.signature(collect_company_careers)
    assert "max_new_jobs" in sig.parameters


def test_job_identity_keys_for_company_single_query(tmp_path):
    from pathlib import Path

    from app.db.local import LocalDB

    db = LocalDB(tmp_path / "id.db")
    db.upsert_job(
        {
            "company": "测试公司",
            "title": "后端",
            "apply_url": "https://x.example/a",
            "source_url": "https://x.example/list",
            "status": "pending_review",
        }
    )
    db.upsert_job(
        {
            "company": "其他",
            "title": "前端",
            "apply_url": "https://x.example/b",
            "status": "active",
        }
    )
    urls, titles = db.job_identity_keys_for_company("测试公司")
    assert "https://x.example/a" in urls
    assert "后端" in titles
    assert "https://x.example/b" not in urls


def test_job_identity_keys_include_pending_review_queue(tmp_path):
    from app.db.local import LocalDB

    db = LocalDB(tmp_path / "review-id.db")
    cid = db.upsert_company({"name": "测试公司"})
    db.enqueue_review(
        "missing_fields",
        {
            "company_id": cid,
            "company": "测试公司",
            "title": "数据工程师",
            "apply_url": "https://x.example/jobs/data?jobId=42",
        },
        "待补全",
    )
    urls, titles = db.job_identity_keys_for_company("测试公司", cid)
    assert "https://x.example/jobs/data?jobId=42" in urls
    assert "数据工程师" in titles


def test_deep_collect_forces_serial():
    import inspect

    from app.collector import deep as deep_mod

    src = inspect.getsource(deep_mod.run_deep_collect)
    assert "concurrency = 1" in src
