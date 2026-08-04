"""岗位审核多选辅助与推云配置校验（无真实密钥）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db.local import LocalDB
from app.sync.supabase import SupabaseSync
from app.ui.job_review import (
    _ABNORMAL_COLS,
    _DISPLAY_COLS,
    _NORMAL_COLS,
    _REVIEW_COLS,
    cloud_upload_progress,
    display_group_name,
    display_jd_sections,
    display_job_type,
    display_page_updated_at,
    prefer_job_url,
    resolve_page_updated_at,
    review_row_values,
    tree_range_iids,
)
from app.ui.tree_check import CHECK_COL, CHECK_OFF, CHECK_ON, check_mark, with_check


def test_page_updated_at_prefers_open_at_not_db_stamp():
    job = {
        "open_at": "2026-03-01",
        "list_updated_at": "2026-02-01",
        "updated_at": "2026-08-01T99:00:00",
        "created_at": "2026-08-01",
    }
    assert resolve_page_updated_at(job) == "2026-03-01"
    assert display_page_updated_at(job) == "2026-03-01"
    assert display_page_updated_at({"updated_at": "2026-08-01"}) == "-"
    assert display_page_updated_at({"published_at": "2025-12-20T08:00:00"}) == "2025-12-20 08:00"


def test_tree_range_iids_order():
    kids = ["a", "b", "c", "d", "e"]
    assert tree_range_iids(kids, "b", "d") == ["b", "c", "d"]
    assert tree_range_iids(kids, "d", "b") == ["b", "c", "d"]
    assert tree_range_iids(kids, "a", "a") == ["a"]
    assert tree_range_iids([], "a", "b") == []


def test_with_check_prefix():
    assert check_mark(False) == CHECK_OFF
    assert check_mark(True) == CHECK_ON
    assert with_check(("公司", "岗位")) == (CHECK_OFF, "公司", "岗位")
    assert with_check(("a",), selected=True) == (CHECK_ON, "a")


def test_prefer_job_url():
    assert prefer_job_url({"apply_url": "https://a.example/apply", "source_url": "https://a.example/src"}) == (
        "https://a.example/apply"
    )
    assert prefer_job_url({"source_url": "https://a.example/src"}) == "https://a.example/src"
    assert prefer_job_url({}) == ""


def test_prefer_job_url_adds_hotjob_post_type():
    base = (
        "https://wecruit.hotjob.cn/SU654f2e0b3538bc6c4d600eab/pb/posDetail.html"
        "?postId=6a697041ba2dc64cbb91229f"
    )
    out = prefer_job_url(
        {
            "apply_url": base,
            "recruit_project": "实习生招聘",
            "recruit_bucket": "日常实习",
        }
    )
    assert "postType=intern" in out
    assert "postId=6a697041ba2dc64cbb91229f" in out
    # 已有 postType 不重复改写
    already = base + "&postType=intern"
    assert prefer_job_url({"apply_url": already}) == already


def test_review_column_order():
    titles = [t for _k, _w, t in _REVIEW_COLS]
    assert titles == [
        "集团",
        "公司",
        "岗位名称",
        "薪资范围",
        "招聘人数",
        "岗位类型",
        "岗位要求",
        "任职要求",
        "学历要求",
        "工作地点",
        "链接",
        "岗位发布时间",
    ]
    assert _NORMAL_COLS == _REVIEW_COLS
    ab_titles = [t for _k, _w, t in _ABNORMAL_COLS]
    assert ab_titles[: len(titles)] == titles
    assert ab_titles[-2:] == ["状态", "原因"]
    disp_titles = [t for _k, _w, t in _DISPLAY_COLS]
    assert disp_titles[: len(titles)] == titles
    assert disp_titles[-1] == "云端上传进度"
    # 勾选列仍前置
    assert with_check(review_row_values({"company": "A", "title": "T"}))[0] == CHECK_OFF
    assert CHECK_COL


def test_cloud_upload_progress_labels():
    assert cloud_upload_progress({}) == "未推送"
    assert cloud_upload_progress({"updated_at": "2026-08-01T10:00:00"}).startswith("未推送")
    assert "已推送" in cloud_upload_progress(
        {"cloud_updated_at": "2026-08-01T12:00:00", "updated_at": "2026-08-01T11:00:00"}
    )
    assert "待推送" in cloud_upload_progress(
        {"cloud_updated_at": "2026-08-01T10:00:00", "updated_at": "2026-08-01T12:00:00"}
    )


def test_panel_mode_is_fixed():
    """岗位审核 / 异常队列 / 岗位显示各自固定 mode，不再页内切换。"""
    import inspect

    from app.ui.job_review import JobReviewPanel

    src = inspect.getsource(JobReviewPanel._build)
    assert "CTkSegmentedButton" not in src
    assert "岗位审核" in src or "异常队列" in src or "岗位显示" in src
    actions = inspect.getsource(JobReviewPanel._rebuild_actions)
    assert "重新识别" in actions
    assert "岗位去重" in actions
    assert "校园招聘识别" in actions
    assert "信息时效性识别" in actions
    assert "自动审核" in actions
    assert "推送云端" in actions
    assert "反审核" in actions
    assert "check_timeliness_selected" in inspect.getsource(JobReviewPanel)
    assert "strip_jd_duplicates_selected" in inspect.getsource(JobReviewPanel)
    assert "deduplicate_selected" in inspect.getsource(JobReviewPanel)
    assert "campus_recognition_selected" in inspect.getsource(JobReviewPanel)
    assert "auto_approve_normal" in inspect.getsource(JobReviewPanel)
    # 重新识别：勾选结果交给独立进程逐条写入，而非在 Tk 线程集中落库
    re_src = inspect.getsource(JobReviewPanel._reidentify_single_resolved)
    assert "pick_job_candidates" in re_src
    assert "_start_reidentify_process" in re_src
    assert "serialize_candidate" in re_src
    assert '"mode": "selected"' in re_src

    from app.collector.reidentify_worker import _selected

    worker_src = inspect.getsource(_selected)
    assert 'merged["status"] = "pending_review"' in worker_src
    assert '"item"' in worker_src
    assert "inserted_n" in worker_src and "updated_n" in worker_src
    tip_src = inspect.getsource(JobReviewPanel._build)
    assert "每批最多约 50" in tip_src or "未入库岗" in tip_src
    assert "新增到岗位审核" in tip_src or "勾选将新增" in tip_src
    sig = inspect.signature(JobReviewPanel.__init__)
    assert "mode" in sig.parameters


def test_display_group_and_type_and_jd_sections():
    # 不再从公司名截取「…集团」；需入库字段或门户种子+子公司
    assert display_group_name({"company": "中国五矿集团有限公司"}) == ""
    assert display_group_name({"group_name": "某某集团", "company": "子公司"}) == "某某集团"
    assert display_group_name({"company": "字节跳动"}) == ""
    assert display_job_type({"raw_category": "技术管理类", "recruit_project": "校园招聘"}) == "校招"
    assert display_job_type({"recruit_bucket": "校招"}) == "校招"
    assert display_job_type({"recruit_project": "暑期实习"}) == "日常实习"
    duties, reqs = display_jd_sections(
        "工作职责：\n做研发\n\n任职要求：\n硕士学历\n"
    )
    assert "研发" in duties
    assert "硕士" in reqs
    duties2, reqs2 = display_jd_sections("岗位要求：\n熟悉 Python\n\n任职资格：\n本科及以上\n")
    assert "Python" in duties2
    assert "本科" in reqs2
    duties_en, reqs_en = display_jd_sections(
        "Responsibilities:\nCreate seasonal graphics\n\n"
        "Skills:\nAdobe Illustrator\n\n"
        "Requirements:\nDesign major\n"
    )
    assert "seasonal graphics" in duties_en
    assert "Illustrator" in reqs_en
    assert "Design major" in reqs_en
    duties_syn, reqs_syn = display_jd_sections(
        "一、主要职责：\n负责模型训练和服务部署。\n\n"
        "二、你需要具备：\n本科及以上学历，熟悉 Python。\n\n"
        "福利待遇：\n年度体检、餐补。"
    )
    assert "模型训练" in duties_syn
    assert "本科及以上" in reqs_syn
    assert "年度体检" not in reqs_syn
    row = review_row_values(
        {
            "company": "海天集团股份有限公司",
            "title": "研发工程师",
            "salary_range": "面议",
                "headcount": "若干",
                "recruit_bucket": "校招",
                "raw_category": "技术类",
            "education": "硕士",
            "work_location": "佛山",
            "source_url": "https://example.com/j",
            "open_at": "2026-07-15",
            "updated_at": "2026-08-01T12:00:00",
            "jd_text": "岗位职责：编码\n任职要求：3年经验",
        }
    )
    assert row[0] == ""
    assert row[1] == "海天集团股份有限公司"
    assert row[2] == "研发工程师"
    assert row[3] == "面议"
    assert row[4] == "若干"
    assert row[5] == "校招"
    assert "编码" in row[6]
    assert "3年" in row[7]
    assert row[8] == "硕士"
    assert row[9] == "佛山"
    assert row[10] == "https://example.com/j"
    # 「岗位发布时间」用页面 open_at，不用本机 updated_at
    assert row[11].startswith("2026-07-15")
    # 「链接」列优先网申/申请链
    prefer_apply = review_row_values(
        {
            "company": "A",
            "title": "T",
            "apply_url": "https://ats.example.com/apply/1",
            "source_url": "https://campus.example.com/school",
        }
    )
    assert prefer_apply[10] == "https://ats.example.com/apply/1"
    empty_meta = review_row_values({"company": "A", "title": "T"})
    assert empty_meta[3] == "-"
    assert empty_meta[4] == "-"
    # 仅有本机戳、无页面日期 → 显示 -
    local_only = review_row_values(
        {
            "company": "A",
            "title": "T",
            "updated_at": "2026-08-01T12:00:00",
            "created_at": "2026-08-01T11:00:00",
        }
    )
    assert local_only[11] == "-"


def test_push_requires_config_message():
    sync = SupabaseSync("", "", "")
    with pytest.raises(RuntimeError, match="未配置 Supabase"):
        sync.require_write_config()

    sync2 = SupabaseSync("https://example.supabase.co", "anon-key", "")
    with pytest.raises(RuntimeError, match="Service Role Key"):
        sync2.require_write_config()


def test_publish_local_jobs_for_sync_empty_db(tmp_path: Path):
    db = LocalDB(tmp_path / "p.db")
    sync = SupabaseSync("https://example.supabase.co", "anon", "service")
    # 不发起真实 HTTP：jobs 为空时直接返回 0，不访问网络
    result = sync.publish_local_jobs_for_sync(db)
    assert result["pushed"] == 0
    assert result["active"] == 0
    assert result["deleted"] == 0


def test_publish_includes_deleted_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = LocalDB(tmp_path / "d.db")
    db.upsert_job(
        {
            "id": "j1",
            "company": "A",
            "title": "T1",
            "source_url": "https://example.com/1",
            "status": "active",
        }
    )
    db.upsert_job(
        {
            "id": "j2",
            "company": "B",
            "title": "T2",
            "source_url": "https://example.com/2",
            "status": "active",
        }
    )
    assert db.soft_delete_jobs(["j2"]) == 1

    captured: list[list[dict]] = []

    def fake_push(self, jobs, *, chunk_size=200):  # noqa: ANN001
        captured.append(list(jobs))
        return len(jobs)

    monkeypatch.setattr(SupabaseSync, "push_jobs", fake_push)
    sync = SupabaseSync("https://example.supabase.co", "anon", "service")
    result = sync.publish_local_jobs_for_sync(db)
    assert result["pushed"] == 2
    assert result["active"] == 1
    assert result["deleted"] == 1
    by_id = {j["id"]: j["status"] for j in captured[0]}
    assert by_id["j1"] == "active"
    assert by_id["j2"] == "deleted"
