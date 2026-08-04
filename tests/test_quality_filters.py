from pathlib import Path

from app.collector.cleanup import cleanup_noise_and_duplicate_jobs, deduplicate_jobs
from app.collector.extract import should_auto_publish
from app.collector.filters import (
    is_closed_or_referral_jd,
    is_closed_or_referral_title,
    is_invalid_closed_or_referral_job,
    is_job_detail_link,
    is_job_posting,
    is_noise_location,
    is_noise_title,
    normalize_job_title,
    normalize_job_identity_url,
    normalize_url_for_dedupe,
    recover_title_from_jd,
    sanitize_job_title,
    title_company_mismatch,
)
from app.db.local import LocalDB


def test_noise_title_and_detail_link():
    assert is_noise_title("提示")
    assert is_noise_title("个人中心")
    assert is_noise_title("微官网招聘系统")
    assert is_noise_title("登录")
    assert is_noise_title("Not Found")
    assert is_noise_title("招聘系统--招聘详细")
    assert is_noise_title("电子工业出版社有限公司招聘系统--招聘详细")
    assert is_noise_title("Security Verification")
    assert is_noise_title("Search Jobs - 中国内地 - 招贤纳才 (中国)")
    assert is_noise_title("招贤纳才")
    assert is_noise_title("招聘首页")
    assert is_noise_title("校招行程")
    assert is_noise_title("应聘指南")
    assert is_noise_title("{{item.postName}}")
    assert is_noise_title("{{item.postName}} · {{item.workPlace}}")
    assert is_noise_title("✅ Chrome | ✅ Firefox | ✅ Edge | ✅ Safari")
    assert is_noise_title("【温馨提示】检测到您正在使用兼容模式/旧版IE浏览器")
    assert not is_noise_title("2026届校园招聘公告")
    assert not is_noise_title("营销岗（2026应届生）")
    assert is_job_detail_link("https://x.jobs.feishu.cn/campus/position/1/detail", "后端")
    assert not is_job_detail_link("https://x.jobs.feishu.cn/campus/login", "登录")


def test_sanitize_and_recover_chrome_titles():
    assert sanitize_job_title("Search Jobs - 中国内地 - 招贤纳才 (中国)") is None
    assert sanitize_job_title("CN-Store Leader - 招贤纳才(中国)") == "CN-Store Leader"
    assert sanitize_job_title("Software Development Engineer - 招贤纳才(中国)") == (
        "Software Development Engineer"
    )
    jd = (
        "CN-Store Leader 114438029 Apple Retail\n"
        "工作地点：中国内地 中的各个工作地点\n"
        "Summary\nAs a Store Leader, you inspire your team.\n"
    )
    assert recover_title_from_jd(jd) == "CN-Store Leader 114438029"
    assert is_noise_location("团队")
    assert is_noise_location("筛选")
    assert not is_noise_location("北京市-丰台区")
    assert not is_noise_location("中国内地 中的各个工作地点")


def test_closed_or_referral_titles():
    samples = [
        "当前网页已关停",
        "网页已关闭",
        "职位已关闭",
        "已下线",
        "停止招聘",
        "内部推荐",
        "仅限内推",
        "内推专用",
        "仅内部推荐",
        "【提示】当前网页已关停",
        "本岗位停止招聘",
    ]
    for title in samples:
        assert is_noise_title(title), title
        assert is_closed_or_referral_title(title), title
        assert is_invalid_closed_or_referral_job(title=title), title

    assert not is_closed_or_referral_title("后端开发工程师")
    assert not is_noise_title("后端开发工程师（欢迎内推加分）")


def test_closed_or_referral_jd_and_job_posting():
    assert is_closed_or_referral_jd("很抱歉，该职位已关闭，无法继续投递。")
    assert is_closed_or_referral_jd("本岗位仅内部推荐，不对外开放网申。")
    assert is_closed_or_referral_jd("仅限内推通道，请联系 HR。")
    assert not is_closed_or_referral_jd("岗位职责：开发。" + "欢迎通过内推投递。" + "x" * 40)

    ok, _, reason = is_job_posting(
        title="后端开发工程师",
        source_url="https://demo.jobs.feishu.cn/campus/position/1/detail",
        company="某科技",
        jd_text="该职位已关闭，感谢关注。" + "x" * 40,
    )
    assert not ok
    assert "关闭" in reason or "内推" in reason

    ok2, _, _ = is_job_posting(title="当前网页已关停", source_url="https://x.com/a")
    assert not ok2


