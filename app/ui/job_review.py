"""Admin 审核列表面板：按 mode 固定为「正常岗位」或「异常队列」，不再页内切换。"""

from __future__ import annotations

import re
import threading
import webbrowser
from typing import Any, Callable, Literal

import customtkinter as ctk
from tkinter import ttk, messagebox

from app.collector.fill_from_url import (
    FillCandidate,
    FillFromUrlResult,
    apply_reidentify_fields,
    fields_look_like_no_job_posting,
    find_matching_fill_candidate,
    known_keys_from_jobs,
    pick_fill_url,
    resolve_result_is_no_job_posting,
    summarize_reidentify_changes,
    take_collect_batch,
)
from app.collector.auto_review import assess_jobs_for_auto_review
from app.collector.cleanup import deduplicate_jobs
from app.collector.filters import (
    campus_recognition_action,
    is_portal_shell_record,
    normalize_recruit_bucket,
)
from app.collector.label_fields import (
    extract_education_requirement,
    extract_labeled_fields,
    normalize_headcount,
    normalize_salary_range,
    split_jd_sections,
    summarize_jd_dedupe,
    strip_redundant_jd_meta,
)
from app.collector.pipeline import publish_review_item
from app.collector.reidentify_worker import (
    deserialize_result,
    drain_reidentify_messages,
    serialize_candidate,
    start_reidentify_process,
)
from app.db.local import LocalDB
from app.ui.job_editor import (
    JobEditDialog,
    JobEditForm,
    merge_job_fields,
    pick_job_candidates,
)
from app.ui.tree_check import (
    CHECK_COL,
    is_check_cell,
    setup_check_column,
    sync_tree_checks,
    with_check,
)
from app.ui.tree_sort import TreeSortController, company_cluster_key


LogCb = Callable[[str], None]
ChangedCb = Callable[[], None]
PushCloudCb = Callable[[list[str] | None], None]
PanelMode = Literal["normal", "abnormal", "display"]

# 展示列（首列勾选框另由 CHECK_COL 前置）：集团 → … → 岗位发布时间（open_at）
_REVIEW_COLS = (
    ("group_name", 100, "集团"),
    ("company", 110, "公司"),
    ("title", 160, "岗位名称"),
    ("salary_range", 70, "薪资范围"),
    ("headcount", 60, "招聘人数"),
    ("job_type", 80, "岗位类型"),
    ("job_duties", 140, "岗位要求"),
    ("job_requirements", 140, "任职要求"),
    ("education", 70, "学历要求"),
    ("work_location", 90, "工作地点"),
    ("source_url", 140, "链接"),
    ("updated_at", 110, "岗位发布时间"),
)

_NORMAL_COLS = _REVIEW_COLS
# 岗位显示：在审核列基础上增加云端上传进度
_DISPLAY_COLS = (
    *_REVIEW_COLS,
    ("cloud_progress", 130, "云端上传进度"),
)
# 异常队列额外展示状态 / 原因，便于审异常
_ABNORMAL_COLS = (
    *_REVIEW_COLS,
    ("status", 70, "状态"),
    ("reason", 140, "原因"),
)

_GROUP_FIELD_KEYS = ("group", "group_name", "corp_group", "parent_company", "集团")


def _clip_cell(text: str | None, limit: int = 96) -> str:
    s = re.sub(r"\s+", " ", (text or "").strip())
    if len(s) <= limit:
        return s
    return s[: max(limit - 1, 1)] + "…"


def display_group_name(
    job: dict[str, Any] | None,
    *,
    seed_company_name: str | None = None,
) -> str:
    """
    集团：入库 group_name 优先；否则用种子公司名与岗位「公司」比对——
    仅当岗位公司是集团官网下识别到的其他招聘单位时填集团。
    不再从公司名正则截取「…集团」（避免子公司自身名含集团时误判）。
    """
    from app.collector.filters import resolve_group_and_company

    src = job or {}
    for key in _GROUP_FIELD_KEYS:
        val = str(src.get(key) or "").strip()
        if val:
            return val
    seed = (seed_company_name or "").strip()
    if not seed:
        return ""
    group, _company = resolve_group_and_company(
        seed_company_name=seed,
        page_company=str(src.get("company") or "") or None,
    )
    return group


def display_job_type(job: dict[str, Any] | None) -> str:
    """岗位类型统一为：校招 / 应届生实习 / 日常实习。"""
    src = job or {}
    return normalize_recruit_bucket(
        str(src.get("recruit_bucket") or "") or None,
        recruit_project=str(src.get("recruit_project") or "") or None,
        title=str(src.get("title") or "") or None,
    ) or ""


def display_jd_sections(jd_text: str | None) -> tuple[str, str]:
    """
    从 JD 文本拆出表格用（岗位要求, 任职要求）。
    支持中英分段标题；岗位要求列取职责段，任职要求列取要求/技能/资格段。
    """
    duties, reqs = split_jd_sections(jd_text)
    return _clip_cell(duties), _clip_cell(reqs)


def display_education(job: dict[str, Any] | None) -> str:
    """学历要求：库字段优先，否则从 JD/任职要求/标题括号抽取；空则「-」。"""
    src = job or {}
    edu = str(src.get("education") or "").strip()
    if edu:
        return edu
    edu = (
        extract_education_requirement(
            str(src.get("jd_text") or "") or None,
            title=str(src.get("title") or "") or None,
        )
        or ""
    )
    return edu or "-"


def display_field_or_dash(job: dict[str, Any] | None, key: str) -> str:
    """结构化短字段展示：有值原文，缺省「-」（不把无关正文塞进列）。"""
    val = str((job or {}).get(key) or "").strip()
    return val if val else "-"


def display_salary_range(job: dict[str, Any] | None) -> str:
    return display_field_or_dash(job, "salary_range")


def display_headcount(job: dict[str, Any] | None) -> str:
    return display_field_or_dash(job, "headcount")


def resolve_page_updated_at(job: dict[str, Any] | None) -> str | None:
    """
    岗位发布时间（网页发布/更新参照日，非本机 jobs.updated_at）。

    优先级：open_at → list_updated_at → published_at。
    """
    src = job or {}
    for key in ("open_at", "list_updated_at", "published_at"):
        raw = src.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        if raw is not None and not isinstance(raw, str):
            s = str(raw).strip()
            if s:
                return s
    return None


def display_page_updated_at(
    job: dict[str, Any] | None,
    *,
    override: str | None = None,
) -> str:
    """审核/显示/Viewer「岗位发布时间」列：页面日期；无则「-」（不用本地 DB 戳）。"""
    raw = (override or "").strip() if override is not None else None
    if raw is None:
        raw = resolve_page_updated_at(job) or ""
    # 显式传入本地库戳时仍拒绝（防异常队列误传 updated_at）
    if not raw:
        return "-"
    return raw.replace("T", " ")[:16]


def review_row_values(
    job: dict[str, Any],
    *,
    updated_at: str | None = None,
    seed_company_name: str | None = None,
) -> tuple[str, ...]:
    """组装岗位审核表一行（不含勾选列）。"""
    duties, reqs = display_jd_sections(job.get("jd_text") if isinstance(job, dict) else None)
    # updated_at 参数仅作页面日期覆盖；缺省走 open_at 等，不用 jobs.updated_at
    when = display_page_updated_at(
        job,
        override=updated_at if updated_at is not None else None,
    )
    return (
        display_group_name(job, seed_company_name=seed_company_name),
        str(job.get("company") or ""),
        str(job.get("title") or ""),
        display_salary_range(job),
        display_headcount(job),
        display_job_type(job),
        duties,
        reqs,
        display_education(job),
        str(job.get("work_location") or ""),
        # 「链接」列：优先岗位申请/网申，无则校招/原文列表页
        str(job.get("apply_url") or job.get("source_url") or ""),
        when,
    )


def prefer_job_url(job: dict[str, Any] | None) -> str:
    """打开网址用：优先网申链接，其次原文链接。

    hotjob/wecruit posDetail 若缺 postType，浏览器 SPA 详情会空白，这里按岗位频道补全。
    """
    src = job or {}
    url = str(src.get("apply_url") or src.get("source_url") or "").strip()
    if not url:
        return ""
    try:
        from app.collector.adapters import hotjob as hotjob_adapter

        if hotjob_adapter.can_handle(url):
            return hotjob_adapter.ensure_posdetail_post_type(url, fields=src)
    except Exception:  # noqa: BLE001
        return url
    return url


def cloud_upload_progress(job: dict[str, Any] | None) -> str:
    """根据 cloud_updated_at / updated_at 推导云端上传进度文案。"""
    src = job or {}
    cloud = str(src.get("cloud_updated_at") or "").strip()
    local = str(src.get("updated_at") or "").strip()
    if not cloud:
        return "未推送"
    cloud_short = cloud[:16]
    if local and local > cloud:
        return f"待推送（本地有更新 · 上次 {cloud_short}）"
    return f"已推送 {cloud_short}"

# Windows tk event.state 修饰键位
_SHIFT = 0x0001
_CTRL = 0x0004


def tree_range_iids(children: list[str], start: str, end: str) -> list[str]:
    """返回 Treeview 子项中从 start 到 end（含）的连续 iid 列表。"""
    if not children:
        return []
    if start not in children:
        return [end] if end in children else []
    if end not in children:
        return [start]
    i, j = children.index(start), children.index(end)
    if i > j:
        i, j = j, i
    return children[i : j + 1]


