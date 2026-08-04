"""管理员端 Admin：种子导入、采集、异常队列、日报、应急粘贴。"""

from __future__ import annotations

import multiprocessing as mp
import threading
from datetime import datetime
from pathlib import Path
from tkinter import ttk, messagebox, filedialog, simpledialog
from typing import Any

import customtkinter as ctk

from app.collector.fill_from_url import (
    FillCandidate,
    FillFromUrlResult,
    apply_reidentify_fields,
    discover_portal_jobs_from_url,
    enrich_candidate_detail,
    fields_look_like_no_job_posting,
    pick_fill_url,
    resolve_jobs_from_url,
)
from app.collector.import_xlsx import import_xlsx_paths_to_companies
from app.collector.cleanup import cleanup_expired_jobs
from app.collector.deep_worker import drain_deep_messages, start_deep_collect_process
from app.collector.nightly import NightlyScheduler
from app.collector.pipeline import (
    run_collect_pipeline,
)
from app.config import APP_VERSION, load_config, resolve_seed_xlsx_paths, save_config
from app.db.local import LocalDB
from app.sync.supabase import SupabaseSync
from app.ui.account_admin import AccountAdminPanel
from app.ui.job_display import JobDisplayPanel
from app.ui.job_editor import JobEditForm, merge_job_fields, pick_job_candidates
from app.ui.job_review import JobReviewPanel
from app.ui.tree_check import (
    CHECK_COL,
    bind_check_column_click,
    setup_check_column,
    sync_tree_checks,
    with_check,
)
from app.ui.tree_sort import TreeSortController, company_cluster_key

# 「清除当前所有数据」确认口令（本机 Admin 门禁；勿写入对外 README）
CLEAR_ALL_DATA_PASSWORD = "+A1838406637zjh"


