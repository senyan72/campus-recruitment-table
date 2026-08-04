"""列表 HTML 表格多岗位解析：一公司多行 + 壳标题过滤。"""

from pathlib import Path

from app.collector.adapters.generic import enumerate_job_table_rows, parse_generic
from app.collector.cleanup import cleanup_noise_and_duplicate_jobs
from app.collector.filters import is_noise_title, recover_title_from_jd
from app.db.local import LocalDB

FIXTURES = Path(__file__).parent / "fixtures"


def test_enumerate_job_table_three_titles():
    html = (FIXTURES / "job_list_table.html").read_text(encoding="utf-8")
    posts = enumerate_job_table_rows("https://campus.example.com/school", html)
    titles = [p.title for p in posts]
    assert titles == [
        "编辑岗（2026应届生）",
        "研究岗（2026应届生）",
        "营销岗（2026应届生）",
    ]
    assert all(p.work_location == "北京市-丰台区" for p in posts)
    assert all(p.recruit_project == "校园招聘" or p.recruit_bucket == "校招" for p in posts)
    assert len({p.apply_url for p in posts}) == 3


def test_shell_titles_filtered():
    shells = [
        "Not Found",
        "招聘系统--招聘详细",
        "招聘详细",
        "电子工业出版社有限公司招聘系统--招聘详细",
        "中国电力工程顾问集团有限公司招聘系统--招聘详细",
        "Security Verification",
        "Search Jobs - 中国内地 - 招贤纳才 (中国)",
        "招贤纳才",
        "招聘首页",
        "找到你的理想职位。",
        "中国五矿集团招聘官网",
        "精进电动招聘官网",
    ]
    for t in shells:
        assert is_noise_title(t), t
    assert not is_noise_title("营销岗（2026应届生）")
    assert not is_noise_title("编辑岗（2026应届生）")
    assert not is_noise_title("CN-Store Leader")
    assert not is_noise_title("Software Development Engineer")


def test_recover_title_from_detail_jd():
    jd = (
        "营销岗（2026应届生）\n"
        "招聘类别：校园招聘\n"
        "发布时间：2025-10-11\n"
        "工作地点：北京市-丰台区\n"
        "工作职责\n"
        "1. 负责图书营销策划\n"
    )
    assert recover_title_from_jd(jd) == "营销岗（2026应届生）"
    html = f"<html><head><title>招聘系统--招聘详细</title></head><body><div>{jd}</div></body></html>"
    result = parse_generic("https://campus.example.com/detail/1", html)
    assert result.title == "营销岗（2026应届生）"
    assert result.work_location == "北京市-丰台区"


def test_upsert_keeps_multiple_titles_same_company(tmp_path: Path):
    db = LocalDB(tmp_path / "multi.db")
    cid = "c-phei"
    db.upsert_company({"id": cid, "name": "电子工业出版社有限公司", "verify_status": "official"})
    base = "https://campus.example.com/school"
    for title, loc in (
        ("编辑岗（2026应届生）", "北京市-丰台区"),
        ("研究岗（2026应届生）", "北京市-丰台区"),
        ("营销岗（2026应届生）", "北京市-丰台区"),
    ):
        db.upsert_job(
            {
                "company_id": cid,
                "company": "电子工业出版社有限公司",
                "title": title,
                "source_url": base,
                "apply_url": f"{base}?__job={title}",
                "recruit_project": "校园招聘",
                "recruit_bucket": "校招",
                "work_location": loc,
                "jd_text": f"{title}\n岗位职责：" + "x" * 50,
                "status": "active",
                "confidence": 0.8,
            }
        )
    # 壳标题不应覆盖真实岗
    db.upsert_job(
        {
            "company_id": cid,
            "company": "电子工业出版社有限公司",
            "title": "电子工业出版社有限公司招聘系统--招聘详细",
            "source_url": base,
            "apply_url": base,
            "recruit_project": "校园招聘",
            "recruit_bucket": "校招",
            "jd_text": "营销岗（2026应届生）\n" + "y" * 50,
            "status": "active",
            "confidence": 0.3,
        }
    )
    active = db.list_jobs(status="active", limit=100)
    real_titles = {
        j["title"]
        for j in active
        if j["title"]
        in ("编辑岗（2026应届生）", "研究岗（2026应届生）", "营销岗（2026应届生）")
    }
    assert len(real_titles) == 3

    result = cleanup_noise_and_duplicate_jobs(db)
    assert result["noise_title"] >= 1
    titles = {j["title"] for j in db.list_jobs(status="active", limit=100)}
    assert titles == {
        "编辑岗（2026应届生）",
        "研究岗（2026应届生）",
        "营销岗（2026应届生）",
    }