class JobReviewPanel(ctk.CTkFrame):
    """固定模式列表 + 侧栏编辑；支持 Ctrl/Shift/拖选多选与批量操作。

    mode=\"normal\" → 岗位审核（pending_review，待人工确认）
    mode=\"display\" → 岗位显示（active，已确认可推云端）
    mode=\"abnormal\" → 异常队列（仅 pending 待审）
    """

    def __init__(
        self,
        master: Any,
        db: LocalDB,
        *,
        on_log: LogCb | None = None,
        on_changed: ChangedCb | None = None,
        on_push_cloud: PushCloudCb | None = None,
        mode: PanelMode | None = None,
        initial_segment: str | None = None,
    ) -> None:
        super().__init__(master)
        self.db = db
        self._on_log = on_log or (lambda _m: None)
        self._on_changed = on_changed or (lambda: None)
        self._on_push_cloud = on_push_cloud
        # mode 优先；兼容旧参数 initial_segment
        raw = mode if mode is not None else (initial_segment or "normal")
        if raw == "abnormal":
            self._mode: PanelMode = "abnormal"
        elif raw == "display":
            self._mode = "display"
        else:
            self._mode = "normal"
        self._segment = self._mode  # 兼容内部旧字段名
        self._normal_items: dict[str, dict[str, Any]] = {}
        self._display_items: dict[str, dict[str, Any]] = {}
        self._abnormal_items: dict[str, dict[str, Any]] = {}
        self._current_id: str | None = None
        self._current_kind: str | None = None  # "job" | "review"
        self._list_total = 0
        # 拖选状态
        self._drag_active = False
        self._drag_anchor: str | None = None
        self._drag_moved = False
        self._drag_base: tuple[str, ...] = ()
        self._drag_ctrl = False
        self._shift_anchor: str | None = None
        self._reidentify_busy = False
        self._timeliness_busy = False
        self._reidentify_btn: Any | None = None
        self._reidentify_process: Any | None = None
        self._reidentify_message_queue: Any | None = None
        self._reidentify_pending_payload: tuple[str, Any] | None = None
        self._reidentify_exit_poll_count = 0
        self._sort = self._make_sort_controller()
        self._build()

    @property
    def _cols(self) -> tuple[tuple[str, int, str], ...]:
        if self._mode == "abnormal":
            return _ABNORMAL_COLS
        if self._mode == "display":
            return _DISPLAY_COLS
        return _NORMAL_COLS

    def _is_job_list(self) -> bool:
        return self._mode in ("normal", "display")

    def _make_sort_controller(self) -> TreeSortController:
        def _company(item: dict[str, Any]) -> tuple[str, str]:
            if self._is_job_list():
                return company_cluster_key(item.get("company"))
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            return company_cluster_key(payload.get("company") or item.get("company"))

        def _title(item: dict[str, Any]) -> str:
            if self._is_job_list():
                return str(item.get("title") or "").lower()
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            return str(payload.get("title") or item.get("title") or "").lower()

        def _updated(item: dict[str, Any]) -> str:
            if self._is_job_list():
                return resolve_page_updated_at(item) or ""
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            return (
                resolve_page_updated_at(payload)
                or resolve_page_updated_at(item)
                or ""
            )

        def _cloud(item: dict[str, Any]) -> str:
            return cloud_upload_progress(item)

        sortable = {
            "company": ("公司", _company),
            "title": ("岗位名称", _title),
            "updated_at": ("岗位发布时间", _updated),
        }
        if self._mode == "display":
            sortable["cloud_progress"] = ("云端上传进度", _cloud)

        return TreeSortController(
            sortable=sortable,
            default_col="company",
            default_ascending=True,
            tie_breakers=[_company, _title, _updated],
        )

    def _build(self) -> None:
        top = ctk.CTkFrame(self)
        top.pack(fill="x", padx=6, pady=6)

        if self._mode == "display":
            title = "岗位显示 · 已审核可推送"
        elif self._mode == "normal":
            title = "岗位审核 · 待确认正常岗"
        else:
            title = "异常队列 · 待审异常"
        ctk.CTkLabel(top, text=title, font=ctk.CTkFont(weight="bold")).pack(side="left", padx=4)

        kw_ph = (
            "筛选关键词（公司/标题/原因）"
            if self._mode == "abnormal"
            else "筛选关键词（公司/标题）"
        )
        self.kw_entry = ctk.CTkEntry(top, width=220, placeholder_text=kw_ph)
        self.kw_entry.pack(side="left", padx=8)
        self.kw_entry.bind("<Return>", lambda _e: self.refresh())
        ctk.CTkButton(top, text="筛选/刷新", width=90, command=self.refresh).pack(side="left", padx=2)
        ctk.CTkButton(top, text="全选当前列表", width=110, command=self.select_all).pack(
            side="left", padx=4
        )
        ctk.CTkButton(top, text="取消全选", width=90, command=self.clear_selection).pack(
            side="left", padx=2
        )

        if self._mode == "display":
            tip = (
                "岗位显示：已审核通过的正常岗（active），供 Viewer 同步。"
                "审错可「反审核」退回「岗位审核」；曾推送过的会在下次「推送云端」时从云端撤下。"
                "「云端上传进度」；勾选可推选中，未勾选推全部显示中+软删。内容异常→「异常队列」。"
            )
        elif self._mode == "normal":
            tip = (
                "岗位审核：仅待人工确认的正常岗（pending_review）。"
                "「重新识别」= 局部重采：展开同站校招+实习（近约半年），单企业每批最多约 50 个未入库岗；"
                "勾选将新增到岗位审核，仅与原行匹配的一条覆盖原行。剩余请再次重新识别。"
                "页面无岗则软删。仍不对就侧栏/编辑人工改。点「审核」后移入「岗位显示」。"
                "内容异常→「异常队列」；官网可信→「源验证」。勾选 ☐/☑；Ctrl / Shift / 拖选。"
            )
        else:
            tip = (
                "异常队列：岗位内容/解析/规则问题（届别、过期、缺字段、误标社招等），不是官网可信度。"
                "官网未确认请用「源验证」。「打开网址」核验链接；勾选首列 ☐/☑。"
            )
        ctk.CTkLabel(
            self,
            text=tip,
            text_color="#666666",
            anchor="w",
            justify="left",
        ).pack(fill="x", padx=10, pady=(0, 4))

        self.action_row = ctk.CTkFrame(self)
        self.action_row.pack(fill="x", padx=6, pady=2)
        self._rebuild_actions()
        self.selection_hint_var = ctk.StringVar(value="")
        self.selection_hint = ctk.CTkLabel(
            self,
            textvariable=self.selection_hint_var,
            anchor="w",
            justify="left",
            text_color="#64748b",
        )
        self.selection_hint.pack(fill="x", padx=10, pady=(0, 4))

        body = ctk.CTkFrame(self)
        body.pack(fill="both", expand=True, padx=6, pady=4)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        cols = self._cols
        self.tree = ttk.Treeview(
            left,
            columns=[CHECK_COL, *[c[0] for c in cols]],
            show="headings",
            height=22,
            selectmode="extended",
        )
        self._apply_columns(cols)
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_double_click)
        # 自定义单击/拖选，return "break" 避免与默认选中打架
        self.tree.bind("<Button-1>", self._on_tree_button1)
        self.tree.bind("<B1-Motion>", self._on_tree_b1_motion)
        self.tree.bind("<ButtonRelease-1>", self._on_tree_button_release)

        right = ctk.CTkFrame(body)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            right,
            text="编辑选中项（可滚动；多选时显示最后点击的一行）",
            font=ctk.CTkFont(weight="bold"),
        ).grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 0))
        self.form = JobEditForm(right)
        self.form.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        if self._mode == "normal":
            self.form.on_import_selected = self._import_fill_candidates_to_review
        form_btns = ctk.CTkFrame(right)
        form_btns.grid(row=2, column=0, sticky="ew", padx=4, pady=6)
        ctk.CTkButton(form_btns, text="保存修改", command=self.save_current).pack(side="left", padx=4)
        if self._mode == "abnormal":
            ctk.CTkButton(form_btns, text="打开网址", command=self.open_selected_url).pack(
                side="left", padx=4
            )

        self.count_label = ctk.CTkLabel(self, text="", anchor="w", text_color="#555")
        self.count_label.pack(fill="x", padx=10, pady=(0, 4))

    def _rebuild_actions(self) -> None:
        for w in self.action_row.winfo_children():
            w.destroy()
        # 主操作固定顺序：删除 → 编辑 → 审核 / 推送云端（作用于当前勾选/多选）
        if self._mode == "display":
            ctk.CTkButton(
                self.action_row, text="删除", fg_color="#a33", command=self.reject_display
            ).pack(side="left", padx=4)
            ctk.CTkButton(self.action_row, text="编辑", command=self.open_edit_dialog).pack(
                side="left", padx=4
            )
            ctk.CTkButton(
                self.action_row,
                text="反审核",
                fg_color="#8a5a00",
                command=self.unapprove_display,
            ).pack(side="left", padx=4)
            ctk.CTkButton(
                self.action_row,
                text="推送云端",
                fg_color="#0b6e4f",
                command=self.push_selected_or_all,
            ).pack(side="left", padx=4)
        elif self._mode == "normal":
            ctk.CTkButton(
                self.action_row, text="删除", fg_color="#a33", command=self.reject_normal
            ).pack(side="left", padx=4)
            ctk.CTkButton(self.action_row, text="编辑", command=self.open_edit_dialog).pack(
                side="left", padx=4
            )
            self._reidentify_btn = ctk.CTkButton(
                self.action_row,
                text="重新识别（局部重采）",
                fg_color="#1f6aa5",
                command=self.reidentify_selected,
            )
            self._reidentify_btn.pack(side="left", padx=4)
            if self._reidentify_busy:
                try:
                    self._reidentify_btn.configure(state="disabled")
                except Exception:
                    pass
            ctk.CTkButton(
                self.action_row,
                text="岗位去重",
                fg_color="#2d6a4f",
                command=self.deduplicate_selected,
            ).pack(side="left", padx=4)
            ctk.CTkButton(
                self.action_row,
                text="校园招聘识别",
                fg_color="#6b3fa0",
                command=self.campus_recognition_selected,
            ).pack(side="left", padx=4)
            ctk.CTkButton(
                self.action_row,
                text="信息时效性识别",
                fg_color="#5c4b8a",
                command=self.check_timeliness_selected,
            ).pack(side="left", padx=4)
            ctk.CTkButton(
                self.action_row,
                text="自动审核",
                fg_color="#0b6e4f",
                command=self.auto_approve_normal,
            ).pack(side="left", padx=4)
            ctk.CTkButton(
                self.action_row, text="审核", command=self.approve_normal
            ).pack(side="left", padx=4)
        else:
            ctk.CTkButton(
                self.action_row, text="删除", fg_color="#a33", command=self.reject_abnormal
            ).pack(side="left", padx=4)
            ctk.CTkButton(self.action_row, text="编辑", command=self.open_edit_dialog).pack(
                side="left", padx=4
            )
            ctk.CTkButton(
                self.action_row,
                text="审核",
                fg_color="#1f6aa5",
                command=self.approve_abnormal,
            ).pack(side="left", padx=4)
            ctk.CTkButton(
                self.action_row, text="打开网址", command=self.open_selected_url
            ).pack(side="left", padx=4)
            ctk.CTkButton(
                self.action_row, text="实习岗全部转正常", command=self.promote_all_interns
            ).pack(side="left", padx=4)
            ctk.CTkButton(
                self.action_row, text="清空异常队列", fg_color="#a33", command=self.clear_abnormal
            ).pack(side="left", padx=4)

    def _apply_columns(self, cols: tuple[tuple[str, int, str], ...]) -> None:
        self.tree["columns"] = [CHECK_COL, *[c[0] for c in cols]]
        setup_check_column(self.tree, width=40)
        self._sort.bind_headings(
            self.tree,
            on_sorted=self.refresh,
            column_widths=cols,
        )

    def refresh(self) -> None:
        prev_sel = list(self.tree.selection())
        for i in self.tree.get_children():
            self.tree.delete(i)
        kw = (self.kw_entry.get() or "").strip().lower()
        if self._mode == "normal":
            self._fill_normal(kw)
        elif self._mode == "display":
            self._fill_display(kw)
        else:
            self._fill_abnormal(kw)
        # 保持勾选/多选（按 iid）；清空 _current_id 以便选中回调/显式加载能从库刷新侧栏
        keep = [i for i in prev_sel if self.tree.exists(i)]
        if keep:
            self._current_id = None
            self.tree.selection_set(keep)
            focus = keep[-1]
            if self.tree.exists(focus):
                self._load_form_for(focus)
        cols = self._cols
        self._sort.apply_heading_labels(
            self.tree,
            all_cols=[(k, t) for k, _w, t in cols],
            on_sorted=self.refresh,
        )
        self._update_count_label()
        # 刷新后侧栏仍须可编辑（重新识别等流程也会走这里）
        if hasattr(self, "form"):
            self.form._ensure_entries_editable()

    def _seed_name_for_job(self, job: dict[str, Any] | None) -> str | None:
        """采集种子公司名（集团门户），用于无 group_name 时的展示回退。"""
        if not job:
            return None
        cid = str(job.get("company_id") or "").strip()
        if not cid:
            return None
        cache = getattr(self, "_seed_name_cache", None)
        if cache is None:
            self._seed_name_cache = {}
            cache = self._seed_name_cache
        if cid in cache:
            return cache[cid]
        co = self.db.get_company(cid)
        name = str((co or {}).get("name") or "").strip() or None
        cache[cid] = name
        return name

    def _fill_normal(self, kw: str) -> None:
        self._normal_items.clear()
        jobs = self.db.list_jobs(status="pending_review", keyword=kw or None, limit=800)
        jobs = self._sort.sort_items(list(jobs))
        for job in jobs:
            jid = job["id"]
            self._normal_items[jid] = job
            self.tree.insert(
                "",
                "end",
                iid=jid,
                values=with_check(
                    review_row_values(job, seed_company_name=self._seed_name_for_job(job))
                ),
            )
        self._list_total = len(jobs)

    def _fill_display(self, kw: str) -> None:
        self._display_items.clear()
        jobs = self.db.list_jobs(status="active", keyword=kw or None, limit=800)
        jobs = self._sort.sort_items(list(jobs))
        for job in jobs:
            jid = job["id"]
            self._display_items[jid] = job
            self.tree.insert(
                "",
                "end",
                iid=jid,
                values=with_check(
                    (
                        *review_row_values(
                            job, seed_company_name=self._seed_name_for_job(job)
                        ),
                        cloud_upload_progress(job),
                    )
                ),
            )
        self._list_total = len(jobs)

    def _fill_abnormal(self, kw: str) -> None:
        self._abnormal_items.clear()
        items = self.db.list_review_queue("pending", limit=800)
        filtered: list[dict[str, Any]] = []
        for item in items:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            company = str(payload.get("company") or "")
            title = str(payload.get("title") or "")
            if kw and kw not in company.lower() and kw not in title.lower():
                reason = str(item.get("reason") or "").lower()
                kind = str(item.get("kind") or "").lower()
                if kw not in reason and kw not in kind:
                    continue
            filtered.append(item)
        filtered = self._sort.sort_items(filtered)
        for item in filtered:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            company = str(payload.get("company") or "")
            title = str(payload.get("title") or "")
            rid = item["id"]
            self._abnormal_items[rid] = item
            row_src = dict(payload)
            if not row_src.get("company"):
                row_src["company"] = company
            if not row_src.get("title"):
                row_src["title"] = title or (item.get("kind") or "")
            base_vals = review_row_values(
                row_src,
                seed_company_name=self._seed_name_for_job(row_src),
            )
            self.tree.insert(
                "",
                "end",
                iid=rid,
                values=with_check(
                    (
                        *base_vals,
                        item.get("kind") or "pending",
                        item.get("reason") or "",
                    )
                ),
            )
        self._list_total = len(filtered)

    def _selected_ids(self) -> list[str]:
        return list(self.tree.selection())

    def _update_count_label(self) -> None:
        sync_tree_checks(self.tree)
        n_sel = len(self._selected_ids())
        if self._mode == "display":
            base = f"岗位显示 {self._list_total} 条（active · 可推云端）"
        elif self._mode == "normal":
            base = f"待审核 {self._list_total} 条（pending_review）"
        else:
            base = f"异常队列 {self._list_total} 条（待审）"
        self.count_label.configure(text=f"{base}  ·  已选 {n_sel} 条")
        if self._mode != "normal" or not n_sel:
            self.selection_hint_var.set("")
            return
        shell_count = 0
        for item_id in self._selected_ids():
            job = self._normal_items.get(item_id) or self.db.get_job(item_id)
            if job and is_portal_shell_record(
                title=job.get("title"),
                jd_text=job.get("jd_text"),
                source_url=job.get("source_url"),
            ):
                shell_count += 1
        if shell_count:
            self.selection_hint_var.set(
                f"⚠ 选中 {shell_count} 条门户壳记录（标题像招聘首页且 JD 为空）；批量检测会按详情 UUID 直取，不会再把岗位错配到第一条。"
            )
        else:
            self.selection_hint_var.set(
                f"已选 {n_sel} 条；批量检测将保留每条岗位的详情链接，失败项会显示岗位 ID。"
            )

    def select_all(self) -> None:
        kids = self.tree.get_children()
        if kids:
            self.tree.selection_set(kids)
            self._shift_anchor = kids[0]
            self._load_form_for(kids[0])
        self._update_count_label()

    def clear_selection(self) -> None:
        self.tree.selection_remove(self.tree.selection())
        self._current_id = None
        self._current_kind = None
        self.form.clear()
        self._update_count_label()

    # ---- 鼠标单击 / Ctrl / Shift / 拖选 ----
    def _on_tree_button1(self, event: Any) -> str | None:
        if self.tree.identify_region(event.x, event.y) == "heading":
            # 交给表头 command（首列全选/取消）；其它列无 command
            self.after(1, self._update_count_label)
            return None
        row = self.tree.identify_row(event.y)
        if not row:
            return "break"
        ctrl = bool(event.state & _CTRL)
        shift = bool(event.state & _SHIFT)
        # 勾选列单击：切换加入/移出选中集（不清除其它已选）
        additive = ctrl or is_check_cell(self.tree, event)
        prev_sel = tuple(self.tree.selection()) if additive else ()
        self._drag_active = True
        self._drag_moved = False
        self._drag_anchor = row
        self._drag_ctrl = additive
        self._drag_base = prev_sel

        if shift and self._shift_anchor:
            rng = tree_range_iids(list(self.tree.get_children()), self._shift_anchor, row)
            if additive:
                merged = list(dict.fromkeys([*prev_sel, *rng]))
                self.tree.selection_set(merged)
            else:
                self.tree.selection_set(rng)
        elif additive:
            if row in self.tree.selection():
                self.tree.selection_remove(row)
            else:
                self.tree.selection_add(row)
            self._shift_anchor = row
            # 拖选延续时以切换后的集合为底，避免刚取消的行被加回
            self._drag_base = tuple(self.tree.selection())
        else:
            self.tree.selection_set(row)
            self._shift_anchor = row

        self._load_form_for(row)
        self._update_count_label()
        return "break"

    def _on_tree_b1_motion(self, event: Any) -> str | None:
        if not self._drag_active or not self._drag_anchor:
            return "break"
        # 靠近边缘时自动滚动
        height = max(self.tree.winfo_height(), 1)
        if event.y < 24:
            self.tree.yview_scroll(-1, "units")
        elif event.y > height - 24:
            self.tree.yview_scroll(1, "units")

        row = self.tree.identify_row(event.y)
        if not row:
            return "break"
        if row != self._drag_anchor:
            self._drag_moved = True
        rng = tree_range_iids(list(self.tree.get_children()), self._drag_anchor, row)
        if self._drag_ctrl:
            merged = list(dict.fromkeys([*self._drag_base, *rng]))
            self.tree.selection_set(merged)
        else:
            self.tree.selection_set(rng)
        self._shift_anchor = self._drag_anchor
        self._load_form_for(row)
        self._update_count_label()
        return "break"

    def _on_tree_button_release(self, _event: Any) -> str | None:
        self._drag_active = False
        self._drag_moved = False
        self._drag_anchor = None
        self._drag_base = ()
        self._update_count_label()
        return None

    def _on_select(self, _event: Any = None) -> None:
        # 拖选过程中由 Button/Motion 维护；仍响应键盘等触发的选中变化
        if self._drag_active:
            self._update_count_label()
            return
        sel = self._selected_ids()
        if not sel:
            self._update_count_label()
            return
        item_id = sel[-1]
        # 同一岗位重复触发选中（模态框关闭/焦点回树）时勿重载侧栏，
        # 否则会覆盖「从链接识别填充」后尚未保存的编辑内容。
        if item_id == self._current_id:
            self._update_count_label()
            return
        self._load_form_for(item_id)
        self._update_count_label()

    def _job_cache(self) -> dict[str, dict[str, Any]]:
        if self._mode == "display":
            return self._display_items
        return self._normal_items

    def _load_form_for(self, item_id: str) -> None:
        if self._is_job_list():
            # 优先读库，避免重新识别后仍显示侧栏/缓存旧值
            job = self.db.get_job(item_id) or self._job_cache().get(item_id)
            if not job:
                return
            self._job_cache()[item_id] = job
            self._current_id = item_id
            self._current_kind = "job"
            conf = job.get("confidence")
            self.form.set_meta(f"岗位 ID: {item_id}  ·  置信度: {conf}")
            # 侧栏集团与表格一致：库无 group_name 时用种子公司名回填展示
            form_job = dict(job)
            if not str(form_job.get("group_name") or "").strip():
                seed = self._seed_name_for_job(job)
                shown = display_group_name(job, seed_company_name=seed)
                if shown:
                    form_job["group_name"] = shown
            # 岗位发布时间：表单展示与表格同口径（open_at / list_updated_at / published_at）
            if not str(form_job.get("open_at") or "").strip():
                page_date = resolve_page_updated_at(job)
                if page_date:
                    form_job["open_at"] = page_date
            self.form.load(form_job)
            self.form._ensure_entries_editable()
        else:
            item = self._abnormal_items.get(item_id) or self.db.get_review_item(item_id)
            if not item:
                return
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            self._current_id = item_id
            self._current_kind = "review"
            self.form.set_meta(
                f"队列 ID: {item_id}  ·  kind={item.get('kind') or ''}  ·  {item.get('reason') or ''}"
            )
            self.form.load(payload)
            self.form._ensure_entries_editable()

    def _on_double_click(self, _event: Any = None) -> None:
        # 拖选结束后若未真正拖动，双击仍打开编辑
        if self._drag_moved:
            return
        self.open_edit_dialog()

    def _selected_id(self) -> str | None:
        sel = self._selected_ids()
        return sel[-1] if sel else self._current_id

    def _job_url_for(self, item_id: str | None) -> str:
        """优先 apply_url，否则 source_url。"""
        if not item_id:
            return ""
        if self._is_job_list():
            job = self._job_cache().get(item_id) or self.db.get_job(item_id) or {}
            return prefer_job_url(job)
        item = self._abnormal_items.get(item_id) or self.db.get_review_item(item_id) or {}
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        # 侧栏若已改链接，优先用表单当前值
        if self._current_id == item_id:
            form_url = prefer_job_url(self.form.values())
            if form_url:
                return form_url
        return prefer_job_url(payload)

    def open_selected_url(self) -> None:
        """打开选中行链接；多选时逐条打开（过多则确认）。"""
        ids = self._selected_ids()
        if not ids and self._current_id:
            ids = [self._current_id]
        if not ids:
            messagebox.showinfo("提示", "请先勾选要打开的岗位")
            return
        urls: list[str] = []
        for item_id in ids:
            url = self._job_url_for(item_id)
            if url and (url.startswith("http://") or url.startswith("https://")):
                if url not in urls:
                    urls.append(url)
        if not urls:
            messagebox.showwarning("无法打开", "选中行没有可用的网申链接或原文链接")
            return
        if len(urls) > 8:
            if not messagebox.askyesno(
                "打开网址",
                f"将打开 {len(urls)} 个链接，数量较多。继续？",
            ):
                return
        elif len(urls) > 1:
            if not messagebox.askyesno(
                "打开网址",
                f"将对选中的 {len(urls)} 条分别打开链接。继续？",
            ):
                return
        for url in urls:
            webbrowser.open(url)
        self._on_log(f"已打开网址 {len(urls)} 个")

    def open_edit_dialog(self) -> None:
        item_id = self._selected_id()
        if not item_id:
            messagebox.showinfo("提示", "请先选择一条记录")
            return
        if self._is_job_list():
            base = self._job_cache().get(item_id) or self.db.get_job(item_id) or {}
            label = "岗位显示" if self._mode == "display" else "岗位审核"
            meta = f"编辑{label} · {item_id}"
        else:
            item = self._abnormal_items.get(item_id) or self.db.get_review_item(item_id) or {}
            base = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            meta = f"编辑异常项 · {item.get('kind') or ''} · {item.get('reason') or ''}"

        def on_save(merged: dict[str, Any]) -> None:
            self._persist(item_id, merged)
            self.refresh()
            self._on_changed()

        JobEditDialog(
            self.winfo_toplevel(),
            data=base if isinstance(base, dict) else {},
            title=meta or "编辑岗位",
            on_save=on_save,
        )

    def save_current(self) -> None:
        item_id = self._current_id or self._selected_id()
        if not item_id:
            messagebox.showinfo("提示", "请先选择一条记录")
            return
        edited = self.form.values()
        if not edited.get("title") or not edited.get("source_url"):
            messagebox.showwarning("校验失败", "岗位名称与原文链接为必填")
            return
        if self._is_job_list():
            base = self._job_cache().get(item_id) or self.db.get_job(item_id) or {}
        else:
            item = self._abnormal_items.get(item_id) or self.db.get_review_item(item_id) or {}
            base = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        merged = merge_job_fields(base, edited)
        self._persist(item_id, merged)
        self._on_log("已保存修改")
        self.refresh()
        self._on_changed()

    def _persist(self, item_id: str, merged: dict[str, Any]) -> None:
        before: dict[str, Any] = {}
        if self._is_job_list():
            before = self._job_cache().get(item_id) or self.db.get_job(item_id) or {}
            data = dict(merged)
            data["id"] = item_id
            # 保持所在列表语义：审核页 pending_review；显示页 active
            keep = "active" if self._mode == "display" else "pending_review"
            data["status"] = before.get("status") or data.get("status") or keep
            if self._mode == "display":
                data["status"] = "active"
            elif self._mode == "normal":
                data["status"] = "pending_review"
            self.db.upsert_job(data)
        else:
            item = self._abnormal_items.get(item_id) or self.db.get_review_item(item_id) or {}
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            before = dict(payload or {})
            self.db.update_review_payload(item_id, merged)
        self._sync_company_seeds(before, merged)

    def _sync_company_seeds(
        self, before: dict[str, Any] | None, after: dict[str, Any] | None
    ) -> bool:
        """链接变更时覆盖公司种子 URL，避免下次采集再踩坏链。"""
        result = self.db.sync_seed_urls_from_job_fields(before, after)
        if result.get("synced"):
            self._on_log("已同步更新公司种子链接")
            return True
        return False

    def check_timeliness_selected(self) -> None:
        """近半年窗识别：窗外软删，窗内保留；缺日期则抓取源码线索。"""
        if self._mode != "normal":
            return
        if self._timeliness_busy or self._reidentify_busy:
            messagebox.showinfo("提示", "正在处理，请稍候")
            return
        ids = self._selected_ids()
        if not ids:
            messagebox.showinfo("提示", "请先勾选要识别时效性的岗位")
            return
        if not messagebox.askyesno(
            "信息时效性识别",
            f"将对选中的 {len(ids)} 条检查更新日期是否在近约 6 个月内。\n"
            "超出窗口的岗位将软删（与「删除」相同）；无日期可识别的保留。\n"
            "缺日期时会抓取网申/原文链接补全。继续？",
        ):
            return
        self._timeliness_busy = True
        self._on_log(f"正在识别信息时效性 {len(ids)} 条…")

        def worker() -> None:
            from app.collector.freshness_check import evaluate_timeliness
            from app.config import load_config

            jobs: list[dict[str, Any]] = []
            for jid in ids:
                job = self.db.get_job(jid) or self._normal_items.get(jid)
                if job:
                    jobs.append(dict(job))
            cfg = load_config()
            months = cfg.get("list_collect_months")
            try:
                report = evaluate_timeliness(
                    jobs,
                    months=int(months) if months else None,
                    fetch_missing=True,
                )
            except Exception as exc:  # noqa: BLE001
                self.after(
                    0,
                    lambda: self._timeliness_finish_error(str(exc)),
                )
                return
            self.after(0, lambda: self._timeliness_apply_report(report))

        threading.Thread(target=worker, daemon=True).start()

    def _timeliness_finish_error(self, err: str) -> None:
        self._timeliness_busy = False
        self._on_log(f"信息时效性识别失败：{err}")
        messagebox.showerror("信息时效性识别失败", err)

    def _timeliness_apply_report(self, report: Any) -> None:
        """写回新识别的 open_at；确认后软删窗外项并刷新。"""
        self._timeliness_busy = False
        # 无论是否删除，先写回新识别到的发布/更新日期
        for jid, open_at in report.writebacks:
            job = self.db.get_job(jid)
            if not job:
                continue
            if not (job.get("open_at") or "").strip():
                job["open_at"] = open_at
                job["status"] = job.get("status") or "pending_review"
                self.db.upsert_job(job)

        delete_ids = list(report.delete_ids)
        preview = report.summary_zh()
        n_del = 0
        if delete_ids:
            if not messagebox.askyesno(
                "确认删除过时岗位",
                f"{preview}\n\n确认将窗外 {len(delete_ids)} 条软删？\n"
                "（窗内与无日期项不会删除）",
            ):
                self._on_log("已取消删除；已写回识别到的日期（如有）")
                self.refresh()
                self._on_changed()
                messagebox.showinfo(
                    "信息时效性识别",
                    f"已取消删除。\n{preview}",
                )
                return
            n_del = self.db.soft_delete_jobs(delete_ids)

        keep_n = len(report.keep_ids)
        msg = f"信息时效性识别完成：保留 {keep_n}，删除 {n_del}"
        self._on_log(msg)
        self.form.clear()
        self._current_id = None
        self.refresh()
        self._on_changed()
        messagebox.showinfo("信息时效性识别", f"{msg}\n\n{preview}")

    def _import_fill_candidates_to_review(self, candidates: list[FillCandidate]) -> None:
        """从链接识别：仅将勾选候选写入 pending_review（不导入未勾选）。"""
        if not candidates:
            return
        cur: dict[str, Any] | None = None
        seed = None
        if self._current_id:
            cur = self.db.get_job(self._current_id) or self._normal_items.get(
                self._current_id
            )
            seed = self._seed_name_for_job(cur)
        # 原行只更新一次：勾选中与当前行严格匹配的那条（无匹配则全部新增）
        original_match = (
            find_matching_fill_candidate(
                candidates,
                title=cur.get("title"),
                source_url=cur.get("source_url"),
                apply_url=cur.get("apply_url"),
            )
            if cur
            else None
        )

        written = 0
        updated = 0
        inserted = 0
        focus_id: str | None = self._current_id
        for cand in candidates:
            fields = cand.fields or {}
            if fields_look_like_no_job_posting(fields):
                continue
            if (
                original_match is cand
                and self._current_id
                and cur
            ):
                merged = apply_reidentify_fields(cur, fields, seed_company_name=seed)
                merged["id"] = self._current_id
                merged["status"] = "pending_review"
                self.db.upsert_job(merged)
                self._sync_company_seeds(cur, merged)
                self._normal_items[self._current_id] = (
                    self.db.get_job(self._current_id) or merged
                )
                focus_id = self._current_id
                updated += 1
            else:
                base: dict[str, Any] = {
                    "company": (fields.get("company") or (cur or {}).get("company") or "")
                    or "未知企业",
                    "group_name": (cur or {}).get("group_name"),
                    "status": "pending_review",
                }
                merged = apply_reidentify_fields(base, fields, seed_company_name=seed)
                merged.pop("id", None)
                merged["status"] = "pending_review"
                jid = self.db.upsert_job(merged)
                self._sync_company_seeds(base, merged)
                focus_id = jid
                inserted += 1
            written += 1
        self.refresh()
        if focus_id and self.tree.exists(focus_id):
            self.tree.selection_set(focus_id)
            self._load_form_for(focus_id)
        self._on_changed()
        self._on_log(
            f"从链接识别：勾选写入岗位审核 {written} 条"
            f"（更新原行 {updated} / 新增 {inserted}；未勾选未导入）"
        )

    def _set_reidentify_busy(self, busy: bool) -> None:
        """仅禁用「重新识别」按钮；其它页签/操作仍可用。"""
        self._reidentify_busy = busy
        btn = self._reidentify_btn
        if btn is None:
            return
        try:
            btn.configure(state="disabled" if busy else "normal")
            btn.configure(text="局部重采中…" if busy else "重新识别（局部重采）")
        except Exception:
            pass

    def _reidentify_log(self, msg: str) -> None:
        """Process progress already arrives on the Tk thread via polling."""
        self._on_log(msg)

    @staticmethod
    def _reidentify_limits() -> tuple[int, float, int | None]:
        from app.config import load_config

        cfg = load_config()
        try:
            batch_size = max(1, int(cfg.get("reidentify_collect_batch") or 15))
        except (TypeError, ValueError):
            batch_size = 15
        try:
            timeout = max(1.0, float(cfg.get("reidentify_detail_timeout") or 45))
        except (TypeError, ValueError):
            timeout = 45.0
        raw_months = cfg.get("list_collect_months")
        try:
            months = int(raw_months) if raw_months else None
        except (TypeError, ValueError):
            months = None
        return batch_size, timeout, months

    def _start_reidentify_process(self, task: dict[str, Any]) -> None:
        try:
            process, message_queue = start_reidentify_process(self.db.path, task)
        except Exception as exc:  # noqa: BLE001
            self._reidentify_finish(0, 0, 1, [str(exc)], focus_id=None)
            return
        self._reidentify_process = process
        self._reidentify_message_queue = message_queue
        self._reidentify_pending_payload = None
        self._reidentify_exit_poll_count = 0
        self.after(100, self._poll_reidentify_process)

    def _poll_reidentify_process(self) -> None:
        process = self._reidentify_process
        message_queue = self._reidentify_message_queue
        if process is None or message_queue is None:
            return
        saw_finished = False
        item_events: list[dict[str, Any]] = []
        for kind, payload in drain_reidentify_messages(message_queue, limit=200):
            if kind == "progress":
                self._reidentify_log(str(payload))
            elif kind == "item" and isinstance(payload, dict):
                item_events.append(payload)
            elif kind in ("discovery", "result"):
                self._reidentify_pending_payload = (kind, payload)
            elif kind == "error":
                self._reidentify_pending_payload = ("error", payload)
            elif kind == "finished":
                saw_finished = True

        if item_events:
            self._apply_reidentify_item_events(item_events)

        if process.is_alive() and not saw_finished:
            self.after(150, self._poll_reidentify_process)
            return
        process.join(timeout=0.1)
        if (
            self._reidentify_pending_payload is None
            and not saw_finished
            and self._reidentify_exit_poll_count < 3
        ):
            self._reidentify_exit_poll_count += 1
            self.after(100, self._poll_reidentify_process)
            return

        pending = self._reidentify_pending_payload
        try:
            message_queue.close()
        except Exception:
            pass
        self._reidentify_process = None
        self._reidentify_message_queue = None
        self._reidentify_pending_payload = None
        if pending is None:
            self._reidentify_finish(
                0,
                0,
                1,
                [f"局部重采进程异常结束（退出码 {process.exitcode}）"],
                focus_id=None,
            )
            return
        self.after(0, lambda p=pending: self._handle_reidentify_process_payload(*p))

    def _handle_reidentify_process_payload(self, kind: str, payload: Any) -> None:
        if kind == "error":
            message = payload.get("message") if isinstance(payload, dict) else payload
            self._reidentify_finish(0, 0, 1, [str(message or "局部重采失败")], focus_id=None)
            return
        if kind == "discovery" and isinstance(payload, dict):
            result_payload = payload.get("result")
            result = deserialize_result(result_payload if isinstance(result_payload, dict) else {})
            self._reidentify_single_resolved(
                str(payload.get("item_id") or ""),
                dict(payload.get("job") or {}),
                result,
                self.winfo_toplevel(),
                batch_size=int(payload.get("batch_size") or 15),
            )
            return
        if kind == "result" and isinstance(payload, dict):
            self._reidentify_finish(
                int(payload.get("ok_n") or 0),
                int(payload.get("del_n") or 0),
                int(payload.get("fail_n") or 0),
                [str(x) for x in payload.get("errors") or []],
                focus_id=str(payload.get("focus_id") or "") or None,
                notes=[str(x) for x in payload.get("notes") or []],
            )

    def _apply_reidentify_item_events(self, events: list[dict[str, Any]]) -> None:
        """Apply one poll's committed rows without rebuilding the whole table."""
        changed = 0
        for event in events:
            action = str(event.get("action") or "")
            job_id = str(event.get("job_id") or "")
            if action == "deleted" and job_id:
                self._normal_items.pop(job_id, None)
                if self.tree.exists(job_id):
                    self.tree.delete(job_id)
                if self._current_id == job_id:
                    self.form.clear()
                    self._current_id = None
                changed += 1
                continue
            if action not in ("inserted", "updated") or not job_id:
                continue
            job = self.db.get_job(job_id)
            if not job or job.get("status") != "pending_review":
                continue
            kw = (self.kw_entry.get() or "").strip().lower()
            haystack = " ".join(
                str(job.get(key) or "")
                for key in ("company", "group_name", "title", "work_location", "jd_text")
            ).lower()
            if kw and kw not in haystack:
                continue
            self._normal_items[job_id] = job
            values = with_check(
                review_row_values(job, seed_company_name=self._seed_name_for_job(job)),
                selected=self.tree.exists(job_id) and job_id in self.tree.selection(),
            )
            if self.tree.exists(job_id):
                self.tree.item(job_id, values=values)
            else:
                self.tree.insert("", "end", iid=job_id, values=values)
            changed += 1
        if changed:
            self._list_total = len(self._normal_items)
            self._update_count_label()
            self._on_changed()
            self._on_log(f"局部重采：本轮已有 {changed} 条完成并进入岗位审核")

    def deduplicate_selected(self) -> None:
        """在所选岗位所属公司范围内识别并软删重复岗位。"""
        if self._mode != "normal":
            return
        ids = self._selected_ids()
        if not ids:
            messagebox.showinfo("提示", "请先选择要去重的岗位")
            return
        jobs = [self.db.get_job(item_id) for item_id in ids]
        company_keys = {
            str(job.get("company_id") or job.get("company") or "").strip()
            for job in jobs
            if job and str(job.get("company_id") or job.get("company") or "").strip()
        }
        if not company_keys:
            messagebox.showinfo("提示", "所选岗位没有可用于去重的公司信息")
            return
        if not messagebox.askyesno(
            "岗位去重",
            f"将在所选岗位所属的 {len(company_keys)} 家公司内识别重复岗位。\n"
            "同公司岗位名称归一化后相同的记录视为重复；将保留 JD 更完整、置信度更高的一条，"
            "其余记录软删除。继续？",
        ):
            return
        result = deduplicate_jobs(self.db, company_keys=company_keys)
        deleted = int(result.get("deleted") or 0)
        self.refresh()
        self._on_changed()
        msg = (
            f"岗位去重完成：识别并软删除 {deleted} 条重复岗位。"
            if deleted
            else "岗位去重完成：所选岗位所属公司内未发现重复岗位。"
        )
        self._on_log(msg)
        messagebox.showinfo("岗位去重", msg)

    def reidentify_selected(self) -> None:
        """局部重采：以链接展开校招+实习岗，勾选后仅导入所选 → pending_review。"""
        if self._mode != "normal":
            return
        if self._reidentify_busy:
            messagebox.showinfo("提示", "正在局部重采，请稍候；其它操作仍可继续")
            return
        ids = self._selected_ids()
        if not ids:
            messagebox.showinfo("提示", "请先选择要重新识别的岗位")
            return
        batch_n, timeout, months = self._reidentify_limits()
        if len(ids) > 1:
            if not messagebox.askyesno(
                "重新识别（局部重采）",
                f"将对选中的 {len(ids)} 条做局部重新采集（非全库深度采集）。\n"
                "展开同站「校园招聘 + 实习生招聘」近约半年岗位；\n"
                f"每家企业每批最多 {batch_n} 个未入库岗，其余下次再采。\n"
                "勾选将新增到岗位审核；仅与原行匹配的一条覆盖原行。\n"
                "页面无岗则软删。多选行自动对齐。界面保持可用。网络失败不删。继续？",
            ):
                return
        elif not messagebox.askyesno(
            "重新识别（局部重采）",
            "将以当前行链接做局部重新采集（非全库深度采集）。\n"
            f"展开同站校招+实习近约半年岗位；每批最多 {batch_n} 个未入库岗。\n"
            "勾选将新增到岗位审核；仅与原行严格匹配的一条会覆盖原行。\n"
            "若还有剩余请再次重新识别。页面无岗则软删。界面保持可用。网络失败不删。继续？",
        ):
            return

        # 主线程快照：禁止 worker 读 Tk 控件 / 共享 UI 状态
        snapshots: list[tuple[str, dict[str, Any], str | None]] = []
        for item_id in ids:
            job = self.db.get_job(item_id) or self._normal_items.get(item_id)
            if not job:
                snapshots.append((item_id, {}, None))
                continue
            job = dict(job)
            if self._current_id == item_id:
                job = merge_job_fields(job, self.form.values())
            seed = self._seed_name_for_job(job)
            snapshots.append((item_id, job, seed))

        self._set_reidentify_busy(True)
        self._on_log(f"局部重采：发现中…（{len(ids)} 条入口，可继续使用其它功能）")

        # 单选：门户发现 + 多选勾选导入
        if len(ids) == 1:
            item_id, job, seed = snapshots[0]
            self._start_reidentify_process(
                {
                    "mode": "discover",
                    "item_id": item_id,
                    "job": job,
                    "seed_company_name": seed,
                    "batch_size": batch_n,
                    "timeout": timeout,
                    "list_collect_months": months,
                }
            )
            return
        self._start_reidentify_process(
            {
                "mode": "batch",
                "snapshots": [
                    {
                        "item_id": item_id,
                        "job": job,
                        "seed_company_name": seed,
                    }
                    for item_id, job, seed in snapshots
                ],
                "batch_size": batch_n,
                "timeout": timeout,
                "list_collect_months": months,
            }
        )

    def _jobs_known_for_company(self, job: dict[str, Any] | None) -> list[dict[str, Any]]:
        """同公司已入库岗位（待审/显示/异常），用于分批跳过。"""
        co = str((job or {}).get("company") or "").strip()
        if not co:
            return []
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for st in ("pending_review", "active", "abnormal"):
            for row in self.db.list_jobs(status=st, keyword=co, limit=5000):
                if str(row.get("company") or "").strip() != co:
                    continue
                jid = str(row.get("id") or "")
                if jid and jid in seen:
                    continue
                if jid:
                    seen.add(jid)
                out.append(row)
        return out

    def _reidentify_single_resolved(
        self,
        item_id: str,
        job: dict[str, Any],
        result: FillFromUrlResult,
        root: Any,
        *,
        batch_size: int | None = None,
    ) -> None:
        if not result.ok or not result.candidates:
            if resolve_result_is_no_job_posting(result):
                self._reidentify_soft_delete_one(
                    item_id, job, note="页面无招聘信息"
                )
                return
            self._reidentify_finish(
                0, 0, 1, [result.error or "未能识别岗位信息"], focus_id=item_id
            )
            return
        batch_n = (
            int(batch_size)
            if batch_size is not None
            else self._reidentify_limits()[0]
        )
        known_urls, known_titles = known_keys_from_jobs(self._jobs_known_for_company(job))
        # 当前行本身允许出现在候选里（用于覆盖原行），勿当「已入库」跳过
        for key in ("apply_url", "source_url"):
            known_urls.discard(
                (str(job.get(key) or "").strip().split("?")[0].rstrip("/").lower())
            )
        known_titles.discard(str(job.get("title") or "").strip().lower())

        candidates, batch_stats = take_collect_batch(
            list(result.candidates),
            known_urls=known_urls,
            known_titles=known_titles,
            batch_size=batch_n,
        )
        if not candidates:
            # 扫描到的都已入库：提示再次识别或已采完
            msg = (
                f"本批无可新增岗位（已跳过已入库 {batch_stats.get('skipped_known', 0)} 个；"
                f"扫描 {batch_stats.get('scanned', 0)} 个）。"
                "若企业仍有更多岗，请稍后再重新识别或检查是否已全部入库。"
            )
            self._set_reidentify_busy(False)
            self._on_log(msg)
            messagebox.showinfo("重新识别", msg)
            return

        preselect: list[int] = []
        matched = find_matching_fill_candidate(
            candidates,
            title=job.get("title"),
            source_url=job.get("source_url"),
            apply_url=job.get("apply_url"),
        )
        if matched is not None and matched in candidates:
            # 原岗匹配可预勾；其余「超出近半年」默认不勾
            preselect = [candidates.index(matched)]
        else:
            preselect = [
                i for i, c in enumerate(candidates) if not getattr(c, "outside_lookback", False)
            ]
            # 窗内过多时不默认全勾，避免误导入；仅 1 条窗内时预勾
            if len(preselect) != 1:
                preselect = []
        outside_n = sum(
            1 for c in candidates if getattr(c, "outside_lookback", False)
        )
        remain = int(batch_stats.get("remaining_after_batch") or 0)
        skipped = int(batch_stats.get("skipped_known") or 0)
        notice_parts = [
            f"本批最多 {batch_n} 个未入库岗（已选入 {len(candidates)}）；"
            f"已跳过已入库 {skipped}；本扫描后大约还剩 {remain} 个可下次再采。"
        ]
        if outside_n:
            notice_parts.append(
                f"其中 {outside_n} 个标注「超出近半年」，默认不勾选，仍可手动勾选。"
            )
        notice = " ".join(notice_parts)
        log_msg = f"局部重采：待勾选本批 {len(candidates)} 个…"
        self._on_log(log_msg + " " + notice)
        chosen_list = pick_job_candidates(
            root,
            candidates,
            title="局部重采 · 勾选岗位",
            preselect=preselect,
            hint=(
                notice
                + "\n勾选岗位将新增到岗位审核；仅与原行严格匹配的一条会覆盖原行。\n"
                "未勾选的不会导入。有剩余请再次「重新识别」。"
            ),
        )
        if chosen_list is None:
            self._set_reidentify_busy(False)
            self._on_log("局部重采已取消")
            return
        if not chosen_list:
            self._set_reidentify_busy(False)
            self._on_log("局部重采：未勾选任何岗位")
            messagebox.showinfo("重新识别", "未勾选任何岗位，未写入。")
            return

        found_n = len(candidates)
        selected_n = len(chosen_list)
        self._on_log(f"局部重采：写入中…（已勾选 {selected_n}/{found_n}，抓取详情）")
        seed = self._seed_name_for_job(job)
        page_url = result.page_url or pick_fill_url(
            job.get("apply_url"), job.get("source_url")
        )
        _batch_n, timeout, _months = self._reidentify_limits()
        self._start_reidentify_process(
            {
                "mode": "selected",
                "item_id": item_id,
                "before": job,
                "seed_company_name": seed,
                "page_url": page_url,
                "candidates": [serialize_candidate(c) for c in chosen_list],
                "found_n": found_n,
                "selected_n": selected_n,
                "timeout": timeout,
            }
        )

    def _reidentify_soft_delete_one(
        self,
        item_id: str,
        job: dict[str, Any] | None,
        *,
        note: str = "页面无招聘信息",
    ) -> None:
        title = str((job or {}).get("title") or item_id)[:24]
        try:
            n = self.db.soft_delete_jobs([item_id])
        except Exception as exc:  # noqa: BLE001
            self._reidentify_finish(
                0, 0, 1, [f"{title}：删除失败：{exc}"], focus_id=None
            )
            return
        if not n:
            self._reidentify_finish(
                0, 0, 1, [f"{title}：删除失败"], focus_id=None
            )
            return
        self._normal_items.pop(item_id, None)
        if self._current_id == item_id:
            self.form.clear()
            self._current_id = None
        self._reidentify_finish(
            0,
            1,
            0,
            [],
            focus_id=None,
            notes=[f"{title}：{note}，已删除"],
        )

    def _reidentify_apply_one(
        self,
        item_id: str,
        before: dict[str, Any],
        candidate: FillCandidate,
        seed_company_name: str | None = None,
    ) -> None:
        self._reidentify_apply_selected(
            item_id,
            before,
            [candidate],
            seed_company_name,
            found_n=1,
            selected_n=1,
        )

    def _reidentify_apply_selected(
        self,
        item_id: str,
        before: dict[str, Any],
        chosen: list[FillCandidate],
        seed_company_name: str | None = None,
        *,
        found_n: int,
        selected_n: int,
    ) -> None:
        """仅写入勾选候选：匹配原行则更新，其余新增 pending_review。"""
        if not chosen:
            self._set_reidentify_busy(False)
            self._on_log("重新识别：未勾选任何岗位")
            return
        seed = seed_company_name or self._seed_name_for_job(before)
        # 勾选中与原行严格匹配的那条用于更新原 id；无匹配则全部新增
        original_match = find_matching_fill_candidate(
            chosen,
            title=before.get("title"),
            source_url=before.get("source_url"),
            apply_url=before.get("apply_url"),
        )

        ok_n = 0
        fail_n = 0
        inserted_n = 0
        updated_n = 0
        errors: list[str] = []
        notes: list[str] = []
        focus_id: str | None = item_id
        updated_original = False

        for cand in chosen:
            fields = cand.fields or {}
            if fields_look_like_no_job_posting(fields):
                # 勾选了空壳：若是唯一且匹配原行 → 软删；否则跳过该条
                if len(chosen) == 1 and (
                    original_match is cand or original_match is None
                ):
                    self._reidentify_soft_delete_one(
                        item_id, before, note="页面无招聘信息"
                    )
                    return
                fail_n += 1
                errors.append(
                    f"{(fields.get('title') or '未命名')[:20]}：无有效岗位信息，已跳过"
                )
                continue
            try:
                if original_match is cand and not updated_original:
                    merged = apply_reidentify_fields(
                        before, fields, seed_company_name=seed
                    )
                    merged["id"] = item_id
                    merged["status"] = "pending_review"
                    self.db.upsert_job(merged)
                    self._sync_company_seeds(before, merged)
                    self._normal_items[item_id] = self.db.get_job(item_id) or merged
                    changes = summarize_reidentify_changes(before, merged)
                    if changes:
                        note = "原行已更新 " + "；".join(changes[:4])
                    else:
                        note = "原行字段无实质变更"
                    notes.append(note)
                    updated_original = True
                    updated_n = 1
                    focus_id = item_id
                    ok_n += 1
                else:
                    base: dict[str, Any] = {
                        "company": (
                            fields.get("company")
                            or before.get("company")
                            or "未知企业"
                        ),
                        "group_name": before.get("group_name"),
                        "status": "pending_review",
                    }
                    merged = apply_reidentify_fields(
                        base, fields, seed_company_name=seed
                    )
                    merged.pop("id", None)
                    merged["status"] = "pending_review"
                    jid = self.db.upsert_job(merged)
                    self._sync_company_seeds(base, merged)
                    title = str(merged.get("title") or jid)[:24]
                    notes.append(f"新增待审：{title}")
                    focus_id = jid
                    inserted_n += 1
                    ok_n += 1
            except Exception as exc:  # noqa: BLE001
                fail_n += 1
                errors.append(f"保存失败：{exc}")

        # 强制突出新增数，避免误以为「只改了一行」
        summary = (
            f"识别到 {found_n} 个，已勾选 {selected_n} 个；"
            f"新增 {inserted_n} / 更新原行 {updated_n} / 失败 {fail_n}"
        )
        notes.insert(0, summary)
        self._reidentify_finish(
            ok_n, 0, fail_n, errors, focus_id=focus_id, notes=notes
        )

    def strip_jd_duplicates_selected(self) -> None:
        """剥掉 JD 中与结构化字段重复的页眉/元信息行。"""
        if self._mode != "normal":
            return
        ids = self._selected_ids()
        if not ids:
            messagebox.showinfo("提示", "请先选择要处理的岗位")
            return
        if not messagebox.askyesno(
            "重复信息处理",
            f"将对选中的 {len(ids)} 条清理 JD 中与岗位名称、招聘类别、工作性质、"
            "工作地点、薪资范围、招聘人数等已识别字段重复的页眉行，"
            "保留工作职责/任职要求正文。继续？",
        ):
            return
        n = 0
        summaries: list[str] = []
        for item_id in ids:
            job = self.db.get_job(item_id)
            if not job:
                continue
            old_jd = str(job.get("jd_text") or "")
            labels = extract_labeled_fields(old_jd)
            edu = str(job.get("education") or "").strip()
            if not edu:
                edu = (
                    extract_education_requirement(
                        old_jd,
                        labels=labels,
                        title=str(job.get("title") or "") or None,
                    )
                    or ""
                )
            salary = str(job.get("salary_range") or "").strip()
            if not salary:
                salary = normalize_salary_range(labels.get("salary_range")) or ""
            headcount = str(job.get("headcount") or "").strip()
            if not headcount:
                headcount = normalize_headcount(labels.get("headcount")) or ""
            cleaned = strip_redundant_jd_meta(
                old_jd,
                title=job.get("title"),
                company=job.get("company"),
                recruit_project=job.get("recruit_project"),
                recruit_bucket=job.get("recruit_bucket"),
                work_location=job.get("work_location"),
                raw_category=job.get("raw_category"),
                education=edu or job.get("education"),
                salary_range=salary or job.get("salary_range"),
                headcount=headcount or job.get("headcount"),
            )
            if cleaned is None:
                cleaned = ""
            changed = cleaned.strip() != old_jd.strip()
            edu_changed = bool(edu and edu != str(job.get("education") or "").strip())
            sal_changed = bool(
                salary and salary != str(job.get("salary_range") or "").strip()
            )
            hc_changed = bool(
                headcount and headcount != str(job.get("headcount") or "").strip()
            )
            if changed or edu_changed or sal_changed or hc_changed:
                job["jd_text"] = cleaned or None
                if edu:
                    job["education"] = edu
                if salary:
                    job["salary_range"] = salary
                if headcount:
                    job["headcount"] = headcount
                job["id"] = item_id
                job["status"] = job.get("status") or "pending_review"
                self.db.upsert_job(job)
                n += 1
                title = str(job.get("title") or item_id)[:20]
                parts = [summarize_jd_dedupe(old_jd, cleaned)]
                if edu_changed:
                    parts.append(f"补学历={edu}")
                if sal_changed:
                    parts.append(f"补薪资={salary}")
                if hc_changed:
                    parts.append(f"补人数={headcount}")
                summaries.append(f"{title}：{'；'.join(parts)}")
        if n == 0:
            msg = "重复信息处理完成：选中岗位无需清理（JD 已无重复页眉）"
        else:
            detail = "\n".join(summaries[:8])
            if len(summaries) > 8:
                detail += f"\n…共 {len(summaries)} 条"
            msg = f"重复信息处理完成：已更新 {n} 条\n{detail}"
        self._on_log(msg.split("\n", 1)[0])
        self.refresh()
        focus = ids[0] if ids and self.tree.exists(ids[0]) else None
        if focus:
            self.tree.selection_set(focus)
            self._load_form_for(focus)
        self._on_changed()
        messagebox.showinfo("重复信息处理", msg)

    def campus_recognition_selected(self) -> None:
        """保留校招/实习岗，软删明确社招岗。"""
        if self._mode != "normal":
            return
        ids = self._selected_ids()
        if not ids:
            messagebox.showinfo("提示", "请先选择要识别的岗位")
            return
        keep_ids: list[str] = []
        delete_ids: list[str] = []
        for item_id in ids:
            job = self.db.get_job(item_id) or self._normal_items.get(item_id)
            if not job:
                continue
            if campus_recognition_action(job) == "delete":
                delete_ids.append(item_id)
            else:
                keep_ids.append(item_id)
        if not delete_ids:
            msg = f"校园招聘识别：保留 {len(keep_ids)}，删除 0（未发现社招岗）"
            self._on_log(msg)
            messagebox.showinfo("校园招聘识别", msg)
            return
        if not messagebox.askyesno(
            "校园招聘识别",
            f"选中 {len(ids)} 条中：保留校招/实习等 {len(keep_ids)} 条，\n"
            f"将软删明确「社会招聘」信号的 {len(delete_ids)} 条（与「删除」相同）。继续？",
        ):
            return
        n_del = self.db.soft_delete_jobs(delete_ids)
        msg = f"校园招聘识别完成：保留 {len(keep_ids)}，删除 {n_del}"
        self._on_log(msg)
        self.form.clear()
        self._current_id = None
        self.refresh()
        self._on_changed()
        messagebox.showinfo("校园招聘识别", msg)

    def _reidentify_finish(
        self,
        ok_n: int,
        del_n: int,
        fail_n: int,
        errors: list[str],
        *,
        focus_id: str | None,
        notes: list[str] | None = None,
    ) -> None:
        self._set_reidentify_busy(False)
        parts = [
            f"局部重采完成：写入 {ok_n} / 删除 {del_n}（页面无招聘信息）/ 失败 {fail_n}"
        ]
        if errors:
            parts.append("；".join(errors[:5]))
            if len(errors) > 5:
                parts.append(f"等共 {len(errors)} 条失败")
        msg = "。".join(parts) if len(parts) > 1 else parts[0]
        note_blob = ""
        if notes:
            note_blob = "\n".join(notes[:5])
            if len(notes) > 5:
                note_blob += f"\n…共 {len(notes)} 条说明"
        self._on_log(msg if not note_blob else f"{msg} | {notes[0]}")
        self.refresh()
        if focus_id and self.tree.exists(focus_id):
            self.tree.selection_set(focus_id)
            self._load_form_for(focus_id)
        elif del_n and not ok_n:
            self.form.clear()
            self._current_id = None
        self._on_changed()
        detail = f"{msg}\n{note_blob}".strip() if note_blob else msg
        if fail_n and not ok_n and not del_n:
            messagebox.showerror("重新识别失败", detail)
        elif fail_n:
            messagebox.showwarning("重新识别部分失败", detail)
        elif del_n and not ok_n:
            messagebox.showinfo(
                "重新识别完成",
                f"{detail}\n\n已软删无招聘信息的岗位；夜间采集会再爬相关公司链接。",
            )
        else:
            tip = (
                "\n\n请核对侧栏字段（岗位名称/薪资/人数/地点/JD/学历等），"
                "仍可人工修改后保存或点「审核」。"
            )
            if del_n:
                tip = (
                    f"\n\n其中 {del_n} 条因页面无招聘信息已软删；"
                    "其余请核对侧栏字段后保存或审核。"
                )
            messagebox.showinfo("重新识别完成", f"{detail}{tip}")


    def auto_approve_normal(self) -> None:
        """Strictly approve complete, recent jobs; leave all other rows pending."""
        selected_ids = self._selected_ids()
        if selected_ids:
            jobs = self.db.get_jobs_by_ids(selected_ids)
            scope = f"选中的 {len(jobs)} 条"
        else:
            jobs = self.db.list_jobs(status="pending_review", limit=5000)
            scope = f"全部待审核 {len(jobs)} 条"
        jobs = [job for job in jobs if job.get("status") == "pending_review"]
        if not jobs:
            messagebox.showinfo("自动审核", "没有可检查的待审核岗位")
            return

        report = assess_jobs_for_auto_review(jobs, months=3)
        eligible = set(report.eligible_ids)
        if not eligible:
            messagebox.showinfo(
                "自动审核",
                f"检查范围：{scope}\n\n{report.summary_zh()}",
            )
            return
        if not messagebox.askyesno(
            "自动审核",
            f"检查范围：{scope}\n\n{report.summary_zh()}\n\n"
            f"确认将符合条件的 {len(eligible)} 条移入「岗位显示」？",
        ):
            return

        approved = 0
        for job in jobs:
            item_id = str(job.get("id") or "")
            if item_id not in eligible:
                continue
            job["status"] = "active"
            self.db.upsert_job(job)
            approved += 1
        self.db.remove_cloud_revoke_ids(list(eligible))
        msg = (
            f"自动审核完成：通过 {approved} 条，"
            f"保留人工审核 {len(report.skipped)} 条"
        )
        self._on_log(msg)
        self.form.clear()
        self._current_id = None
        self.refresh()
        self._on_changed()
        messagebox.showinfo("自动审核完成", f"{msg}\n\n{report.summary_zh()}")

    def approve_normal(self) -> None:
        """审核通过：pending_review → active，离开岗位审核，进入岗位显示。"""
        ids = self._selected_ids()
        if not ids:
            messagebox.showinfo("提示", "请先选择要审核的岗位")
            return
        if len(ids) > 1:
            if not messagebox.askyesno(
                "审核",
                f"确认将选中的 {len(ids)} 条审核通过并移入「岗位显示」？",
            ):
                return
        ok = 0
        seed_synced = 0
        for item_id in ids:
            job = self.db.get_job(item_id)
            if not job:
                continue
            before = dict(job)
            if self._current_id == item_id and len(ids) == 1:
                edited = self.form.values()
                if edited.get("title") and edited.get("source_url"):
                    job = merge_job_fields(job, edited)
            job["id"] = item_id
            job["status"] = "active"
            self.db.upsert_job(job)
            # 再次审核通过：取消待撤云标记
            self.db.remove_cloud_revoke_ids([item_id])
            if self._sync_company_seeds(before, job):
                seed_synced += 1
            ok += 1
        msg = f"已审核通过并移入「岗位显示」{ok} 条"
        if seed_synced:
            msg += f"；已同步更新公司种子链接 {seed_synced} 家"
        self._on_log(msg)
        self.form.clear()
        self._current_id = None
        self.refresh()
        self._on_changed()

    def reject_normal(self) -> None:
        ids = self._selected_ids()
        if not ids:
            messagebox.showinfo("提示", "请先选择要删除的岗位")
            return
        if not messagebox.askyesno(
            "删除",
            f"确认将选中的 {len(ids)} 条岗位标记为 deleted（软删）？",
        ):
            return
        n = self.db.soft_delete_jobs(ids)
        self._on_log(f"已删除 {n} 条")
        self.form.clear()
        self._current_id = None
        self.refresh()
        self._on_changed()

    def reject_display(self) -> None:
        """岗位显示页软删。"""
        self.reject_normal()

    def unapprove_display(self) -> None:
        """反审核：active → pending_review，退回岗位审核；曾推送过的记入下次撤云。"""
        if self._mode != "display":
            return
        ids = self._selected_ids()
        if not ids:
            messagebox.showinfo("提示", "请先选择要反审核的岗位")
            return
        if not messagebox.askyesno(
            "反审核",
            f"确认将选中的 {len(ids)} 条退回「岗位审核」？\n\n"
            "· 将离开「岗位显示」，可再编辑 / 重新识别后审核\n"
            "· 云端上传进度将重置为「未推送」\n"
            "· 若此前已推送云端，下次点「推送云端」时会以 deleted 覆盖撤下，避免 Viewer 继续看到错误岗",
        ):
            return
        result = self.db.revert_jobs_to_pending_review(ids)
        n = int(result.get("reverted") or 0)
        revoke_n = int(result.get("revoke_queued") or 0)
        msg = f"已反审核 {n} 条，已退回「岗位审核」"
        if revoke_n:
            msg += f"；其中 {revoke_n} 条曾推送，将在下次「推送云端」时从云端撤下"
        self._on_log(msg)
        self.form.clear()
        self._current_id = None
        self.refresh()
        self._on_changed()
        messagebox.showinfo("反审核完成", msg)

    def push_selected_or_all(self) -> None:
        """岗位显示：有勾选则推选中，否则推全部显示中岗位（含软删同步）。"""
        if self._on_push_cloud is None:
            messagebox.showwarning("不可用", "未绑定推送云端回调")
            return
        ids = self._selected_ids()
        self._on_push_cloud(ids if ids else None)

    def approve_abnormal(self) -> None:
        ids = self._selected_ids()
        if not ids:
            messagebox.showinfo("提示", "请先选择要审核的异常项")
            return
        if len(ids) > 1:
            if not messagebox.askyesno(
                "审核",
                f"确认将选中的 {len(ids)} 条异常项发布到「岗位显示」？",
            ):
                return
        ok_n = 0
        fail_n = 0
        seed_hint = False
        for item_id in ids:
            override = None
            item = self._abnormal_items.get(item_id) or self.db.get_review_item(item_id) or {}
            base = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if self._current_id == item_id and len(ids) == 1:
                edited = self.form.values()
                if edited.get("title") and edited.get("source_url"):
                    override = merge_job_fields(base, edited)
            url_edited = False
            if override is not None:
                for key in ("source_url", "apply_url"):
                    if (base.get(key) or "").strip() != (override.get(key) or "").strip():
                        url_edited = True
                        break
            if publish_review_item(self.db, item_id, payload_override=override):
                ok_n += 1
                if url_edited:
                    seed_hint = True
            else:
                fail_n += 1
        if fail_n and not ok_n:
            messagebox.showwarning("审核失败", "需要有效的岗位名称与原文链接")
            return
        msg = f"已审核通过并进入「岗位显示」{ok_n} 条"
        if fail_n:
            msg += f"，失败 {fail_n} 条（缺标题或链接）"
        if seed_hint:
            msg += "；已同步更新公司种子链接"
        self._on_log(msg)
        self.form.clear()
        self._current_id = None
        self.refresh()
        self._on_changed()

    def reject_abnormal(self) -> None:
        ids = self._selected_ids()
        if not ids:
            messagebox.showinfo("提示", "请先选择要删除的异常项")
            return
        if not messagebox.askyesno(
            "删除",
            f"确认删除/忽略选中的 {len(ids)} 条异常项？",
        ):
            return
        for item_id in ids:
            self.db.resolve_review(item_id, "ignored")
        self._on_log(f"已删除异常项 {len(ids)} 条")
        self.form.clear()
        self._current_id = None
        self.refresh()
        self._on_changed()

    def promote_all_interns(self) -> None:
        from app.collector.promote_intern import promote_internship_from_review

        if not messagebox.askyesno("实习岗转正常", "将异常队列中所有实习相关条目发布为正常岗位？"):
            return
        result = promote_internship_from_review(self.db)
        self._on_log(result.get("text") or "完成")
        self.refresh()
        self._on_changed()
        messagebox.showinfo("完成", result.get("text") or "完成")

    def clear_abnormal(self) -> None:
        n = len(self.db.list_review_queue("pending", limit=5000))
        if n <= 0:
            messagebox.showinfo("提示", "异常队列已为空")
            return
        if not messagebox.askyesno(
            "清空异常队列",
            f"删除 {n} 条待审异常（不影响已发布正常岗）。建议先「实习岗全部转正常」。继续？",
        ):
            return
        deleted = self.db.clear_review_queue("pending")
        self._on_log(f"已清空异常队列 {deleted} 条")
        self.refresh()
        self._on_changed()
        messagebox.showinfo("完成", f"已清空 {deleted} 条")