def test_title_company_mismatch():
    assert title_company_mismatch("晨光生物", "景顺长城基金校园招聘")
    assert not title_company_mismatch("晨光生物", "晨光生物2026校园招聘")
    assert not title_company_mismatch("字节跳动", "后端开发工程师")


def test_should_auto_publish_rejects_noise_and_mismatch():
    ok, reason = should_auto_publish(
        verify_status="official",
        title="个人中心",
        source_url="https://example.com/a",
        recruit_project="秋招",
        confidence=0.9,
        aggregator=False,
        company="测试公司",
    )
    assert not ok
    assert "噪声" in reason

    ok2, reason2 = should_auto_publish(
        verify_status="official",
        title="景顺长城基金管培生",
        source_url="https://example.com/b",
        recruit_project="秋招",
        confidence=0.9,
        aggregator=False,
        company="晨光生物",
        jd_text="岗位职责：" + "x" * 50,
    )
    assert not ok2
    assert "不符" in reason2


def _raw_insert_job(db: LocalDB, **kw) -> None:
    """绕过 upsert 去重，模拟历史脏数据。"""
    from app.db.local import new_id, utc_now

    now = utc_now()
    jid = kw.get("id") or new_id()
    with db.conn() as c:
        c.execute(
            """
            INSERT INTO jobs(
                id, company_id, company, recruit_project, recruit_bucket, company_nature,
                title, source_url, apply_url, deadline, work_location, industry, education,
                open_at, graduation_batch, jd_text, job_tags, raw_category, parse_status,
                confidence, status, cloud_updated_at, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                jid,
                kw.get("company_id"),
                kw.get("company") or "",
                kw.get("recruit_project"),
                kw.get("recruit_bucket"),
                None,
                kw["title"],
                kw["source_url"],
                kw.get("apply_url"),
                None,
                kw.get("work_location"),
                None,
                None,
                None,
                None,
                kw.get("jd_text"),
                "[]",
                None,
                "ok",
                float(kw.get("confidence") or 0.0),
                "active",
                None,
                now,
                now,
            ),
        )


def test_dedupe_normalize_and_cleanup(tmp_path: Path):
    assert normalize_job_title("【2026届】后端开发") == normalize_job_title("后端开发")
    u1 = normalize_url_for_dedupe("https://A.example.com/job/1/?utm_source=x#y")
    u2 = normalize_url_for_dedupe("https://a.example.com/job/1")
    assert u1 == u2
    assert normalize_job_identity_url("https://a.example.com/job/1?x=1") == u2
    assert "jobId=42" in normalize_job_identity_url(
        "https://a.example.com/job?jobId=42&page=3"
    )
    moka_a = normalize_job_identity_url(
        "https://app.mokahr.com/campus-recruitment/acme/1#/job/"
        "009217cf-2040-4369-b15f-5f332d2dad41"
    )
    moka_b = normalize_job_identity_url(
        "https://app.mokahr.com/campus-recruitment/acme/1#/job/"
        "4a3c366b-2040-4369-b15f-5f332d2dad41"
    )
    assert moka_a != moka_b
    assert moka_a.endswith("#/job/009217cf-2040-4369-b15f-5f332d2dad41")

    db = LocalDB(tmp_path / "q.db")
    cid = "c1"
    db.upsert_company({"id": cid, "name": "晨光生物", "verify_status": "official"})
    _raw_insert_job(
        db,
        company_id=cid,
        company="晨光生物",
        title="提示",
        source_url="https://a.example.com/1",
        recruit_project="校园招聘",
        recruit_bucket="校招",
    )
    _raw_insert_job(
        db,
        company_id=cid,
        company="晨光生物",
        title="晨光生物 校园招聘入口",
        source_url="https://a.example.com/portal",
        apply_url="https://a.example.com/portal",
        recruit_project="校园招聘",
        recruit_bucket="校招",
        confidence=0.4,
    )
    _raw_insert_job(
        db,
        company_id=cid,
        company="晨光生物",
        title="晨光生物校园招聘入口",
        source_url="https://a.example.com/portal2",
        apply_url="https://a.example.com/portal2",
        recruit_project="校园招聘",
        recruit_bucket="校招",
        confidence=0.3,
    )
    _raw_insert_job(
        db,
        company_id=cid,
        company="晨光生物",
        title="景顺长城基金秋招",
        source_url="https://shared-ats.example.com/x",
        recruit_project="秋招",
        recruit_bucket="校招",
    )
    _raw_insert_job(
        db,
        company_id=cid,
        company="晨光生物",
        title="后端开发工程师",
        source_url="https://a.example.com/same-a",
        apply_url="https://a.example.com/be",
        recruit_project="秋招",
        recruit_bucket="校招",
        jd_text="这是一段足够长的岗位介绍，用于去重时保留高质量记录。" * 2,
        confidence=0.9,
    )
    _raw_insert_job(
        db,
        company_id=cid,
        company="晨光生物",
        title="产品经理实习",
        source_url="https://a.example.com/same-b",
        apply_url="https://a.example.com/pm",
        recruit_project="秋招",
        recruit_bucket="校招",
        jd_text="这是另一段足够长的岗位介绍，同公司不同岗位应保留。" * 2,
        confidence=0.9,
    )
    # 同标题重复 → 保留质量高的一条
    _raw_insert_job(
        db,
        company_id=cid,
        company="晨光生物",
        title="后端开发工程师",
        source_url="https://a.example.com/be-dup",
        apply_url="https://a.example.com/be-dup",
        recruit_project="秋招",
        recruit_bucket="校招",
        jd_text="短",
        confidence=0.4,
    )

    result = cleanup_noise_and_duplicate_jobs(db)
    assert result["deleted"] >= 3
    active = db.list_jobs(status="active", limit=100)
    titles = {j["title"] for j in active}
    assert "提示" not in titles
    assert not any("景顺长城" in t for t in titles)
    # 同公司不同岗位名保留多行
    assert "后端开发工程师" in titles
    assert "产品经理实习" in titles


def test_deduplicate_jobs_limits_scope_and_keeps_best_record(tmp_path: Path):
    db = LocalDB(tmp_path / "dedupe_selected_company.db")
    _raw_insert_job(
        db,
        id="best",
        company_id="company-a",
        company="公司 A",
        title="【2026届】后端开发工程师",
        source_url="https://a.example.com/backend-best",
        jd_text="岗位职责：" + "x" * 120,
        confidence=0.9,
    )
    _raw_insert_job(
        db,
        id="duplicate",
        company_id="company-a",
        company="公司 A",
        title="后端开发工程师",
        source_url="https://a.example.com/backend-duplicate",
        jd_text="短 JD",
        confidence=0.2,
    )
    _raw_insert_job(
        db,
        id="other-company",
        company_id="company-b",
        company="公司 B",
        title="后端开发工程师",
        source_url="https://b.example.com/backend",
        jd_text="另一家公司岗位",
        confidence=0.1,
    )

    result = deduplicate_jobs(db, company_keys={"company-a"})

    assert result["deleted"] == 1
    assert result["deleted_ids"] == ["duplicate"]
    assert db.get_job("best")["status"] == "active"
    assert db.get_job("duplicate")["status"] == "deleted"
    assert db.get_job("other-company")["status"] == "active"


def test_cleanup_closed_or_referral_jobs(tmp_path: Path):
    db = LocalDB(tmp_path / "closed.db")
    cid = "c-closed"
    db.upsert_company({"id": cid, "name": "测试公司", "verify_status": "official"})
    _raw_insert_job(
        db,
        id="j-closed-title",
        company_id=cid,
        company="测试公司",
        title="当前网页已关停",
        source_url="https://ex.com/closed",
        recruit_project="秋招",
        recruit_bucket="校招",
        jd_text="占位",
    )
    _raw_insert_job(
        db,
        id="j-referral-title",
        company_id=cid,
        company="测试公司",
        title="内部推荐",
        source_url="https://ex.com/ref",
        recruit_project="秋招",
        recruit_bucket="校招",
    )
    _raw_insert_job(
        db,
        id="j-closed-jd",
        company_id=cid,
        company="测试公司",
        title="数据分析师",
        source_url="https://ex.com/da",
        recruit_project="秋招",
        recruit_bucket="校招",
        jd_text="抱歉，该职位已关闭，无法投递。" + "x" * 20,
        confidence=0.9,
    )
    _raw_insert_job(
        db,
        id="j-keep",
        company_id=cid,
        company="测试公司",
        title="后端开发工程师",
        source_url="https://ex.com/be",
        recruit_project="秋招",
        recruit_bucket="校招",
        jd_text="岗位职责：负责后端开发与维护。" + "x" * 40,
        confidence=0.9,
    )

    result = cleanup_noise_and_duplicate_jobs(db)
    assert result["closed_or_referral_title"] >= 2
    assert result["closed_or_referral_jd"] >= 1
    active_titles = {j["title"] for j in db.list_jobs(status="active", limit=100)}
    assert "当前网页已关停" not in active_titles
    assert "内部推荐" not in active_titles
    assert "数据分析师" not in active_titles
    assert "后端开发工程师" in active_titles