class AdminApp(ctk.CTk):
    def __init__(self, db: LocalDB | None = None) -> None:
        super().__init__()
        self.db = db or LocalDB()
        self.cfg = load_config()
        self.sync = SupabaseSync(
            self.cfg.get("supabase_url", ""),
            self.cfg.get("supabase_anon_key", ""),
            self.cfg.get("supabase_service_role_key", ""),
        )
        self.title(f"校招投递表 Admin v{APP_VERSION}")
        self.geometry("1180x780")
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("dark-blue")

        self.log_var = ctk.StringVar(value="Admin 就绪。注意：xlsx 种子只进 companies，不直接发布旧岗。")
        self.deep_progress_var = ctk.StringVar(value="深度采集进度：未开始")
        self._deep_running = False
        self._deep_progress_scheduled = False
        self._deep_progress_pending = ""
        self._deep_progress_at = 0.0
        self._deep_process: mp.Process | None = None
        self._deep_message_queue: Any | None = None
        self._deep_result_received = False
        self._deep_error_received = False
        self._deep_exit_poll_count = 0
        self._source_edit_id: str | None = None
        self._source_items: dict[str, dict[str, Any]] = {}
        self._source_sort = TreeSortController(
            sortable={
                "company": (
                    "公司",
                    lambda it: company_cluster_key(it.get("company_name") or it.get("company")),
                ),
                "source_value": (
                    "源 URL / 值",
                    lambda it: str(it.get("source_value") or "").lower(),
                ),
                "created_at": (
                    "时间",
                    lambda it: str(it.get("created_at") or ""),
                ),
            },
            default_col="company",
            default_ascending=True,
            tie_breakers=[
                lambda it: company_cluster_key(it.get("company_name") or it.get("company")),
                lambda it: str(it.get("source_value") or "").lower(),
                lambda it: str(it.get("created_at") or ""),
            ],
        )
        self._nightly = NightlyScheduler(
            self.db,
            enabled=bool(self.cfg.get("nightly_enabled", True)),
            hour=int(self.cfg.get("nightly_hour", 2) if self.cfg.get("nightly_hour") is not None else 2),
            minute=int(self.cfg.get("nightly_minute", 0) or 0),
            lookback_days=int(self.cfg.get("lookback_days", 90) or 90),
            limit_companies=int(self.cfg.get("nightly_limit_companies") or 0),
            on_log=lambda m: self.after(0, lambda msg=m: self.log(msg)),
            get_config=load_config,
        )
        self._build()
        self.refresh_all()
        self._nightly.start()
        self._run_retention_startup()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self) -> None:
        tabs = ctk.CTkTabview(self)
        tabs.pack(fill="both", expand=True, padx=8, pady=8)
        self.tabs = tabs
        self.tab_dash = tabs.add("日报/操作")
        self.tab_jobs = tabs.add("岗位审核")
        self.tab_display = tabs.add("岗位显示")
        self.tab_review = tabs.add("异常队列")
        self.tab_source = tabs.add("源验证")
        self.tab_paste = tabs.add("应急粘贴")
        self.tab_accounts = tabs.add("账号管理")
        self.tab_cfg = tabs.add("配置")

        self._build_dash()
        self._build_jobs()
        self._build_display()
        self._build_review()
        self._build_source()
        self._build_paste()
        self._build_accounts()
        self._build_cfg()

        bottom = ctk.CTkFrame(self)
        bottom.pack(fill="x", padx=8, pady=4)
        ctk.CTkLabel(bottom, textvariable=self.log_var, anchor="w").pack(fill="x")

    def _build_dash(self) -> None:
        f = self.tab_dash
        row = ctk.CTkFrame(f)
        row.pack(fill="x", padx=8, pady=(8, 8))
        ctk.CTkButton(row, text="导入xlsx种子", command=self.import_xlsx).pack(side="left", padx=4)
        ctk.CTkButton(row, text="快速采集测试", command=self.run_pipeline).pack(side="left", padx=4)
        ctk.CTkButton(
            row, text="深度采集（近3个月）", command=self.run_deep_pipeline, fg_color="#1f6aa5"
        ).pack(side="left", padx=4)
        ctk.CTkButton(row, text="中断当前任务", command=self.cancel_deep_pipeline).pack(
            side="left", padx=4
        )
        ctk.CTkButton(row, text="夜间复检", command=self.run_nightly_now).pack(side="left", padx=4)
        ctk.CTkButton(
            row, text="清除当前所有数据", command=self.clear_all_data, fg_color="#a33"
        ).pack(side="left", padx=4)

        self.stats_label = ctk.CTkLabel(f, text="", justify="left", anchor="w")
        self.stats_label.pack(fill="x", padx=12, pady=4)
        self.scan_time_var = ctk.StringVar(value="最近监测：尚未执行")
        self.scan_time_label = ctk.CTkLabel(
            f, textvariable=self.scan_time_var, justify="left", anchor="w", text_color="#0a7"
        )
        self.scan_time_label.pack(fill="x", padx=12, pady=2)
        self.deep_progress_label = ctk.CTkLabel(
            f, textvariable=self.deep_progress_var, justify="left", anchor="w", text_color="#1f6aa5"
        )
        self.deep_progress_label.pack(fill="x", padx=12, pady=2)

        ctk.CTkLabel(f, text="最近日报", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=12)
        self.digest_box = ctk.CTkTextbox(f, height=280)
        self.digest_box.pack(fill="both", expand=True, padx=12, pady=8)
        tip = (
            "xlsx 种子只进 companies，不直接发布旧岗。"
            "配置「种子 xlsx 路径列表」后点「导入xlsx种子」会同时导入春招/秋招。"
            "新采正常岗→「岗位审核」→审核后进「岗位显示」；推送云端在「岗位显示」；"
            "内容/解析异常→「异常队列」；官网是否可信→「源验证」。"
        )
        ctk.CTkLabel(f, text=tip, justify="left", text_color="#666", wraplength=1000).pack(
            anchor="w", padx=12, pady=(0, 6)
        )

    def _build_jobs(self) -> None:
        """岗位审核：待人工确认的正常岗（pending_review）。"""
        self.job_review_panel = JobReviewPanel(
            self.tab_jobs,
            self.db,
            on_log=self.log,
            on_changed=self._on_jobs_changed,
            mode="normal",
        )
        self.job_review_panel.pack(fill="both", expand=True)

    def _build_display(self) -> None:
        """岗位显示：已审核 active，可推云端。"""
        self.job_display_panel = JobDisplayPanel(
            self.tab_display,
            self.db,
            on_log=self.log,
            on_changed=self._on_jobs_changed,
            on_push_cloud=self.push_cloud,
        )
        self.job_display_panel.pack(fill="both", expand=True)

    def _build_review(self) -> None:
        """异常队列：仅岗位内容/规则异常（与岗位审核、源验证职责分离）。"""
        tip = ctk.CTkLabel(
            self.tab_review,
            text="异常队列＝岗位解析/内容有问题（缺字段、届别不对、过期、社招误标等）。官网是否可信请到「源验证」；待确认正常岗请到「岗位审核」；已通过请到「岗位显示」。",
            text_color="#666",
            anchor="w",
        )
        tip.pack(fill="x", padx=10, pady=(6, 0))
        self.review_panel = JobReviewPanel(
            self.tab_review,
            self.db,
            on_log=self.log,
            on_changed=self._on_jobs_changed,
            mode="abnormal",
        )
        self.review_panel.pack(fill="both", expand=True)

    def _on_jobs_changed(self) -> None:
        n_co = self.db.count_companies()
        n_pending = self.db.count_jobs(status="pending_review")
        n_display = self.db.count_jobs(status="active")
        n_rev = len(self.db.list_review_queue("pending", limit=5000))
        n_src = len(self.db.list_source_verify("pending", limit=5000))
        self.stats_label.configure(
            text=(
                f"企业种子 {n_co} | 待审核 {n_pending} | 岗位显示 {n_display} | "
                f"异常队列 {n_rev} | 源验证 {n_src}"
            )
        )
        # 同步另一页签列表（面板自身已 refresh，这里补刷兄弟面板）
        if getattr(self, "_syncing_job_panels", False):
            return
        self._syncing_job_panels = True
        try:
            if hasattr(self, "job_review_panel"):
                self.job_review_panel.refresh()
            if hasattr(self, "job_display_panel"):
                self.job_display_panel.refresh()
            if hasattr(self, "review_panel"):
                self.review_panel.refresh()
        finally:
            self._syncing_job_panels = False

    def _build_source(self) -> None:
        f = self.tab_source
        btns = ctk.CTkFrame(f)
        btns.pack(fill="x", padx=8, pady=6)
        ctk.CTkButton(btns, text="刷新", command=self.refresh_source).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="全选", width=64, command=self.select_all_sources).pack(
            side="left", padx=4
        )
        ctk.CTkButton(btns, text="取消全选", width=80, command=self.clear_source_selection).pack(
            side="left", padx=2
        )
        ctk.CTkButton(btns, text="标为官方", command=lambda: self.resolve_source("official")).pack(
            side="left", padx=4
        )
        ctk.CTkButton(
            btns,
            text="全部待验标官方并加入采集",
            command=self.batch_mark_official_sources,
        ).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="拒绝", command=lambda: self.resolve_source("rejected")).pack(
            side="left", padx=4
        )
        ctk.CTkButton(btns, text="复制选中", command=self.copy_source_selected).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="复制链接", command=self.copy_source_link).pack(side="left", padx=4)

        body = ctk.CTkFrame(f)
        body.pack(fill="both", expand=True, padx=8, pady=4)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self.source_tree = ttk.Treeview(
            left,
            columns=(
                CHECK_COL,
                "company",
                "source_type",
                "source_value",
                "reason",
                "created_at",
            ),
            show="headings",
            height=20,
            selectmode="extended",
        )
        setup_check_column(self.source_tree)
        _src_cols = (
            ("company", 120, "公司"),
            ("source_type", 70, "类型"),
            ("source_value", 280, "源 URL / 值"),
            ("reason", 160, "原因"),
            ("created_at", 120, "时间"),
        )
        for c, w, t in _src_cols:
            self.source_tree.column(c, width=w)
            if c in self._source_sort.sortable:
                self.source_tree.heading(
                    c,
                    text=t,
                    command=lambda col=c: self._on_source_sort_heading(col),
                )
            else:
                self.source_tree.heading(c, text=t)
        self._source_sort.apply_heading_labels(
            self.source_tree,
            all_cols=[(c, t) for c, _w, t in _src_cols],
            on_sorted=self.refresh_source,
        )
        yscroll = ttk.Scrollbar(left, orient="vertical", command=self.source_tree.yview)
        self.source_tree.configure(yscrollcommand=yscroll.set)
        self.source_tree.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")
        self.source_tree.bind("<<TreeviewSelect>>", self._on_source_select)
        bind_check_column_click(self.source_tree, on_after=self._on_source_select)
        self.source_tree.bind("<Control-c>", self._on_source_ctrl_c)
        self.source_tree.bind("<Control-C>", self._on_source_ctrl_c)
        self.source_tree.bind("<Double-1>", self._on_source_double_click)

        right = ctk.CTkFrame(body)
        right.grid(row=0, column=1, sticky="nsew")
        ctk.CTkLabel(right, text="编辑选中项", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=6, pady=(6, 2)
        )
        form = ctk.CTkScrollableFrame(right)
        form.pack(fill="both", expand=True, padx=4, pady=2)

        self.source_meta_var = ctk.StringVar(value="点击左侧一行进行编辑；多选时编辑首项。")
        ctk.CTkLabel(form, textvariable=self.source_meta_var, anchor="w", wraplength=320).pack(
            fill="x", padx=4, pady=(2, 6)
        )

        ctk.CTkLabel(form, text="源 URL / 值", anchor="w").pack(fill="x", padx=4)
        self.source_value_entry = ctk.CTkEntry(form, placeholder_text="https://...")
        self.source_value_entry.pack(fill="x", padx=4, pady=(0, 2))
        src_url_row = ctk.CTkFrame(form)
        src_url_row.pack(fill="x", padx=4, pady=(0, 6))
        ctk.CTkButton(
            src_url_row,
            text="从链接识别预览",
            width=130,
            command=self.preview_source_url_jobs,
        ).pack(side="left", padx=0)
        self.source_parse_status = ctk.CTkLabel(
            src_url_row, text="", anchor="w", text_color="#666"
        )
        self.source_parse_status.pack(side="left", padx=8)

        ctk.CTkLabel(form, text="源类型 source_type", anchor="w").pack(fill="x", padx=4)
        self.source_type_entry = ctk.CTkEntry(form, placeholder_text="url / wechat / …")
        self.source_type_entry.pack(fill="x", padx=4, pady=(0, 6))

        ctk.CTkLabel(form, text="原因 / 备注 reason", anchor="w").pack(fill="x", padx=4)
        self.source_reason_box = ctk.CTkTextbox(form, height=80)
        self.source_reason_box.pack(fill="x", padx=4, pady=(0, 6))

        ctk.CTkLabel(form, text="关联公司名（只读）", anchor="w").pack(fill="x", padx=4)
        self.source_company_name_var = ctk.StringVar(value="—")
        ctk.CTkLabel(form, textvariable=self.source_company_name_var, anchor="w").pack(
            fill="x", padx=4, pady=(0, 6)
        )

        ctk.CTkLabel(
            form,
            text="公司 ID（谨慎修改；留空表示解除关联）",
            anchor="w",
        ).pack(fill="x", padx=4)
        self.source_company_id_entry = ctk.CTkEntry(form, placeholder_text="companies.id")
        self.source_company_id_entry.pack(fill="x", padx=4, pady=(0, 6))

        ctk.CTkLabel(form, text="公司展示备注（写入 companies.notes）", anchor="w").pack(
            fill="x", padx=4
        )
        self.source_company_notes_box = ctk.CTkTextbox(form, height=60)
        self.source_company_notes_box.pack(fill="x", padx=4, pady=(0, 6))

        # 保留只读详情区，便于多选时对照/复制
        ctk.CTkLabel(form, text="选中摘要（可复制）", anchor="w").pack(fill="x", padx=4, pady=(4, 0))
        self.source_detail = ctk.CTkTextbox(form, height=120)
        self.source_detail.pack(fill="both", expand=True, padx=4, pady=4)
        self.source_detail.insert("1.0", "点击左侧行查看详情；Ctrl+C 或按钮可复制。")

        save_row = ctk.CTkFrame(right)
        save_row.pack(fill="x", padx=6, pady=(2, 6))
        ctk.CTkButton(save_row, text="保存修改", command=self.save_source_verify).pack(
            side="left", padx=2
        )

        tip = ctk.CTkLabel(
            f,
            text=(
                "源验证＝确认招聘源是否官方官网（与岗位内容是否解析成功无关）。"
                "岗位解析/规则失败请到「异常队列」。"
                "点首列勾选后，「标为官方 / 拒绝」会对全部选中行批量执行；表头可排序。"
            ),
            text_color="#666",
            anchor="w",
        )
        tip.pack(fill="x", padx=10, pady=(0, 4))

    def _build_paste(self) -> None:
        f = self.tab_paste
        f.grid_columnconfigure(0, weight=1)
        f.grid_rowconfigure(3, weight=1)

        ctk.CTkLabel(
            f,
            text="应急粘贴：粘贴官方岗位/公告 URL，走与「岗位审核」相同的识别链路（适配器 + 标签抽取 + 可选 OCR）",
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 2))

        url_row = ctk.CTkFrame(f)
        url_row.grid(row=1, column=0, sticky="ew", padx=12, pady=4)
        url_row.grid_columnconfigure(0, weight=1)
        self.url_entry = ctk.CTkEntry(
            url_row, placeholder_text="https://… 岗位详情或列表页"
        )
        self.url_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(
            url_row, text="从链接识别", width=120, command=self.recognize_paste_url
        ).grid(row=0, column=1, padx=2)
        ctk.CTkButton(
            url_row, text="清空表单", width=90, command=self.clear_paste_form
        ).grid(row=0, column=2, padx=2)

        tip = ctk.CTkLabel(
            f,
            text=(
                "识别后字段与岗位审核侧栏一致，可直接改；确认无误后点「保存到岗位审核」。"
                "列表页会弹出选岗。OCR 遵循配置 ocr_enabled。"
            ),
            text_color="#666",
            anchor="w",
            justify="left",
        )
        tip.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 4))

        # JobEditForm 自身可滚，勿再套一层 ScrollableFrame
        self.paste_form = JobEditForm(f)
        self.paste_form.grid(row=3, column=0, sticky="nsew", padx=8, pady=4)
        self.paste_form.set_meta("粘贴 URL → 从链接识别 → 核对字段 → 保存到岗位审核")

        btn_row = ctk.CTkFrame(f)
        btn_row.grid(row=4, column=0, sticky="ew", padx=12, pady=8)
        ctk.CTkButton(
            btn_row,
            text="保存到岗位审核",
            fg_color="#1f6aa5",
            command=self.save_paste_job,
        ).pack(side="left", padx=4)
        self.paste_status = ctk.CTkLabel(btn_row, text="", anchor="w", text_color="#666")
        self.paste_status.pack(side="left", padx=8, fill="x", expand=True)
        self._paste_busy = False
        self._paste_base: dict[str, Any] = {}

    def _build_accounts(self) -> None:
        AccountAdminPanel(
            self.tab_accounts,
            self.db,
            self.sync,
            on_log=self.log,
        ).pack(fill="both", expand=True)

    def _build_cfg(self) -> None:
        f = self.tab_cfg
        self.cfg_entries: dict[str, ctk.CTkEntry] = {}
        fields = [
            ("supabase_url", "Supabase URL"),
            ("supabase_anon_key", "Anon Key（同学端）"),
            ("supabase_service_role_key", "Service Role Key（仅本机 Admin）"),
            ("rss_bridge_base", "公众号 RSS 桥接基址"),
            ("seed_xlsx_path", "默认种子 xlsx 路径（兼容单项）"),
            ("sync_interval_minutes", "同学端同步间隔(分钟)"),
            ("collect_months", "深度采集月数（默认3）"),
            ("lookback_days", "时间窗天数（默认90，优先于月数）"),
            ("list_collect_months", "列表翻页月数（默认6）"),
            ("retention_days", "岗位保留天数（默认365，超期软删）"),
            ("deep_collect_batch_size", "深度采集每批公司数（建议≤10）"),
            ("deep_collect_max_companies", "深度采集默认企业数（启动时可修改）"),
            ("per_company_collect_batch", "单企业每轮最多新岗数（默认50）"),
            ("deep_collect_concurrency", "深度采集并发（本地库固定为1）"),
            ("deep_collect_max_company_batches", "单企业自动续采批数上限（默认10）"),
            ("deep_collect_parse_supplement_limit", "单企业解析补全上限（默认5，0=关闭）"),
            ("deep_collect_parse_supplement_timeout", "单条解析补全超时秒数（默认20）"),
            ("company_nature_lookup_enabled", "企业性质网络补全（true/false）"),
            ("company_nature_lookup_timeout", "企业性质检索超时秒数（默认5）"),
            ("company_nature_lookup_max_calls", "每次深度采集最多检索企业数（默认2）"),
            ("reidentify_collect_batch", "局部重新识别每批岗位数（默认15）"),
            ("reidentify_detail_timeout", "局部重新识别单条超时秒数（默认45）"),
            ("nightly_enabled", "夜间复检开关（true/false）"),
            ("nightly_hour", "夜间复检小时（0-23，默认2）"),
            ("nightly_minute", "夜间复检分钟（默认0）"),
            ("nightly_limit_companies", "夜间复检公司数上限（0=全量校招+实习）"),
        ]
        for key, label in fields:
            ctk.CTkLabel(f, text=label).pack(anchor="w", padx=12, pady=(8, 0))
            e = ctk.CTkEntry(f, width=860)
            e.pack(anchor="w", padx=12)
            e.insert(0, str(self.cfg.get(key) or ""))
            self.cfg_entries[key] = e

        ctk.CTkLabel(f, text="种子 xlsx 路径列表（每行一个，春招/秋招可同时配置）").pack(
            anchor="w", padx=12, pady=(10, 0)
        )
        self.seed_paths_box = ctk.CTkTextbox(f, height=90, width=860)
        self.seed_paths_box.pack(anchor="w", padx=12, pady=4)
        paths = self.cfg.get("seed_xlsx_paths") or []
        if isinstance(paths, list) and paths:
            self.seed_paths_box.insert("1.0", "\n".join(str(p) for p in paths))
        elif self.cfg.get("seed_xlsx_path"):
            self.seed_paths_box.insert("1.0", str(self.cfg.get("seed_xlsx_path")))

        cfg_actions = ctk.CTkFrame(f, fg_color="transparent")
        cfg_actions.pack(anchor="w", padx=12, pady=12)
        ctk.CTkButton(cfg_actions, text="保存配置", command=self.save_cfg).pack(
            side="left", padx=(0, 8)
        )
        ctk.CTkButton(
            cfg_actions,
            text="立即备份数据库",
            command=self.backup_database_now,
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            cfg_actions,
            text="检查数据库",
            command=self.check_database_integrity,
        ).pack(side="left", padx=4)
        backup_text = f"自动备份目录：{self.db.backup_dir}（每日一份，保留 14 份）"
        if self.db.last_backup_error:
            backup_text += f"\n最近自动备份失败：{self.db.last_backup_error}"
        self.backup_status = ctk.CTkLabel(
            f,
            text=backup_text,
            justify="left",
            text_color="#666",
        )
        self.backup_status.pack(anchor="w", padx=12, pady=(0, 6))
        tip = (
            "密钥保存在本机 %LOCALAPPDATA%/campus-jobs/config.json，不要提交到 git。\n"
            "Viewer 打包勿带入 service_role_key。\n"
            "导入时会合并「路径列表」与「默认路径」并去重。\n"
            "异常口径：最新信息发布时间超过约3个月；「夜间复检」按钮始终全量扫校招+实习；"
            "定时任务公司数上限填 0 表示全量。"
        )
        ctk.CTkLabel(f, text=tip, justify="left", text_color="#666").pack(anchor="w", padx=12)

    def log(self, msg: str) -> None:
        self.log_var.set(msg)

    def backup_database_now(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.db.backup_dir / f"{self.db.path.stem}-manual-{stamp}.db"
        try:
            saved = self.db.backup_to(target)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("备份失败", str(exc), parent=self)
            self.log(f"数据库备份失败：{exc}")
            return
        self.log(f"数据库已备份：{saved}")
        messagebox.showinfo("备份完成", f"数据库快照已保存：\n{saved}", parent=self)

    def check_database_integrity(self) -> None:
        try:
            result = self.db.integrity_check()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("检查失败", str(exc), parent=self)
            self.log(f"数据库检查失败：{exc}")
            return
        if result.lower() == "ok":
            messagebox.showinfo("检查完成", "数据库完整性正常。", parent=self)
            self.log("数据库完整性检查：正常")
        else:
            messagebox.showerror("数据库异常", result, parent=self)
            self.log(f"数据库完整性检查异常：{result}")

    def _on_close(self) -> None:
        try:
            self._nightly.stop()
        except Exception:
            pass
        if self._deep_process and self._deep_process.is_alive():
            try:
                self.db.request_deep_collect_cancel()
            except Exception:
                pass
        self.destroy()

    def run_nightly_now(self) -> None:
        self.log("正在启动夜间复检（校招+实习全量频道，后台）…")
        self._nightly.run_now_async()

    def _format_scan_time(self) -> str:
        finished = self.db.get_meta("last_scan_finished_at") or "—"
        mode = self.db.get_meta("last_scan_mode") or "—"
        nightly = self.db.get_meta("last_nightly_finished_at")
        nightly_status = self.db.get_meta("last_nightly_status")
        nightly_error = self.db.get_meta("last_nightly_error")
        deep = self.db.get_meta("deep_collect_last_progress")
        parts = [f"最近监测完成：{finished}", f"模式：{mode}"]
        if nightly:
            parts.append(f"夜间复检：{nightly}")
        if nightly_status:
            status_text = {"running": "运行中", "success": "成功", "failed": "失败"}.get(
                nightly_status, nightly_status
            )
            parts.append(f"夜间状态：{status_text}")
        if nightly_status == "failed" and nightly_error:
            parts.append(f"夜间错误：{nightly_error[:160]}")
        if deep:
            parts.append(f"深度进度：{deep.split('|')[0]}")
        return " ｜ ".join(parts)

    def refresh_all(self) -> None:
        n_co = self.db.count_companies()
        n_pending = self.db.count_jobs(status="pending_review")
        n_display = self.db.count_jobs(status="active")
        n_rev = len(self.db.list_review_queue("pending", limit=5000))
        n_src = len(self.db.list_source_verify("pending", limit=5000))
        self.stats_label.configure(
            text=(
                f"企业种子 {n_co} | 待审核 {n_pending} | 岗位显示 {n_display} | "
                f"异常队列 {n_rev} | 源验证 {n_src}"
            )
        )
        if hasattr(self, "scan_time_var"):
            self.scan_time_var.set(self._format_scan_time())
        self.digest_box.delete("1.0", "end")
        digests = self.db.latest_digests(30)
        if not digests:
            self.digest_box.insert("1.0", "暂无日报。导入种子后运行采集流水线即可生成。")
        else:
            self.digest_box.insert("1.0", "\n\n".join(d["summary"] for d in digests))
        self.refresh_review()
        self.refresh_source()

    def promote_interns(self) -> None:
        from app.collector.promote_intern import promote_internship_from_review

        if not messagebox.askyesno(
            "实习岗转正常",
            "将异常队列中带「实习」信号的条目自动发布为正常岗位，是否继续？",
        ):
            return
        result = promote_internship_from_review(self.db)
        self.refresh_all()
        messagebox.showinfo("完成", result.get("text") or "完成")
        self.log(result.get("text") or "实习岗转正常完成")

    def clear_review_queue(self) -> None:
        n = len(self.db.list_review_queue("pending", limit=5000))
        if n <= 0:
            messagebox.showinfo("提示", "异常队列已为空")
            return
        if not messagebox.askyesno(
            "清空异常队列",
            f"将删除异常队列中 {n} 条待审记录（不会删除已发布的正常岗位）。\n"
            "建议先点「实习岗转正常」再清空。是否继续？",
        ):
            return
        deleted = self.db.clear_review_queue("pending")
        self.refresh_all()
        msg = f"已清空异常队列 {deleted} 条"
        self.log(msg)
        messagebox.showinfo("完成", msg)

    def refresh_review(self) -> None:
        if hasattr(self, "job_review_panel"):
            self.job_review_panel.refresh()
        if hasattr(self, "job_display_panel"):
            self.job_display_panel.refresh()
        if hasattr(self, "review_panel"):
            self.review_panel.refresh()

    def _on_source_sort_heading(self, col: str) -> None:
        self._source_sort.toggle(col)
        self.refresh_source()

    def refresh_source(self) -> None:
        prev_sel = list(self.source_tree.selection())
        for i in self.source_tree.get_children():
            self.source_tree.delete(i)
        self._source_items = {}
        keep_id = self._source_edit_id
        self._clear_source_edit_form()
        rows: list[dict[str, Any]] = []
        for item in self.db.list_source_verify("pending", limit=500):
            company_name = ""
            company_notes = ""
            cid = item.get("company_id")
            if cid:
                co = self.db.get_company(cid)
                if co:
                    company_name = co.get("name") or ""
                    company_notes = co.get("notes") or ""
            enriched = dict(item)
            enriched["company_name"] = company_name
            enriched["company_notes"] = company_notes
            self._source_items[item["id"]] = enriched
            rows.append(enriched)
        rows = self._source_sort.sort_items(rows)
        for enriched in rows:
            self.source_tree.insert(
                "",
                "end",
                iid=enriched["id"],
                values=with_check(
                    (
                        enriched.get("company_name") or "",
                        enriched.get("source_type") or "",
                        enriched.get("source_value") or "",
                        enriched.get("reason") or "",
                        enriched.get("created_at") or "",
                    )
                ),
            )
        self._source_sort.apply_heading_labels(
            self.source_tree,
            all_cols=[
                ("company", "公司"),
                ("source_type", "类型"),
                ("source_value", "源 URL / 值"),
                ("reason", "原因"),
                ("created_at", "时间"),
            ],
            on_sorted=self.refresh_source,
        )
        sync_tree_checks(self.source_tree)
        keep = [i for i in prev_sel if self.source_tree.exists(i)]
        if keep:
            self.source_tree.selection_set(keep)
        elif keep_id and keep_id in self._source_items:
            self.source_tree.selection_set(keep_id)
            self.source_tree.see(keep_id)
            self._on_source_select()

    def select_all_sources(self) -> None:
        kids = self.source_tree.get_children()
        if kids:
            self.source_tree.selection_set(kids)
        self._on_source_select()

    def clear_source_selection(self) -> None:
        self.source_tree.selection_remove(self.source_tree.selection())
        self._clear_source_edit_form()
        sync_tree_checks(self.source_tree)

    def _clear_source_edit_form(self) -> None:
        self._source_edit_id = None
        if hasattr(self, "source_meta_var"):
            self.source_meta_var.set("点击左侧一行进行编辑；多选时编辑首项。")
        if hasattr(self, "source_company_name_var"):
            self.source_company_name_var.set("—")
        for entry_name in (
            "source_value_entry",
            "source_type_entry",
            "source_company_id_entry",
        ):
            entry = getattr(self, entry_name, None)
            if entry is not None:
                entry.delete(0, "end")
        for box_name in ("source_reason_box", "source_company_notes_box", "source_detail"):
            box = getattr(self, box_name, None)
            if box is not None:
                box.delete("1.0", "end")
        if hasattr(self, "source_detail"):
            self.source_detail.insert("1.0", "点击左侧行查看详情；Ctrl+C 或按钮可复制。")

    def _fill_source_edit_form(self, item_id: str) -> None:
        item = self._source_items.get(item_id) or {}
        self._source_edit_id = item_id
        created = item.get("created_at") or ""
        self.source_meta_var.set(f"正在编辑 ID：{item_id}\n创建时间：{created}")
        self.source_company_name_var.set(item.get("company_name") or "—")

        self.source_value_entry.delete(0, "end")
        self.source_value_entry.insert(0, item.get("source_value") or "")
        self.source_type_entry.delete(0, "end")
        self.source_type_entry.insert(0, item.get("source_type") or "")
        self.source_company_id_entry.delete(0, "end")
        self.source_company_id_entry.insert(0, item.get("company_id") or "")

        self.source_reason_box.delete("1.0", "end")
        self.source_reason_box.insert("1.0", item.get("reason") or "")
        self.source_company_notes_box.delete("1.0", "end")
        self.source_company_notes_box.insert("1.0", item.get("company_notes") or "")

    def _on_source_select(self, _event: Any = None) -> None:
        sync_tree_checks(self.source_tree)
        sel = self.source_tree.selection()
        if not hasattr(self, "source_detail"):
            return
        if not sel:
            self._clear_source_edit_form()
            return
        self._fill_source_edit_form(sel[0])
        texts = [self._format_source_item(iid) for iid in sel]
        self.source_detail.delete("1.0", "end")
        self.source_detail.insert("1.0", "\n---\n".join(texts))

    def _format_source_item(self, item_id: str) -> str:
        item = self._source_items.get(item_id) or {}
        return (
            f"公司：{item.get('company_name') or '—'}\n"
            f"公司ID：{item.get('company_id') or '—'}\n"
            f"类型：{item.get('source_type') or ''}\n"
            f"源 URL / 值：{item.get('source_value') or ''}\n"
            f"原因：{item.get('reason') or ''}\n"
            f"公司备注：{item.get('company_notes') or ''}\n"
            f"时间：{item.get('created_at') or ''}\n"
            f"ID：{item_id}"
        )

    def preview_source_url_jobs(self) -> None:
        """源验证：复用同一解析，预览链接下识别到的岗位（不写岗位表）。"""
        url = (self.source_value_entry.get() or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            messagebox.showwarning("无法识别", "请先填写有效的 http(s) 源 URL")
            return
        company = ""
        if hasattr(self, "source_company_name_var"):
            company = (self.source_company_name_var.get() or "").strip()
            if company == "—":
                company = ""
        self.source_parse_status.configure(text="识别中…")

        def worker() -> None:
            result = resolve_jobs_from_url(url, keep_company=company or None)

            def done() -> None:
                if not result.ok:
                    self.source_parse_status.configure(text="")
                    messagebox.showerror("识别失败", result.error or "未能识别岗位")
                    return
                lines = [
                    f"适配器：{result.adapter or '—'}",
                    f"共识别 {result.total} 个岗位"
                    + ("（列表页）" if result.is_list or result.total > 1 else "（详情页）"),
                    "",
                ]
                for i, cand in enumerate(result.candidates[:30], 1):
                    f = cand.fields
                    lines.append(
                        f"{i}. {f.get('title') or '（无标题）'}"
                        f"  |  {f.get('recruit_bucket') or f.get('recruit_project') or ''}"
                        f"  |  {f.get('work_location') or ''}"
                        f"  |  {f.get('graduation_batch') or ''}"
                    )
                    if f.get("apply_url"):
                        lines.append(f"   网申：{f['apply_url']}")
                if result.total > 30:
                    lines.append(f"… 另有 {result.total - 30} 条未展示")
                lines.append("")
                lines.append("说明：此为预览，不会写入岗位。岗位审核侧栏可用「从链接识别填充」回填表单。")
                text = "\n".join(lines)
                if hasattr(self, "source_detail"):
                    self.source_detail.delete("1.0", "end")
                    self.source_detail.insert("1.0", text)
                self.source_parse_status.configure(text=f"已识别 {result.total} 个")
                messagebox.showinfo("识别预览", f"共识别 {result.total} 个岗位，详情见右侧摘要区。")

            self.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def save_source_verify(self) -> None:
        item_id = self._source_edit_id
        if not item_id:
            messagebox.showinfo("提示", "请先在左侧选择一条源验证记录")
            return
        if item_id not in self._source_items:
            messagebox.showerror("保存失败", "该记录已不在待验证列表中，请刷新后重试")
            return

        source_value = self.source_value_entry.get().strip()
        source_type = self.source_type_entry.get().strip() or "url"
        reason = self.source_reason_box.get("1.0", "end").strip()
        new_company_id = self.source_company_id_entry.get().strip()
        company_notes = self.source_company_notes_box.get("1.0", "end").strip()

        if not source_value:
            messagebox.showwarning("保存失败", "源 URL / 值不能为空")
            return

        old = self._source_items.get(item_id) or {}
        old_company_id = (old.get("company_id") or "").strip()

        if new_company_id and new_company_id != old_company_id:
            if self.db.get_company(new_company_id) is None:
                messagebox.showerror(
                    "保存失败",
                    f"公司 ID 不存在：{new_company_id}\n请核对后再改，或留空解除关联。",
                )
                return
            if not messagebox.askyesno(
                "确认修改公司 ID",
                f"将把关联公司从\n  {old_company_id or '（无）'}\n改为\n  {new_company_id}\n\n是否继续？",
            ):
                return
        elif not new_company_id and old_company_id:
            if not messagebox.askyesno(
                "确认解除关联",
                f"将解除与公司 {old_company_id} 的关联（company_id 置空）。是否继续？",
            ):
                return

        try:
            old_source_value = (old.get("source_value") or "").strip()
            ok = self.db.update_source_verify(
                item_id,
                source_value=source_value,
                source_type=source_type,
                reason=reason,
                company_id=new_company_id or None,
                update_company_id=True,
            )
            if not ok:
                messagebox.showerror("保存失败", "未找到该记录，可能已被处理或删除")
                return

            seed_synced = False
            seed_company_id = new_company_id or old_company_id
            if seed_company_id and source_value != old_source_value:
                sync = self.db.sync_company_seed_urls(
                    seed_company_id,
                    replacements=[(old_source_value or None, source_value)],
                )
                seed_synced = bool(sync.get("synced"))

            notes_target = new_company_id or old_company_id
            if notes_target:
                # 仅当有关联公司时写入展示备注；解除关联时跳过
                if new_company_id:
                    self.db.update_company_notes(new_company_id, company_notes)
                elif company_notes != (old.get("company_notes") or ""):
                    # 用户改了备注但又清空了 company_id：提示未写入
                    messagebox.showwarning(
                        "部分保存",
                        "源验证字段已保存，但已解除公司关联，公司展示备注未写入。",
                    )
                    self.log(f"源验证已保存（部分）：{item_id}")
                    self.refresh_source()
                    return

            msg = "修改已写入本地数据库，刷新后仍可看到。"
            if seed_synced:
                msg += "\n已同步更新公司种子链接"
                self.log(f"源验证已保存：{item_id}；已同步更新公司种子链接")
            else:
                self.log(f"源验证已保存：{item_id}")
            messagebox.showinfo("已保存", msg)
            self.refresh_source()
        except Exception as e:
            messagebox.showerror("保存失败", f"写入本地数据库失败：{e}")

    def _clipboard_set(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            messagebox.showinfo("提示", "没有可复制的内容")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()
        self.log(f"已复制到剪贴板（{len(text)} 字）")

    def copy_source_selected(self) -> None:
        sel = self.source_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择要复制的源验证项")
            return
        self._clipboard_set("\n---\n".join(self._format_source_item(i) for i in sel))

    def copy_source_link(self) -> None:
        sel = self.source_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择一行")
            return
        links: list[str] = []
        for iid in sel:
            item = self._source_items.get(iid) or {}
            val = (item.get("source_value") or "").strip()
            if val:
                links.append(val)
        self._clipboard_set("\n".join(links))

    def _on_source_ctrl_c(self, _event: Any = None) -> str | None:
        self.copy_source_selected()
        return "break"

    def _on_source_double_click(self, _event: Any = None) -> None:
        self.copy_source_link()

    def import_xlsx(self, limit: int | None = None, *, pick_files: bool = False) -> None:
        """导入配置中的全部种子表；或手动多选 xlsx。只写 companies。"""
        configured = resolve_seed_xlsx_paths(self.cfg)
        paths: list[str] = []
        if pick_files or not configured:
            initial = configured[0] if configured else (self.cfg.get("seed_xlsx_path") or "")
            selected = filedialog.askopenfilenames(
                title="选择种子 xlsx（可多选：春招版 + 秋招版）",
                filetypes=[("Excel", "*.xlsx")],
                initialdir=str(Path(initial).parent) if initial else None,
            )
            paths = [str(p) for p in selected]
            if not paths:
                return
            # 记住本次选择
            self.cfg["seed_xlsx_paths"] = paths
            self.cfg["seed_xlsx_path"] = paths[0]
            save_config(self.cfg)
            if hasattr(self, "seed_paths_box"):
                self.seed_paths_box.delete("1.0", "end")
                self.seed_paths_box.insert("1.0", "\n".join(paths))
            if "seed_xlsx_path" in self.cfg_entries:
                self.cfg_entries["seed_xlsx_path"].delete(0, "end")
                self.cfg_entries["seed_xlsx_path"].insert(0, paths[0])
        else:
            missing = [p for p in configured if not Path(p).exists()]
            if missing:
                tip = "以下配置路径不存在：\n" + "\n".join(missing)
                if not messagebox.askyesno(
                    "路径缺失",
                    tip + "\n\n是 = 改选手动多选文件\n否 = 取消",
                ):
                    return
                return self.import_xlsx(limit=limit, pick_files=True)
            names = "\n".join(f"· {Path(p).name}" for p in configured)
            if not messagebox.askyesno(
                "导入xlsx种子",
                f"将导入以下 {len(configured)} 个文件（仅 companies，不发布旧岗）：\n{names}\n\n继续？",
            ):
                return
            paths = configured

        def worker() -> None:
            self.log(f"正在导入 {len(paths)} 个种子文件（仅 companies）…")
            try:
                result = import_xlsx_paths_to_companies(
                    self.db,
                    paths,
                    limit_per_sheet=limit,
                    progress=lambda m: self.after(0, lambda msg=m: self.log(msg)),
                )
                msg = "；".join(result.summary_lines())
                self.after(0, lambda m=msg: self.log(m))
                self.after(0, lambda r=result: messagebox.showinfo("导入完成", "\n".join(r.summary_lines())))
                self.after(0, self.refresh_all)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.after(0, lambda m=err: messagebox.showerror("导入失败", m))

        threading.Thread(target=worker, daemon=True).start()

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.cfg.get(key) or default)
        except (TypeError, ValueError):
            return default

    def run_pipeline(self) -> None:
        self.log("正在运行【快速采集测试】…（每轮约几十家，非全量）")

        def worker() -> None:
            try:
                result = run_collect_pipeline(
                    self.db,
                    bridge_base=self.cfg.get("rss_bridge_base") or "",
                    lookback_days=self._cfg_int("lookback_days", 90),
                    collect_months=self._cfg_int("collect_months", 3),
                    retention_days=self._cfg_int("retention_days", 365),
                    progress=lambda m: self.after(0, lambda msg=m: self.log(msg)),
                )
                self.after(0, lambda r=result: self._pipeline_done(r))
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.after(0, lambda m=err: self._pipeline_failed(m))

        threading.Thread(target=worker, daemon=True).start()

    def run_deep_pipeline(self) -> None:
        if self._deep_running:
            messagebox.showinfo("提示", "深度采集正在进行中。可点「中断当前任务」后等待当前分片结束。")
            return
        months = self._cfg_int("collect_months", 3)
        days = self._cfg_int("lookback_days", 90)
        per_company_batch = self._cfg_int("per_company_collect_batch", 50)
        configured_company_limit = self._cfg_int("deep_collect_max_companies", 20)
        resume = True
        reset = False
        run_id = self.db.get_meta("deep_collect_run_id")
        if run_id:
            prog = self.db.collect_queue_progress(run_id)
            if prog.get("pending", 0) > 0 or prog.get("running", 0) > 0:
                choice = messagebox.askyesnocancel(
                    "深度采集（近3个月）",
                    f"检测到未完成队列：已处理 {prog.get('processed', 0)}/{prog.get('total', 0)}，"
                    f"待处理 {prog.get('pending', 0)}。\n\n"
                    f"是 = 续跑未完成\n否 = 重新建队从头扫\n取消 = 不开始\n\n"
                    f"时间窗：近 {days} 天（约 {months} 个月）",
                )
                if choice is None:
                    return
                resume = bool(choice)
                reset = not choice
            else:
                if not messagebox.askyesno(
                    "深度采集（近3个月）",
                    f"将扫完所有带 career/hint URL 的公司（近 {days} 天），"
                    f"单企业每批最多 {per_company_batch} 个并自动续采，可中断续跑。\n\n开始？",
                ):
                    return
                reset = True
                resume = False
        else:
            if not messagebox.askyesno(
                "深度采集（近3个月）",
                f"将扫完所有带 career/hint URL 的公司（近 {days} 天），"
                f"单企业每批最多 {per_company_batch} 个并自动续采，可中断续跑。\n"
                f"与「快速采集测试」不同，不会卡在 40 家。\n\n开始？",
            ):
                return

        company_limit = simpledialog.askinteger(
            "深度采集范围",
            "本次最多采集多少家企业？\n请输入 1-5000；取消则不启动。",
            initialvalue=max(1, min(configured_company_limit or 20, 5000)),
            minvalue=1,
            maxvalue=5000,
            parent=self,
        )
        if company_limit is None:
            return

        self._deep_running = True
        self.deep_progress_var.set("深度采集进度：启动中…")
        self.log("正在运行【深度采集（近3个月）】…")

        options = {
            "lookback_days": days,
            "collect_months": months,
            "list_collect_months": self._cfg_int("list_collect_months", 6),
            "retention_days": self._cfg_int("retention_days", 365),
            "batch_size": self._cfg_int("deep_collect_batch_size", 10),
            "concurrency": 1,
            "max_retries": self._cfg_int("deep_collect_max_retries", 2),
            "max_company_batches": self._cfg_int("deep_collect_max_company_batches", 10),
            # Existing resumable queues retain their original company set.
            "max_companies": company_limit if (reset or not run_id) else None,
            "resume": resume,
            "reset": reset,
            "parse_supplement_limit": self._cfg_int("deep_collect_parse_supplement_limit", 5),
            "parse_supplement_timeout": self._cfg_int("deep_collect_parse_supplement_timeout", 20),
        }
        try:
            process, message_queue = start_deep_collect_process(self.db.path, options)
        except Exception as exc:  # noqa: BLE001
            self._pipeline_failed(str(exc))
            return
        self._deep_process = process
        self._deep_message_queue = message_queue
        self._deep_result_received = False
        self._deep_error_received = False
        self._deep_exit_poll_count = 0
        self.after(100, self._poll_deep_process)

    def _poll_deep_process(self) -> None:
        process = self._deep_process
        message_queue = self._deep_message_queue
        if process is None or message_queue is None:
            return
        saw_finished = False
        for kind, payload in drain_deep_messages(message_queue, limit=200):
            if kind == "progress":
                self._schedule_deep_progress(str(payload))
            elif kind == "result" and isinstance(payload, dict):
                self._deep_result_received = True
                self._deep_pipeline_done(payload)
            elif kind == "error":
                self._deep_error_received = True
                if isinstance(payload, dict):
                    self._pipeline_failed(str(payload.get("message") or "深度采集子进程失败"))
                else:
                    self._pipeline_failed(str(payload))
            elif kind == "finished":
                saw_finished = True

        if process.is_alive() and not saw_finished:
            self.after(150, self._poll_deep_process)
            return
        process.join(timeout=0.1)
        if (
            not self._deep_result_received
            and not self._deep_error_received
            and not saw_finished
            and self._deep_exit_poll_count < 3
        ):
            # multiprocessing.Queue may flush a fraction later than process exit.
            self._deep_exit_poll_count += 1
            self.after(100, self._poll_deep_process)
            return
        if not self._deep_result_received and not self._deep_error_received:
            self._pipeline_failed(f"深度采集进程异常结束（退出码 {process.exitcode}）")
        self._deep_finished_flag()
        try:
            message_queue.close()
        except Exception:
            pass
        self._deep_process = None
        self._deep_message_queue = None

    def _schedule_deep_progress(self, msg: str) -> None:
        """合并已进入 Tk 主线程的进度消息。"""
        self._deep_progress_pending = msg
        if self._deep_progress_scheduled:
            return
        self._deep_progress_scheduled = True
        self.after_idle(self._flush_deep_progress)

    def _flush_deep_progress(self) -> None:
        self._deep_progress_scheduled = False
        msg = self._deep_progress_pending or ""
        if msg:
            self._on_deep_progress(msg)

    def _deep_finished_flag(self) -> None:
        self._deep_running = False
        self._deep_progress_scheduled = False

    def _on_deep_progress(self, msg: str) -> None:
        # 节流：避免刷日志；进度条用最新消息（含公司名与跳过数）
        import time

        now = time.monotonic()
        last = getattr(self, "_deep_progress_at", 0.0)
        force = (
            "完成" in msg
            or "中断" in msg
            or "失败" in msg
            or msg.startswith("深度采集进度：")
        )
        self.deep_progress_var.set(msg[:160] if len(msg) > 160 else msg)
        if not force and (now - last) < 1.5:
            return
        self._deep_progress_at = now
        self.log(msg)

    def cancel_deep_pipeline(self) -> None:
        self.db.request_deep_collect_cancel()
        self.log("已请求中断当前任务（当前分片结束后停止，可再次点击续跑）")
        self.deep_progress_var.set(self.deep_progress_var.get() + "（中断中…）")

    def _pipeline_failed(self, err: str) -> None:
        self._deep_running = False
        self.log(f"采集失败：{err}")
        messagebox.showerror("采集失败", err)

    def _pipeline_done(self, result: dict[str, Any]) -> None:
        text = result.get("text") or str(result)
        self.log(text)
        self.refresh_all()
        tip = (
            f"模式：快速采集测试（非全量）\n"
            f"时间窗：近 {result.get('lookback_days', 90)} 天\n"
            f"自动标记 official：{result.get('promoted', 0)}\n"
            f"探测：{result.get('discovered', 0)} 家（本轮新官方 {result.get('new_official', 0)}）\n"
            f"当前 official 总数：{result.get('official_total', 0)}\n"
            f"官网：解析 {result.get('web', {}).get('parsed', 0)} / "
            f"发布 {result.get('web', {}).get('published', 0)} / "
            f"进队列 {result.get('web', {}).get('queued', 0)}\n"
            f"RSS：解析 {result.get('rss', {}).get('parsed', 0)} / "
            f"发布 {result.get('rss', {}).get('published', 0)}\n"
            f"岗位新增：{result.get('jobs_added', 0)}（本地共 {result.get('jobs_after', 0)}）\n"
            f"待验证源：{result.get('source_pending', 0)}；待审异常：{result.get('review_pending', 0)}\n\n"
            f"要扫近 3 个月全量请用「深度采集（近3个月）」。"
        )
        if result.get("jobs_added", 0) > 0:
            messagebox.showinfo("快速采集测试完成", tip)
        else:
            messagebox.showwarning(
                "快速采集测试完成（未新增岗位）",
                tip
                + "\n\n若仍为 0：请到「源验证」批量标为官方，或确认种子网申 URL 可访问。",
            )

    def _deep_pipeline_done(self, result: dict[str, Any]) -> None:
        text = result.get("text") or str(result)
        self.log(text)
        self.refresh_all()
        p = result.get("progress") or {}
        proc = int(result.get("companies_processed", 0) or 0)
        total = int(result.get("companies_total", 0) or 0)
        from app.collector.deep import format_deep_progress

        self.deep_progress_var.set(
            format_deep_progress(proc, total)
            + f"｜新增 {result.get('jobs_added', 0)} 岗｜"
            + ("已中断可续跑" if result.get("cancelled") else "已结束")
        )
        tip = (
            f"模式：深度采集（近 {result.get('lookback_days', 90)} 天）\n"
            f"公司：{proc}/{total}\n"
            f"done {p.get('done', 0)} / error {p.get('error', 0)} / skip {p.get('skipped', 0)}\n"
            f"官网发布 {result.get('web', {}).get('published', 0)} / "
            f"进队列 {result.get('web', {}).get('queued', 0)} / "
            f"过期跳过 {result.get('web', {}).get('stale', 0)}\n"
            f"企业自动续采：共 {result.get('web', {}).get('company_batches', 0)} 批"
            f"（触及安全上限 {result.get('web', {}).get('continuation_limited', 0)} 家）\n"
            f"岗位新增：{result.get('jobs_added', 0)}（本地共 {result.get('jobs_after', 0)}）\n"
            f"{'已中断：再次点击「深度采集（近3个月）」可续跑。' if result.get('cancelled') else '本轮已结束。'}\n\n"
            f"建议：到「岗位显示」点「推送云端」同步 deleted。"
        )
        if result.get("cancelled"):
            messagebox.showwarning("深度采集已中断", tip)
        elif result.get("jobs_added", 0) > 0:
            messagebox.showinfo("深度采集完成", tip)
        else:
            messagebox.showwarning("深度采集完成（未新增岗位）", tip)

    def clear_all_data(self) -> None:
        """清除本地岗位及相关审核/日报/采集数据；需口令确认。不改云端配置与企业种子。"""
        pwd = simpledialog.askstring(
            "清除当前所有数据",
            "将清空本地岗位、投递状态、异常队列、源验证队列、日报与采集队列。\n"
            "保留企业种子（companies）与 Supabase 配置。\n\n"
            "请输入确认密码：",
            show="*",
            parent=self,
        )
        if pwd is None:
            return
        if pwd != CLEAR_ALL_DATA_PASSWORD:
            messagebox.showerror("已拒绝", "密码错误或为空，未清除任何数据。")
            self.log("清除当前所有数据：口令校验失败，已拒绝")
            return
        try:
            result = self.db.clear_all_job_related_data()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("清除失败", str(exc))
            self.log(f"清除当前所有数据失败：{exc}")
            return
        self.refresh_all()
        self.deep_progress_var.set("深度采集进度：未开始")
        msg = (
            f"已清除本地数据：岗位 {result.get('jobs', 0)}，"
            f"异常队列 {result.get('review_queue', 0)}，"
            f"源验证 {result.get('source_verify_queue', 0)}，"
            f"日报 {result.get('digest_log', 0)}，"
            f"采集队列 {result.get('collect_queue', 0)}。"
        )
        self.log(msg)
        messagebox.showinfo("清除完成", msg)

    def _reload_sync_from_config(self) -> None:
        """推送前从已保存配置刷新 sync，避免改配置未点保存或内存过期。"""
        self.cfg = load_config()
        self.sync = SupabaseSync(
            self.cfg.get("supabase_url", ""),
            self.cfg.get("supabase_anon_key", ""),
            self.cfg.get("supabase_service_role_key", ""),
        )

    def push_cloud(self, job_ids: list[str] | None = None) -> None:
        """从「岗位显示」推送 active(+deleted) 到 Supabase；job_ids 非空则只推选中。"""
        self._reload_sync_from_config()
        try:
            self.sync.require_write_config()
        except Exception as exc:  # noqa: BLE001
            self._push_cloud_failed(str(exc))
            return

        n_co = self.db.count_companies()
        n_deleted = self.db.count_jobs(status="deleted")
        if job_ids:
            selected = [
                j
                for j in self.db.get_jobs_by_ids(job_ids)
                if (j.get("status") or "") == "active"
            ]
            n_active = len(selected)
            scope = f"选中 active {n_active}"
        else:
            n_active = self.db.count_jobs(status="active")
            scope = f"全部 active {n_active}"
        if n_active + n_deleted <= 0:
            tip = (
                "没有可推送的岗位（「岗位显示」active / 软删 deleted 均为 0）。\n\n"
                f"当前：企业种子 {n_co}；待审核 {self.db.count_jobs('pending_review')}。\n"
                "xlsx 种子只写入 companies，不会产生岗位。\n"
                "「推送云端」只推「岗位显示」中的 jobs（含软删），不推待审核岗，也不单独推 companies。\n\n"
                "请先采集 → 在「岗位审核」点审核 → 再到本页推送。"
            )
            self.log("推送未执行：岗位显示为空，请先审核通过")
            messagebox.showwarning("无可推送岗位", tip)
            return

        self.log(f"正在推送云端…（{scope} + deleted {n_deleted}）")
        ids = list(job_ids) if job_ids else None

        def worker() -> None:
            try:
                result = self.sync.publish_local_jobs_for_sync(self.db, job_ids=ids)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.after(0, lambda m=err: self._push_cloud_failed(m))
                return
            self.after(0, lambda r=result, co=n_co: self._push_cloud_done(r, co))

        threading.Thread(target=worker, daemon=True).start()

    def _push_cloud_failed(self, err: str) -> None:
        self.log(f"推送失败：{err}")
        messagebox.showerror("推送失败", err)

    def _push_cloud_done(self, result: dict[str, int], n_companies: int) -> None:
        pushed = int(result.get("pushed") or 0)
        n_active = int(result.get("active") or 0)
        n_deleted = int(result.get("deleted") or 0)
        if hasattr(self, "job_display_panel"):
            self.job_display_panel.refresh()
        if pushed == 0:
            tip = (
                "配置已通过校验，但本次推送条数为 0。\n\n"
                f"企业种子 {n_companies}；本地 active {n_active}，deleted {n_deleted}。\n"
                "请确认「岗位显示」是否确有岗位，或稍后重试。"
            )
            self.log("推送结果：0 条")
            messagebox.showwarning("推送结果为 0", tip)
            return
        n_revoked = int(result.get("revoked") or 0)
        msg = (
            f"已推送 {pushed} 条岗位到 Supabase\n"
            f"（岗位显示 active {n_active}，软删 deleted {n_deleted}"
            + (f"，反审核撤云 {n_revoked}" if n_revoked else "")
            + "；同 id 以 deleted 为准）"
        )
        self.log(msg.replace("\n", " "))
        messagebox.showinfo("推送成功", msg)

    def _source_items_by_ids(self, ids: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
        """按当前列表缓存 / 库取出源验证项（保持选中顺序）。"""
        out: list[dict[str, Any]] = []
        pending_map = {
            x["id"]: x for x in self.db.list_source_verify("pending", limit=5000)
        }
        for iid in ids:
            item = self._source_items.get(iid) or pending_map.get(iid)
            if item:
                out.append(item)
        return out

    def _apply_source_resolve(self, item: dict[str, Any], status: str) -> bool:
        """单条标官方 / 拒绝；返回是否写库成功。"""
        item_id = item.get("id")
        if not item_id:
            return False
        if item.get("company_id"):
            if status == "official" and item.get("source_value"):
                self.db.sync_company_seed_urls(
                    item["company_id"],
                    add_urls=[item["source_value"]],
                    set_official=True,
                )
            else:
                self.db.update_company_verify(item["company_id"], status)
        self.db.resolve_source_verify(item_id, status)
        return True

    def resolve_source(self, status: str) -> None:
        """对**全部选中**源验证项批量标为官方或拒绝（不再只处理第一行）。"""
        sel = list(self.source_tree.selection())
        if not sel:
            messagebox.showinfo("提示", "请先勾选要处理的源验证项（可多选）")
            return
        items = self._source_items_by_ids(sel)
        if not items:
            messagebox.showwarning("提示", "选中项已不在待验证列表，请刷新后重试")
            return
        label = "标为官方" if status == "official" else "拒绝"
        if not messagebox.askyesno(
            label,
            f"将对选中的 {len(items)} 条执行「{label}」。继续？",
        ):
            return
        n_ok = 0
        for item in items:
            try:
                if self._apply_source_resolve(item, status):
                    n_ok += 1
            except Exception as exc:  # noqa: BLE001
                self.log(f"源验证 {label} 失败 {item.get('id')}: {exc}")
        self.log(f"源验证批量{label}：成功 {n_ok}/{len(items)} 条")
        if status == "official" and n_ok:
            messagebox.showinfo(
                "已标记",
                f"已将 {n_ok} 条标为官方（含种子链接同步）。\n"
                "可回到「日报/操作」运行深度采集或快速采集测试。",
            )
        elif status == "rejected" and n_ok:
            messagebox.showinfo("已拒绝", f"已拒绝 {n_ok} 条源验证项。")
        self.refresh_source()
        self.refresh_all()

    def batch_mark_official_sources(self) -> None:
        """将当前全部待验证（或仅选中）中带有效 URL 的源标为 official。"""
        pending = self.db.list_source_verify("pending", limit=2000)
        if not pending:
            messagebox.showinfo("提示", "没有待验证源")
            return
        selected = set(self.source_tree.selection())
        if selected:
            items = [x for x in pending if x["id"] in selected]
            scope = f"选中的 {len(items)} 条"
        else:
            items = pending
            scope = f"全部待验证 {len(items)} 条"
        usable = [
            x
            for x in items
            if (x.get("source_value") or "").startswith("http") and x.get("company_id")
        ]
        if not usable:
            messagebox.showwarning("提示", "没有带有效网申 URL 的待验证项可标记")
            return
        if not messagebox.askyesno(
            "标为官方并加入采集",
            f"范围：{scope}\n将把其中 {len(usable)} 条带 URL 的源标为 official 并加入采集。继续？",
        ):
            return
        for item in usable:
            self._apply_source_resolve(item, "official")
        self.log(
            f"已批量标记 official：{len(usable)} 条；已同步更新公司种子链接。"
            "请再运行深度采集或快速采集测试"
        )
        messagebox.showinfo(
            "已标记",
            f"已将 {len(usable)} 条标为官方，并同步更新公司种子链接。\n"
            "请回到「日报/操作」点击「深度采集（近3个月）」或「快速采集测试」。",
        )
        self.refresh_all()

    def clear_paste_form(self) -> None:
        self._paste_base = {}
        self.paste_form.clear()
        self.paste_form.set_meta("粘贴 URL → 从链接识别 → 核对字段 → 保存到岗位审核")
        self.paste_status.configure(text="")

    def recognize_paste_url(self) -> None:
        """应急粘贴：与岗位审核「从链接识别填充」同一路径（fetch + enrich + OCR 配置）。"""
        if self._paste_busy:
            return
        url = (self.url_entry.get() or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            # 若顶部为空，尝试表单里的链接
            vals = self.paste_form.values()
            url = pick_fill_url(vals.get("apply_url"), vals.get("source_url")) or ""
        if not (url.startswith("http://") or url.startswith("https://")):
            messagebox.showwarning("无法识别", "请先粘贴有效的 http(s) 岗位/公告链接")
            return
        # 预填原文链接，便于表单内再次点「从链接识别填充」
        cur = self.paste_form.values()
        if not (cur.get("source_url") or "").strip():
            self.paste_form._write_entry("source_url", url)
        keep_company = (self.paste_form.values().get("company") or "").strip() or None
        self._paste_busy = True
        self.paste_status.configure(text="正在打开链接并识别…")
        self.log("应急粘贴识别中…")

        def worker() -> None:
            try:
                months = self.cfg.get("list_collect_months")
                from app.collector.fill_from_url import (
                    per_company_batch_size,
                    scan_limit_for_batch,
                )

                batch_n = per_company_batch_size(self.cfg)
                result = discover_portal_jobs_from_url(
                    url,
                    fetch=True,
                    keep_company=keep_company,
                    list_collect_months=int(months) if months else None,
                    list_limit=scan_limit_for_batch(batch_n),
                    timeout=90.0,
                )
            except Exception as exc:  # noqa: BLE001
                result = FillFromUrlResult(ok=False, error=f"识别失败：{exc}", page_url=url)
            self.after(
                0,
                lambda: self._paste_on_resolved(result, keep_company=keep_company),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _paste_on_resolved(
        self, result: FillFromUrlResult, *, keep_company: str | None
    ) -> None:
        if not result.ok or not result.candidates:
            self._paste_busy = False
            self.paste_status.configure(text="")
            messagebox.showerror("识别失败", result.error or "未能识别岗位信息")
            return
        from app.collector.fill_from_url import (
            per_company_batch_size,
            take_collect_batch,
        )

        batch_n = per_company_batch_size(self.cfg)
        candidates, batch_stats = take_collect_batch(
            list(result.candidates),
            known_urls=set(),
            known_titles=set(),
            batch_size=batch_n,
        )
        if not candidates:
            self._paste_busy = False
            self.paste_status.configure(text="本批无岗位")
            messagebox.showinfo("应急粘贴", "本批无岗位可勾选。")
            return
        remain = int(batch_stats.get("remaining_after_batch") or 0)
        self.paste_status.configure(
            text=f"本批 {len(candidates)}/{batch_n} 个，约剩 {remain} 可下次再采…"
        )
        chosen_list = pick_job_candidates(
            self,
            candidates,
            title="应急粘贴 · 勾选岗位",
            preselect=[0] if len(candidates) == 1 else None,
            hint=(
                f"本批最多 {batch_n} 个未入库岗（已列出 {len(candidates)}）；"
                f"约剩 {remain} 个请下次再识别。\n"
                "请勾选要写入「岗位审核」的条目；未勾选的不会导入。"
            ),
        )
        if chosen_list is None:
            self._paste_busy = False
            self.paste_status.configure(text="已取消选择")
            return
        if not chosen_list:
            self._paste_busy = False
            self.paste_status.configure(text="未勾选任何岗位")
            return

        list_hint = (
            f"共识别 {len(candidates)} 个，已勾选 {len(chosen_list)} 个"
            "（仅写入勾选项）。"
        )
        self.paste_status.configure(text="正在抓取岗位详情…")
        page_url = result.page_url or ""

        def enrich_worker() -> None:
            enriched: list[FillCandidate] = []
            for cand in chosen_list:
                try:
                    enriched.append(
                        enrich_candidate_detail(
                            cand,
                            page_url=page_url
                            or pick_fill_url(
                                cand.fields.get("apply_url"),
                                cand.fields.get("source_url"),
                            ),
                            keep_company=keep_company,
                        )
                    )
                except Exception:
                    enriched.append(cand)
            self.after(
                0,
                lambda: self._paste_on_enriched(enriched, hint=list_hint, keep_company=keep_company),
            )

        threading.Thread(target=enrich_worker, daemon=True).start()

    def _paste_on_enriched(
        self,
        chosen: list[FillCandidate] | FillCandidate,
        *,
        hint: str,
        keep_company: str | None = None,
    ) -> None:
        items = chosen if isinstance(chosen, list) else [chosen]
        if not items:
            self._paste_busy = False
            self.paste_status.configure(text="未勾选任何岗位")
            return

        top = (self.url_entry.get() or "").strip()
        written = 0
        primary_fields: dict[str, str] | None = None
        for cand in items:
            fields = dict(cand.fields or {})
            if top and not (fields.get("source_url") or "").strip():
                fields["source_url"] = top
            if fields_look_like_no_job_posting(fields):
                continue
            base: dict[str, Any] = {
                "company": fields.get("company") or keep_company or "未知企业",
                "status": "pending_review",
            }
            merged = apply_reidentify_fields(
                base, fields, seed_company_name=keep_company
            )
            merged.pop("id", None)
            merged["status"] = "pending_review"
            try:
                jid = self.db.upsert_job(merged)
                fresh = self.db.get_job(jid) or merged
                self.db.sync_seed_urls_from_job_fields(base, fresh)
                written += 1
                if primary_fields is None:
                    primary_fields = {
                        k: str(fresh.get(k) or fields.get(k) or "")
                        for k in (
                            "group_name",
                            "company",
                            "title",
                            "salary_range",
                            "headcount",
                            "graduation_batch",
                            "recruit_project",
                            "recruit_bucket",
                            "work_location",
                            "education",
                            "deadline",
                            "source_url",
                            "apply_url",
                            "jd_text",
                        )
                    }
            except Exception as exc:  # noqa: BLE001
                self._paste_busy = False
                messagebox.showerror("写入失败", str(exc))
                return

        if primary_fields is None:
            self._paste_busy = False
            self.paste_status.configure(text="勾选项无有效岗位")
            messagebox.showwarning("应急粘贴", "勾选项均无有效岗位信息，未写入。")
            return

        self._paste_base = dict(primary_fields)
        self.paste_form.load(primary_fields)
        self.paste_form._ensure_entries_editable()
        meta = (
            f"已将勾选 {written} 条写入「岗位审核」；表单显示第 1 条，可再改后保存"
        )
        self.paste_form.set_meta(meta)
        self._paste_busy = False
        msg = f"{hint} 已写入岗位审核 {written} 条。"
        self.paste_status.configure(text=msg)
        self.log(msg)
        self.refresh_all()
        messagebox.showinfo("应急粘贴", msg)

    def save_paste_job(self) -> None:
        """将表单写入 pending_review（岗位审核），并同步公司种子链接。"""
        edited = self.paste_form.values()
        if not (edited.get("title") or "").strip():
            messagebox.showwarning("校验失败", "岗位名称为必填")
            return
        if not (edited.get("source_url") or "").strip():
            # 顶部 URL 兜底
            top = (self.url_entry.get() or "").strip()
            if top.startswith("http://") or top.startswith("https://"):
                edited["source_url"] = top
                self.paste_form._write_entry("source_url", top)
            else:
                messagebox.showwarning("校验失败", "原文/公告链接为必填")
                return
        before = dict(self._paste_base or {})
        job = merge_job_fields(before, edited)
        job["status"] = "pending_review"
        try:
            jid = self.db.upsert_job(job)
            fresh = self.db.get_job(jid) or job
            sync = self.db.sync_seed_urls_from_job_fields(before, fresh)
            self._paste_base = dict(fresh)
            self.paste_form.load(fresh)
            self.paste_form._ensure_entries_editable()
            seed_msg = "，并已同步公司种子链接" if sync.get("synced") else ""
            msg = f"已写入「岗位审核」（待确认）{seed_msg}"
            self.paste_status.configure(text=msg)
            self.log(msg)
            self.refresh_all()
            messagebox.showinfo("成功", msg)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc))

    def save_cfg(self) -> None:
        int_keys = {
            "sync_interval_minutes": 30,
            "collect_months": 3,
            "lookback_days": 90,
            "list_collect_months": 6,
            "retention_days": 365,
            "deep_collect_batch_size": 10,
            "deep_collect_concurrency": 1,
            "deep_collect_max_retries": 2,
            "deep_collect_max_company_batches": 10,
            "deep_collect_parse_supplement_limit": 5,
            "deep_collect_parse_supplement_timeout": 20,
            "company_nature_lookup_timeout": 5,
            "company_nature_lookup_max_calls": 2,
            "reidentify_collect_batch": 15,
            "reidentify_detail_timeout": 45,
            "per_company_collect_batch": 50,
            "nightly_hour": 2,
            "nightly_minute": 0,
            "nightly_limit_companies": 0,
        }
        for key, entry in self.cfg_entries.items():
            val = entry.get().strip()
            if key in ("nightly_enabled", "company_nature_lookup_enabled"):
                self.cfg[key] = val.lower() in ("1", "true", "yes", "on", "是")
            elif key in int_keys:
                try:
                    self.cfg[key] = int(val or str(int_keys[key]))
                except ValueError:
                    self.cfg[key] = int_keys[key]
            else:
                self.cfg[key] = val
        # 多路径种子列表：每行一个
        raw_paths = self.seed_paths_box.get("1.0", "end")
        path_list = [p.strip() for p in raw_paths.replace(";", "\n").splitlines() if p.strip()]
        self.cfg["seed_xlsx_paths"] = path_list
        if path_list and not (self.cfg.get("seed_xlsx_path") or "").strip():
            self.cfg["seed_xlsx_path"] = path_list[0]
        self.cfg["mode"] = "admin"
        save_config(self.cfg)
        self.sync = SupabaseSync(
            self.cfg.get("supabase_url", ""),
            self.cfg.get("supabase_anon_key", ""),
            self.cfg.get("supabase_service_role_key", ""),
        )
        # 刷新夜间调度参数
        self._nightly.update_schedule(
            enabled=bool(self.cfg.get("nightly_enabled", True)),
            hour=self.cfg.get("nightly_hour", 2),
            minute=self.cfg.get("nightly_minute", 0),
            lookback_days=self.cfg.get("lookback_days", 90),
            limit_companies=self.cfg.get("nightly_limit_companies", 0),
        )
        messagebox.showinfo("已保存", "配置已写入本机目录（夜间复检参数已更新）")

    def _run_retention_startup(self) -> None:
        """Admin 启动时后台跑一次超期软删（不阻塞 UI）。"""

        def worker() -> None:
            try:
                result = cleanup_expired_jobs(
                    self.db,
                    retention_days=int(self.cfg.get("retention_days", 365) or 365),
                )
                if result.get("deleted") or result.get("review_ignored"):
                    self.after(0, lambda t=result.get("text") or "": self.log(t))
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda m=str(exc): self.log(f"保留清理失败：{m}"))

        threading.Thread(target=worker, daemon=True).start()


def run_admin(db: LocalDB | None = None) -> None:
    app = AdminApp(db=db)
    app.mainloop()
