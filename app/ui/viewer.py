"""同学端 Viewer：高密度表格 + 行点开看 JD。"""

from __future__ import annotations

import threading
import webbrowser
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

import customtkinter as ctk

from app.collector.filters import (
    is_job_portal_listing_url,
    is_noise_location,
    looks_like_url,
    normalize_recruit_bucket,
    recover_title_from_jd,
    sanitize_job_title,
    strip_career_nav_boilerplate,
)
from app.collector.label_fields import extract_education_requirement
from app.collector.link_health import looks_like_legacy_zhiye_url, resolve_zhiye_apply_url
from app.config import APP_VERSION, load_config, save_config
from app.db.local import LocalDB, MY_PICK_CAMPUS
from app.sync.supabase import SupabaseSync
from app.ui.account_login import require_viewer_login
from app.ui.coach_viewer import CoachCompanionFrame
from app.ui.export_csv import export_jobs_csv
from app.ui.job_review import display_page_updated_at
from app.ui.tree_check import (
    CHECK_COL,
    bind_check_column_click,
    setup_check_column,
    sync_tree_checks,
    with_check,
)
from app.ui.tree_sort import make_company_title_updated_sort

APPLY_STATUSES = ("未投递", "已投递", "笔试", "一面", "二面", "三面", "HR面", "Offer", "已结束", "不想投")


def _display_job_title(job: dict[str, Any]) -> str:
    """展示侧轻量清洗：壳标题回退到 JD 内真实岗位名（不改库）。"""
    cleaned = sanitize_job_title(job.get("title"))
    if cleaned:
        return cleaned
    recovered = recover_title_from_jd(job.get("jd_text"))
    return recovered or (job.get("title") or "").strip() or "（无岗位名称）"


def _display_location(job: dict[str, Any]) -> str:
    loc = (job.get("work_location") or "").strip()
    if not loc or is_noise_location(loc):
        return "—"
    return loc


def _display_education(job: dict[str, Any]) -> str:
    edu = (job.get("education") or "").strip()
    if edu:
        return edu
    return (
        extract_education_requirement(
            job.get("jd_text"),
            title=job.get("title"),
        )
        or "-"
    )


def _display_jd(job: dict[str, Any]) -> str:
    jd = strip_career_nav_boilerplate(job.get("jd_text")) or (job.get("jd_text") or "").strip()
    return jd if jd else "岗位介绍未解析"


def _deadline_state(value: Any, *, today: datetime | None = None) -> str:
    raw = str(value or "").strip()[:10]
    if not raw:
        return "none"
    try:
        deadline = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return "none"
    now = today or datetime.now()
    if deadline.date() < now.date():
        return "expired"
    if deadline <= now + timedelta(days=7):
        return "soon"
    return "later"


class ViewerApp(ctk.CTk):
    def __init__(
        self,
        db: LocalDB | None = None,
        sync: SupabaseSync | None = None,
        *,
        logged_in_account: str | None = None,
    ) -> None:
        super().__init__()
        self.db = db or LocalDB()
        self.cfg = load_config()
        self.sync = sync or SupabaseSync(
            self.cfg.get("supabase_url", ""),
            self.cfg.get("supabase_anon_key", ""),
        )
        self.logged_in_account = (logged_in_account or "").strip() or None
        title = f"校招投递表 Viewer v{APP_VERSION}"
        if self.logged_in_account:
            title += f" — {self.logged_in_account}"
        self.title(title)
        self.geometry("1440x900")
        self.minsize(1120, 700)
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        self.configure(fg_color="#f3f6f9")

        self.bucket_var = ctk.StringVar(value="全部")
        self.nature_var = ctk.StringVar(value="")
        self.industry_var = ctk.StringVar(value="")
        self.location_var = ctk.StringVar(value="")
        self.deadline_scope_var = ctk.StringVar(value="全部")
        self.keyword_var = ctk.StringVar(value="")
        self.status_var = ctk.StringVar(value="就绪")
        self._sort = make_company_title_updated_sort(
            company_label="招聘单位",
            title_label="岗位名称",
            updated_label="岗位发布时间",
        )
        self.jobs: list[dict[str, Any]] = []
        self._selected_job_id: str | None = None
        self.coach_frame: CoachCompanionFrame | None = None

        self._build()
        self.after(200, self.refresh_table)
        self.after(500, self.sync_now_async)
        interval_ms = int(self.cfg.get("sync_interval_minutes") or 30) * 60 * 1000
        self.after(max(interval_ms, 60_000), self._schedule_sync)

    def _build(self) -> None:
        header = ctk.CTkFrame(self, fg_color="#ffffff", corner_radius=10, border_width=1, border_color="#dbe3ec")
        header.pack(fill="x", padx=14, pady=(14, 8))
        ctk.CTkLabel(
            header,
            text="校招投递表",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color="#162235",
        ).pack(side="left", padx=(18, 12), pady=14)
        ctk.CTkLabel(
            header,
            text="岗位检索 · 投递进度 · AI 陪伴",
            font=ctk.CTkFont(size=12),
            text_color="#64748b",
        ).pack(side="left", pady=14)
        ctk.CTkLabel(
            header,
            textvariable=self.status_var,
            anchor="e",
            justify="right",
            text_color="#64748b",
        ).pack(side="right", padx=18, pady=14)

        self.main_tabs = ctk.CTkTabview(self, fg_color="transparent")
        self.main_tabs.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        self.main_tabs.add("岗位投递")
        self.main_tabs.add("AI 陪伴")
        jobs_tab = self.main_tabs.tab("岗位投递")
        coach_tab = self.main_tabs.tab("AI 陪伴")

        self._build_jobs_tab(jobs_tab)
        self.coach_frame = CoachCompanionFrame(
            coach_tab,
            account=self.logged_in_account or "viewer",
            get_selected_job=self._current_job,
        )
        self.coach_frame.pack(fill="both", expand=True, padx=4, pady=4)

        bottom = ctk.CTkFrame(self, fg_color="#e8eef5", corner_radius=8)
        bottom.pack(fill="x", padx=14, pady=(0, 12))
        ctk.CTkLabel(bottom, textvariable=self.status_var, anchor="w", text_color="#475569").pack(
            side="left", fill="x", expand=True, padx=12, pady=7
        )

    def _build_jobs_tab(self, parent: Any) -> None:
        scope = ctk.CTkFrame(parent, fg_color="#ffffff", corner_radius=10, border_width=1, border_color="#dbe3ec")
        scope.pack(fill="x", padx=4, pady=4)
        ctk.CTkLabel(scope, text="岗位范围", font=ctk.CTkFont(size=12, weight="bold"), text_color="#334155").pack(
            side="left", padx=(18, 12), pady=11
        )
        for label in ("全部", "校招", "应届生实习", "日常实习", MY_PICK_CAMPUS):
            ctk.CTkRadioButton(
                scope, text=label, variable=self.bucket_var, value=label, command=self.refresh_table
            ).pack(side="left", padx=7, pady=11)

        filters = ctk.CTkFrame(parent, fg_color="#ffffff", corner_radius=10, border_width=1, border_color="#dbe3ec")
        filters.pack(fill="x", padx=4, pady=4)
        ctk.CTkLabel(filters, text="筛选条件", font=ctk.CTkFont(size=12, weight="bold"), text_color="#334155").pack(
            side="left", padx=(18, 12), pady=12
        )

        # Keep the placeholders for keyboard discoverability, while visible labels
        # make the meaning of empty fields unambiguous.
        def add_filter_field(label: str) -> ctk.CTkFrame:
            field = ctk.CTkFrame(filters, fg_color="transparent")
            field.pack(side="left", padx=4, pady=7)
            ctk.CTkLabel(
                field,
                text=label,
                anchor="center",
                justify="center",
                font=ctk.CTkFont(size=11),
                text_color="#64748b",
            ).pack(fill="x")
            return field

        keyword_field = add_filter_field("关键词")
        ctk.CTkEntry(keyword_field, placeholder_text="关键词（公司/岗位）", textvariable=self.keyword_var, justify="center", width=190, height=32).pack()
        nature_field = add_filter_field("企业性质")
        ctk.CTkEntry(nature_field, placeholder_text="企业性质", textvariable=self.nature_var, justify="center", width=110, height=32).pack()
        industry_field = add_filter_field("行业")
        ctk.CTkEntry(industry_field, placeholder_text="行业", textvariable=self.industry_var, justify="center", width=100, height=32).pack()
        location_field = add_filter_field("base 地")
        ctk.CTkEntry(location_field, placeholder_text="base地", textvariable=self.location_var, justify="center", width=110, height=32).pack()
        ctk.CTkButton(filters, text="筛选", width=70, height=32, command=self.refresh_table).pack(
            side="left", padx=(10, 4), pady=(19, 7)
        )
        ctk.CTkButton(filters, text="清空", width=70, height=32, fg_color="#e8eef5", text_color="#334155", hover_color="#dbe5ef", command=self.clear_filters).pack(
            side="left", padx=4, pady=(19, 7)
        )

        deadline_field = add_filter_field("截止范围")
        ctk.CTkOptionMenu(
            deadline_field,
            values=["全部", "7天内", "已过期"],
            variable=self.deadline_scope_var,
            width=92,
            height=32,
            command=lambda _value: self.refresh_table(),
        ).pack()

        actions = ctk.CTkFrame(parent, fg_color="transparent")
        actions.pack(fill="x", padx=4, pady=(4, 8))
        ctk.CTkLabel(actions, text="批量操作", font=ctk.CTkFont(size=12, weight="bold"), text_color="#64748b").pack(
            side="left", padx=(4, 12)
        )
        ctk.CTkButton(actions, text="同步", width=78, height=34, command=self.sync_now_async).pack(side="left", padx=3)
        ctk.CTkButton(actions, text="导出 CSV", width=92, height=34, command=self.export_csv).pack(side="left", padx=3)
        ctk.CTkButton(actions, text="导出选中", width=92, height=34, command=self.export_selected_csv).pack(side="left", padx=3)
        ctk.CTkButton(actions, text="加入个人校招投递", width=128, height=34, fg_color="#0f766e", hover_color="#0b6258", command=self.add_to_my_pick).pack(side="left", padx=3)
        ctk.CTkButton(actions, text="移出个人校招投递", width=128, height=34, fg_color="#64748b", hover_color="#475569", command=self.remove_from_my_pick).pack(side="left", padx=3)
        ctk.CTkButton(actions, text="全选", width=68, height=34, fg_color="#e8eef5", text_color="#334155", hover_color="#dbe5ef", command=self.select_all_rows).pack(side="left", padx=(16, 3))
        ctk.CTkButton(actions, text="取消全选", width=82, height=34, fg_color="#e8eef5", text_color="#334155", hover_color="#dbe5ef", command=self.clear_row_selection).pack(side="left", padx=3)
        ctk.CTkButton(
            actions,
            text="AI 匹配选中岗",
            width=120,
            height=34,
            fg_color="#1d4ed8",
            hover_color="#1e40af",
            command=self.open_ai_match_for_selection,
        ).pack(side="left", padx=(16, 3))

        body = ctk.CTkFrame(parent, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=4, pady=(0, 8))

        table_frame = ctk.CTkFrame(body, fg_color="#ffffff", corner_radius=10, border_width=1, border_color="#dbe3ec")
        table_frame.pack(side="left", fill="both", expand=True)

        # 岗位名称紧跟招聘单位，岗位类型统一显示三类 recruit_bucket。
        cols = (
            CHECK_COL,
            "updated_at",
            "group_name",
            "company",
            "title",
            "salary_range",
            "headcount",
            "recruit_project",
            "education",
            "company_nature",
            "deadline",
            "work_location",
            "my_apply_status",
        )
        style = ttk.Style(self)
        style.configure("Viewer.Treeview", rowheight=30, font=("Microsoft YaHei UI", 10), background="#ffffff", fieldbackground="#ffffff", foreground="#253247")
        style.configure("Viewer.Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"), background="#edf2f7", foreground="#334155", relief="flat", padding=(6, 8))
        style.map("Viewer.Treeview", background=[("selected", "#dbeafe")], foreground=[("selected", "#0f172a")])
        style.map("Viewer.Treeview.Heading", background=[("active", "#dbe5ef")])
        self.tree = ttk.Treeview(
            table_frame, columns=cols, show="headings", height=24, selectmode="extended", style="Viewer.Treeview"
        )
        headers = {
            "updated_at": "岗位发布时间",
            "group_name": "集团",
            "company": "招聘单位",
            "title": "岗位名称",
            "salary_range": "薪资范围",
            "headcount": "招聘人数",
            "recruit_project": "岗位类型",
            "education": "学历要求",
            "company_nature": "企业性质",
            "deadline": "截止时间",
            "work_location": "base地",
            "my_apply_status": "投递进度",
        }
        widths = {
            "updated_at": 110,
            "group_name": 90,
            "company": 110,
            "title": 200,
            "salary_range": 70,
            "headcount": 60,
            "recruit_project": 80,
            "education": 70,
            "company_nature": 60,
            "deadline": 80,
            "work_location": 90,
            "my_apply_status": 70,
        }
        setup_check_column(self.tree)
        for c in cols:
            if c == CHECK_COL:
                continue
            self.tree.column(c, width=widths[c], anchor="w")
            if c in self._sort.sortable:
                self.tree.heading(
                    c,
                    text=headers[c],
                    command=lambda col=c: self._on_sort_heading(col),
                )
            else:
                self.tree.heading(c, text=headers[c])
        self._sort.apply_heading_labels(
            self.tree,
            all_cols=[(c, headers[c]) for c in cols if c != CHECK_COL],
            on_sorted=self.refresh_table,
        )
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")
        bind_check_column_click(self.tree, on_after=self.on_select)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        detail = ctk.CTkFrame(body, width=380, fg_color="#ffffff", corner_radius=10, border_width=1, border_color="#dbe3ec")
        detail.pack(side="right", fill="y", padx=(6, 0))
        detail.pack_propagate(False)
        ctk.CTkLabel(detail, text="岗位详情", font=ctk.CTkFont(size=15, weight="bold"), text_color="#162235").pack(
            anchor="w", padx=8, pady=(8, 4)
        )
        self.detail_title = ctk.CTkLabel(detail, text="请选择一条岗位查看详情", wraplength=350, justify="left", text_color="#334155")
        self.detail_title.pack(anchor="w", padx=8)
        self.detail_meta = ctk.CTkLabel(detail, text="", wraplength=330, justify="left", text_color="#555")
        self.detail_meta.pack(anchor="w", padx=8, pady=4)
        ctk.CTkLabel(detail, text="岗位介绍（JD）", font=ctk.CTkFont(size=12, weight="bold"), text_color="#334155").pack(
            anchor="w", padx=8, pady=(6, 2)
        )
        self.jd_box = ctk.CTkTextbox(detail, width=350, height=340, fg_color="#f8fafc", border_width=1, border_color="#e2e8f0")
        self.jd_box.pack(fill="both", expand=True, padx=8, pady=4)
        self.jd_box.insert("1.0", "JD 未选择")
        self.jd_box.configure(state="disabled")

        link_row = ctk.CTkFrame(detail)
        link_row.pack(fill="x", padx=8, pady=4)
        ctk.CTkButton(link_row, text="打开网申", width=120, command=self.open_apply).pack(side="left", padx=2)

        st_row = ctk.CTkFrame(detail)
        st_row.pack(fill="x", padx=8, pady=6)
        ctk.CTkLabel(st_row, text="本机进度").pack(side="left")
        self.apply_menu = ctk.CTkOptionMenu(
            st_row, values=list(APPLY_STATUSES), command=self.save_apply_status, width=120
        )
        self.apply_menu.set("未投递")
        self.apply_menu.pack(side="left", padx=6)

    def open_ai_match_for_selection(self) -> None:
        """从岗位表跳到 AI 陪伴匹配页，并填入当前选中岗位。"""
        if not self._current_job():
            messagebox.showinfo("提示", "请先选中一条岗位")
            return
        self.main_tabs.set("AI 陪伴")
        if self.coach_frame is not None:
            self.coach_frame.tabs.set("岗位匹配")
            self.coach_frame.fill_job_from_selection()


    def clear_filters(self) -> None:
        """Clear free-text filters without changing the selected job scope."""
        for variable in (self.keyword_var, self.nature_var, self.industry_var, self.location_var):
            variable.set("")
        self.deadline_scope_var.set("全部")
        self.refresh_table()

    def _filters(self) -> dict[str, Any]:
        bucket = self.bucket_var.get()
        my_pick_only = bucket == MY_PICK_CAMPUS
        return {
            "bucket": None if my_pick_only else bucket,
            "company_nature": self.nature_var.get().strip() or None,
            "industry": self.industry_var.get().strip() or None,
            "location": self.location_var.get().strip() or None,
            "keyword": self.keyword_var.get().strip() or None,
            "my_pick_only": my_pick_only,
        }

    def _on_sort_heading(self, col: str) -> None:
        self._sort.toggle(col)
        self.refresh_table()

    def refresh_table(self) -> None:
        prev_sel = list(self.tree.selection())
        for i in self.tree.get_children():
            self.tree.delete(i)
        jobs = self.db.list_jobs(**self._filters(), limit=3000)
        scope = self.deadline_scope_var.get()
        if scope == "7天内":
            jobs = [j for j in jobs if _deadline_state(j.get("deadline")) == "soon"]
        elif scope == "已过期":
            jobs = [j for j in jobs if _deadline_state(j.get("deadline")) == "expired"]
        self.jobs = self._sort.sort_items(list(jobs))
        for job in self.jobs:
            row_id = self.tree.insert(
                "",
                "end",
                iid=job["id"],
                values=with_check(
                    (
                        display_page_updated_at(job),
                        job.get("group_name") or "",
                        job.get("company") or "",
                        _display_job_title(job),
                        (job.get("salary_range") or "").strip() or "-",
                        (job.get("headcount") or "").strip() or "-",
                        normalize_recruit_bucket(
                            job.get("recruit_bucket"),
                            recruit_project=job.get("recruit_project"),
                            title=job.get("title"),
                        )
                        or "",
                        _display_education(job),
                        job.get("company_nature") or "",
                        job.get("deadline") or "",
                        _display_location(job) if (job.get("work_location") or "").strip() else "",
                        job.get("my_apply_status") or "未投递",
                    )
                ),
            )
            self.tree.item(row_id, tags=("even" if len(self.tree.get_children()) % 2 == 0 else "odd",))
        self.tree.tag_configure("even", background="#ffffff")
        self.tree.tag_configure("odd", background="#f8fafc")
        keep = [i for i in prev_sel if self.tree.exists(i)]
        if keep:
            self.tree.selection_set(keep)
        self._sort.apply_heading_labels(
            self.tree,
            all_cols=[
                ("updated_at", "岗位发布时间"),
                ("group_name", "集团"),
                ("company", "招聘单位"),
                ("title", "岗位名称"),
                ("salary_range", "薪资范围"),
                ("headcount", "招聘人数"),
                ("recruit_project", "岗位类型"),
                ("education", "学历要求"),
                ("company_nature", "企业性质"),
                ("deadline", "截止时间"),
                ("work_location", "base地"),
                ("my_apply_status", "投递进度"),
            ],
            on_sorted=self.refresh_table,
        )
        sync_tree_checks(self.tree)
        last = self.db.get_meta("last_sync_ok_at") or self.db.get_meta("last_cloud_sync_at") or "尚未同步"
        n_sel = len(self.tree.selection())
        pick_n = self.db.count_my_pick()
        view = self.bucket_var.get()
        pick_hint = f" | {MY_PICK_CAMPUS} {pick_n} 条" if view != MY_PICK_CAMPUS else ""
        self.status_var.set(
            f"共 {len(self.jobs)} 条{pick_hint} | 已选 {n_sel} | 最后同步: {last} | "
            f"勾选后可「加入个人校招投递」或「导出选中」"
        )

    def select_all_rows(self) -> None:
        kids = self.tree.get_children()
        if kids:
            self.tree.selection_set(kids)
        self.on_select()

    def clear_row_selection(self) -> None:
        self.tree.selection_remove(self.tree.selection())
        self._selected_job_id = None
        sync_tree_checks(self.tree)
        n = len(self.jobs)
        last = self.db.get_meta("last_sync_ok_at") or self.db.get_meta("last_cloud_sync_at") or "尚未同步"
        pick_n = self.db.count_my_pick()
        self.status_var.set(
            f"共 {n} 条 | {MY_PICK_CAMPUS} {pick_n} 条 | 已选 0 | 最后同步: {last} | "
            f"勾选后可「加入个人校招投递」或「导出选中」"
        )

    def on_select(self, _event=None) -> None:
        sync_tree_checks(self.tree)
        sel = self.tree.selection()
        n = len(self.jobs)
        last = self.db.get_meta("last_sync_ok_at") or self.db.get_meta("last_cloud_sync_at") or "尚未同步"
        pick_n = self.db.count_my_pick()
        view = self.bucket_var.get()
        pick_hint = f" | {MY_PICK_CAMPUS} {pick_n} 条" if view != MY_PICK_CAMPUS else ""
        self.status_var.set(
            f"共 {n} 条{pick_hint} | 已选 {len(sel)} | 最后同步: {last} | "
            f"勾选后可「加入个人校招投递」或「导出选中」"
        )
        if not sel:
            return
        job_id = sel[-1]
        self._selected_job_id = job_id
        job = self.db.get_job(job_id)
        if not job:
            return
        title = _display_job_title(job)
        job_type = normalize_recruit_bucket(
            job.get("recruit_bucket"),
            recruit_project=job.get("recruit_project"),
            title=job.get("title"),
        ) or "—"
        base = _display_location(job)
        salary = (job.get("salary_range") or "").strip() or "-"
        headcount = (job.get("headcount") or "").strip() or "-"
        self.detail_title.configure(text=f"岗位名称：{title}")
        self.detail_meta.configure(
            text=(
                f"薪资范围：{salary}\n"
                f"招聘人数：{headcount}\n"
                f"岗位类型：{job_type}\n"
                f"base地：{base}\n"
                f"单位：{job.get('company') or '—'}｜{job.get('company_nature') or ''}"
            )
        )
        self.jd_box.configure(state="normal")
        self.jd_box.delete("1.0", "end")
        self.jd_box.insert("1.0", _display_jd(job))
        self.jd_box.configure(state="disabled")
        self.apply_menu.set(job.get("my_apply_status") or "未投递")

    def save_apply_status(self, value: str) -> None:
        if not self._selected_job_id:
            return
        self.db.set_my_status(self._selected_job_id, value)
        keep_id = self._selected_job_id
        keep_sel = list(self.tree.selection())
        self.refresh_table()
        restore = [i for i in keep_sel if i in self.tree.get_children()]
        if keep_id in self.tree.get_children() and keep_id not in restore:
            restore.append(keep_id)
        if restore:
            self.tree.selection_set(restore)
            sync_tree_checks(self.tree)
            self.on_select()

    def _current_job(self) -> dict[str, Any] | None:
        if not self._selected_job_id:
            return None
        return self.db.get_job(self._selected_job_id)

    def open_apply(self) -> None:
        job = self._current_job()
        if not job:
            return
        url = str(job.get("apply_url") or "").strip()
        if not looks_like_url(url) or is_job_portal_listing_url(url):
            messagebox.showinfo("暂无网申", "该岗位尚未识别到具体岗位网申链接。")
            return
        if looks_like_legacy_zhiye_url(url):
            upgraded = resolve_zhiye_apply_url(url)
            if upgraded != url:
                url = upgraded
                messagebox.showinfo("网申链接已更新", "已将旧版智业链接转换为当前岗位详情页。")
        webbrowser.open(url)

    def export_csv(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile=f"校招投递表_{datetime.now().strftime('%Y%m%d')}.csv",
        )
        if not path:
            return
        export_jobs_csv(self.jobs, path)
        messagebox.showinfo("导出完成", f"已导出 {len(self.jobs)} 条到\n{path}")

    def export_selected_csv(self) -> None:
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("提示", "请先勾选首列 ☐/☑，或用 Ctrl/Shift 多选后，再导出选中")
            return
        by_id = {j["id"]: j for j in self.jobs}
        jobs = [by_id[i] for i in sel if i in by_id]
        if not jobs:
            messagebox.showwarning("提示", "选中行已不在当前列表，请刷新后重试")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile=f"校招投递表_选中_{datetime.now().strftime('%Y%m%d')}.csv",
        )
        if not path:
            return
        export_jobs_csv(jobs, path)
        messagebox.showinfo("导出完成", f"已导出选中 {len(jobs)} 条到\n{path}")

    def add_to_my_pick(self) -> None:
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("提示", "请先勾选首列 ☐/☑，或用 Ctrl/Shift 多选岗位")
            return
        by_id = {j["id"]: j for j in self.jobs}
        valid = [i for i in sel if i in by_id]
        if not valid:
            messagebox.showwarning("提示", "选中行已不在当前列表，请刷新后重试")
            return
        added = self.db.add_my_pick(valid)
        skipped = len(valid) - added
        msg = f"已将 {added} 个岗位加入「{MY_PICK_CAMPUS}」"
        if skipped:
            msg += f"（{skipped} 个已在列表中）"
        messagebox.showinfo("已加入", msg)
        if self.bucket_var.get() == MY_PICK_CAMPUS:
            self.refresh_table()

    def remove_from_my_pick(self) -> None:
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("提示", "请先勾选要移出的岗位")
            return
        removed = self.db.remove_my_pick(sel)
        if not removed:
            messagebox.showinfo("提示", f"选中岗位不在「{MY_PICK_CAMPUS}」中")
            return
        messagebox.showinfo("已移出", f"已从「{MY_PICK_CAMPUS}」移出 {removed} 个岗位")
        self.refresh_table()

    def sync_now_async(self) -> None:
        threading.Thread(target=self._sync_worker, daemon=True).start()

    def _sync_worker(self) -> None:
        self.status_var.set("正在同步…")
        try:
            ok, min_v = self.sync.check_min_version()
            if not ok:
                self.after(0, lambda: messagebox.showwarning("版本过旧", f"请升级到 >= {min_v}"))
                return
            if not self.sync.enabled:
                self.after(0, lambda: self.status_var.set("未配置 Supabase，仅显示本机数据"))
                self.after(0, self.refresh_table)
                return
            if not self.sync.session_token:
                self.after(0, lambda: self.status_var.set("未建立云端会话，仅显示本机数据"))
                self.after(0, self.refresh_table)
                return
            n = self.sync.pull_jobs(self.db)
            self.cfg["last_cloud_sync_at"] = self.db.get_meta("last_cloud_sync_at")
            save_config(self.cfg)
            self.after(0, self.refresh_table)
            self.after(0, lambda: self.status_var.set(f"同步完成，更新 {n} 条"))
        except Exception as exc:  # noqa: BLE001
            self.after(0, lambda: self.status_var.set(f"同步失败: {exc}"))

    def _schedule_sync(self) -> None:
        self.sync_now_async()
        interval_ms = int(self.cfg.get("sync_interval_minutes") or 30) * 60 * 1000
        self.after(max(interval_ms, 60_000), self._schedule_sync)


def _short_time(value: str | None) -> str:
    if not value:
        return ""
    return str(value).replace("T", " ")[:16]


def run_viewer(db: LocalDB | None = None) -> None:
    db = db or LocalDB()
    cfg = load_config()
    sync = SupabaseSync(
        cfg.get("supabase_url", ""),
        cfg.get("supabase_anon_key", ""),
    )
    login = require_viewer_login(db, sync)
    if not login:
        return
    if login.session_token:
        sync.session_token = login.session_token
    else:
        sync.clear_viewer_session()
    app = ViewerApp(db=db, sync=sync, logged_in_account=login.account)
    app.mainloop()
