"""岗位人工编辑表单（正常岗 / 异常队列共用）。"""

from __future__ import annotations

import threading
from typing import Any, Callable

import customtkinter as ctk
from tkinter import messagebox, ttk

from app.collector.fill_from_url import (
    FILLABLE_KEYS,
    FillCandidate,
    FillFromUrlResult,
    discover_portal_jobs_from_url,
    enrich_candidate_detail,
    merge_fill_into_form,
    pick_fill_url,
)
from app.ui.tree_check import (
    CHECK_COL,
    bind_check_column_click,
    setup_check_column,
    sync_tree_checks,
    with_check,
)


# (field_key, label, multiline?)
_JOB_FIELDS: list[tuple[str, str, bool]] = [
    ("group_name", "集团（门户种子；空则非集团下属）", False),
    ("company", "公司（招聘单位）", False),
    ("title", "岗位名称 / 标题", False),
    ("salary_range", "薪资范围", False),
    ("headcount", "招聘人数", False),
    ("graduation_batch", "毕业批次 / 届别（如 2027届）", False),
    ("recruit_project", "招聘项目", False),
    ("recruit_bucket", "类型（校招 / 应届生实习 / 日常实习）", False),
    ("work_location", "工作地点 (base)", False),
    ("education", "学历要求", False),
    ("open_at", "岗位发布时间", False),
    ("deadline", "截止时间", False),
    ("source_url", "原文 / 公告链接", False),
    ("apply_url", "网申链接", False),
    ("jd_text", "JD / 岗位描述", True),
]


def job_fields_from_values(values: dict[str, Any] | None) -> dict[str, str]:
    src = values or {}
    return {key: str(src.get(key) or "") for key, _, _ in _JOB_FIELDS}


def merge_job_fields(base: dict[str, Any] | None, edited: dict[str, str]) -> dict[str, Any]:
    """把表单字段合并回岗位 / review payload。"""
    out = dict(base or {})
    for key, _, _ in _JOB_FIELDS:
        val = (edited.get(key) or "").strip()
        out[key] = val or None
    # 关键字段不允许为空字符串入库
    out["title"] = (edited.get("title") or "").strip()
    out["company"] = (edited.get("company") or "").strip() or "未命名企业"
    out["source_url"] = (edited.get("source_url") or "").strip()
    return out


def _release_grab(win: Any) -> None:
    """关闭模态前释放 grab，避免父窗口控件像被锁死无法编辑。"""
    try:
        win.grab_release()
    except Exception:
        pass


def selected_fill_candidates(
    candidates: list[FillCandidate],
    indices: list[int] | None,
) -> list[FillCandidate]:
    """仅返回勾选索引对应的候选；索引非法则跳过。绝不回退为「全部候选」。"""
    if not candidates or not indices:
        return []
    out: list[FillCandidate] = []
    n = len(candidates)
    seen: set[int] = set()
    for i in indices:
        if not isinstance(i, int) or i in seen or i < 0 or i >= n:
            continue
        seen.add(i)
        out.append(candidates[i])
    return out


class _JobPickDialog(ctk.CTkToplevel):
    """多岗位勾选对话框：Treeview 勾选，大批量不卡死主线程。"""

    def __init__(
        self,
        master: Any,
        candidates: list[FillCandidate],
        *,
        title: str = "选择要填充的岗位",
        preselect: list[int] | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(master)
        self.title(title)
        self.geometry("640x520")
        # 取消 / 未确认为 None；确认后为勾选索引列表（可为空时拦截）
        self.result_indices: list[int] | None = None
        # 兼容旧调用：取勾选中的第一项
        self.result_index: int | None = None
        self._candidates = candidates

        n = len(candidates)
        label = hint or (
            f"共识别到 {n} 个岗位（校招/实习），请勾选要写入的条目后确认。"
            "未勾选的不会导入。"
        )
        ctk.CTkLabel(self, text=label, anchor="w", justify="left", wraplength=600).pack(
            fill="x", padx=12, pady=(12, 6)
        )

        tools = ctk.CTkFrame(self)
        tools.pack(fill="x", padx=12, pady=(0, 4))
        ctk.CTkButton(tools, text="全选", width=72, command=self._select_all).pack(
            side="left", padx=2
        )
        ctk.CTkButton(tools, text="取消全选", width=88, command=self._select_none).pack(
            side="left", padx=2
        )
        self._count_lbl = ctk.CTkLabel(tools, text="", anchor="w", text_color="#555")
        self._count_lbl.pack(side="left", padx=10)

        frame = ctk.CTkFrame(self)
        frame.pack(fill="both", expand=True, padx=12, pady=4)
        cols = (CHECK_COL, "label")
        self._tree = ttk.Treeview(
            frame,
            columns=cols,
            show="headings",
            selectmode="extended",
            height=18,
        )
        setup_check_column(self._tree, width=36)
        self._tree.heading("label", text="岗位")
        self._tree.column("label", width=560, stretch=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=scroll.set)
        self._tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        bind_check_column_click(self._tree, on_after=self._refresh_count)
        self._tree.bind("<<TreeviewSelect>>", lambda _e: self._refresh_count())

        preset = set(preselect or [])
        # 大批量分批插入，避免一次构建数百控件卡死
        self._pending = list(enumerate(candidates))
        self._preset = preset
        self._loaded = 0
        self._batch_insert()

        btns = ctk.CTkFrame(self)
        btns.pack(fill="x", padx=12, pady=10)
        ctk.CTkButton(btns, text="取消", width=90, command=self._cancel).pack(
            side="right", padx=4
        )
        ctk.CTkButton(btns, text="确认所选", width=110, command=self._ok).pack(
            side="right", padx=4
        )

        self.transient(master)
        self.grab_set()
        self.focus()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _batch_insert(self, chunk: int = 80) -> None:
        batch = self._pending[:chunk]
        self._pending = self._pending[chunk:]
        to_select: list[str] = []
        for i, cand in batch:
            iid = str(i)
            self._tree.insert(
                "",
                "end",
                iid=iid,
                values=with_check((cand.label[:160],), selected=(i in self._preset)),
            )
            if i in self._preset:
                to_select.append(iid)
            self._loaded += 1
        if to_select:
            self._tree.selection_add(*to_select)
        sync_tree_checks(self._tree)
        total = len(self._candidates)
        self._count_lbl.configure(
            text=f"已加载 {self._loaded}/{total} · 已勾选 {len(self._tree.selection())}"
        )
        if self._pending:
            self.after(1, self._batch_insert)
        else:
            self._refresh_count()

    def _checked_indices(self) -> list[int]:
        out: list[int] = []
        for iid in self._tree.selection():
            try:
                out.append(int(iid))
            except (TypeError, ValueError):
                continue
        out.sort()
        return out

    def _refresh_count(self) -> None:
        n = len(self._checked_indices())
        total = len(self._candidates)
        loaded = self._loaded
        if loaded < total:
            self._count_lbl.configure(text=f"已加载 {loaded}/{total} · 已勾选 {n}")
        else:
            self._count_lbl.configure(text=f"已勾选 {n} / {total}")

    def _select_all(self) -> None:
        kids = self._tree.get_children("")
        if kids:
            self._tree.selection_set(kids)
        sync_tree_checks(self._tree)
        self._refresh_count()

    def _select_none(self) -> None:
        self._tree.selection_remove(*self._tree.get_children(""))
        sync_tree_checks(self._tree)
        self._refresh_count()

    def _cancel(self) -> None:
        self.result_indices = None
        self.result_index = None
        _release_grab(self)
        self.destroy()

    def _ok(self) -> None:
        if self._pending:
            messagebox.showwarning(
                "仍在加载",
                "岗位列表仍在加载中，请稍候再确认。",
                parent=self,
            )
            return
        picked = self._checked_indices()
        if not picked:
            messagebox.showwarning(
                "未勾选",
                "请至少勾选一个岗位再确认。未勾选的不会导入。",
                parent=self,
            )
            return
        self.result_indices = picked
        self.result_index = picked[0]
        _release_grab(self)
        self.destroy()


def pick_job_candidates(
    master: Any,
    candidates: list[FillCandidate],
    *,
    title: str = "选择要填充的岗位",
    preselect: list[int] | None = None,
    hint: str | None = None,
) -> list[FillCandidate] | None:
    """
    弹出勾选框；返回用户勾选的候选（仅勾选项）。
    - 仅 1 个候选：不弹窗，直接返回该条（不会带入「额外」岗位）
    - 取消：返回 None
    """
    if not candidates:
        return []
    if len(candidates) == 1:
        return [candidates[0]]
    root = master
    picker = _JobPickDialog(
        root, candidates, title=title, preselect=preselect, hint=hint
    )
    try:
        root.wait_window(picker)
    except Exception:
        pass
    if picker.result_indices is None:
        return None
    return selected_fill_candidates(candidates, picker.result_indices)


class _FillPreviewDialog(ctk.CTkToplevel):
    """填充前预览。"""

    def __init__(
        self,
        master: Any,
        summary: str,
        *,
        title: str = "预览识别结果",
        hint: str = "",
    ) -> None:
        super().__init__(master)
        self.title(title)
        self.geometry("560x520")
        self.confirmed = False

        if hint:
            ctk.CTkLabel(self, text=hint, anchor="w", text_color="#555").pack(
                fill="x", padx=12, pady=(10, 0)
            )
        box = ctk.CTkTextbox(self, height=380)
        box.pack(fill="both", expand=True, padx=12, pady=10)
        box.insert("1.0", summary)
        box.configure(state="disabled")

        btns = ctk.CTkFrame(self)
        btns.pack(fill="x", padx=12, pady=10)
        ctk.CTkButton(btns, text="取消", width=90, command=self._cancel).pack(side="right", padx=4)
        ctk.CTkButton(btns, text="确认填充", width=110, command=self._ok).pack(side="right", padx=4)

        self.transient(master)
        self.grab_set()
        self.focus()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _cancel(self) -> None:
        self.confirmed = False
        _release_grab(self)
        self.destroy()

    def _ok(self) -> None:
        self.confirmed = True
        _release_grab(self)
        self.destroy()


class JobEditForm(ctk.CTkScrollableFrame):
    """可嵌入侧栏或对话框的编辑表单（字段区可纵向滚动）。"""

    def __init__(self, master: Any, **kwargs: Any) -> None:
        super().__init__(master, **kwargs)
        self._entries: dict[str, ctk.CTkEntry | ctk.CTkTextbox] = {}
        self._meta = ctk.CTkLabel(self, text="", anchor="w", justify="left", text_color="#666")
        self._meta.pack(fill="x", padx=4, pady=(0, 6))
        for key, label, multi in _JOB_FIELDS:
            ctk.CTkLabel(self, text=label, anchor="w").pack(fill="x", padx=4, pady=(4, 0))
            if multi:
                # 固定高度：在 ScrollableFrame 内勿 expand，否则无法计算可滚内容高度
                w = ctk.CTkTextbox(self, height=160)
                w.pack(fill="x", padx=4, pady=2)
            else:
                w = ctk.CTkEntry(self)
                w.pack(fill="x", padx=4, pady=2)
            self._entries[key] = w

        link_row = ctk.CTkFrame(self)
        link_row.pack(fill="x", padx=4, pady=(8, 2))
        self._fill_btn = ctk.CTkButton(
            link_row,
            text="从链接识别并填充",
            width=140,
            command=self.fill_from_link,
        )
        self._fill_btn.pack(side="left", padx=2)
        self._fill_status = ctk.CTkLabel(
            link_row, text="", anchor="w", text_color="#555"
        )
        self._fill_status.pack(side="left", padx=8, fill="x", expand=True)

        self._fill_busy = False
        self._suppress_url_prompt = False
        self._url_snapshot: dict[str, str] = {"apply_url": "", "source_url": ""}
        for key in ("apply_url", "source_url"):
            widget = self._entries[key]
            if isinstance(widget, ctk.CTkEntry):
                widget.bind("<FocusOut>", self._on_url_focus_out)

    def set_meta(self, text: str) -> None:
        self._meta.configure(text=text or "")

    def _ensure_entries_editable(self) -> None:
        """填充/加载后强制恢复可编辑，防止 CTk 控件残留 disabled。"""
        for widget in self._entries.values():
            try:
                widget.configure(state="normal")
            except Exception:
                pass

    def _write_entry(self, key: str, val: str) -> None:
        widget = self._entries.get(key)
        if widget is None:
            return
        try:
            widget.configure(state="normal")
        except Exception:
            pass
        if isinstance(widget, ctk.CTkTextbox):
            widget.delete("1.0", "end")
            widget.insert("1.0", val)
        else:
            widget.delete(0, "end")
            widget.insert(0, val)

    def _clear_suppress_prompt(self) -> None:
        self._suppress_url_prompt = False

    def clear(self) -> None:
        self.load({})
        self.set_meta("")
        self._set_fill_status("")

    def load(self, data: dict[str, Any] | None) -> None:
        values = job_fields_from_values(data)
        self._suppress_url_prompt = True
        try:
            self._ensure_entries_editable()
            for key in self._entries:
                self._write_entry(key, values.get(key, ""))
            self._url_snapshot = {
                "apply_url": values.get("apply_url", ""),
                "source_url": values.get("source_url", ""),
            }
        finally:
            self._ensure_entries_editable()
            self.after(50, self._clear_suppress_prompt)

    def values(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, widget in self._entries.items():
            if isinstance(widget, ctk.CTkTextbox):
                out[key] = widget.get("1.0", "end").strip()
            else:
                out[key] = widget.get().strip()
        return out

    def apply_fill_fields(self, filled: dict[str, str]) -> None:
        """把识别字段写入表单（含公司），写入后保持可编辑。"""
        merged = merge_fill_into_form(self.values(), filled)
        self._suppress_url_prompt = True
        try:
            for key in (*FILLABLE_KEYS, "company"):
                if key not in self._entries:
                    continue
                self._write_entry(key, merged.get(key, ""))
            self._url_snapshot = {
                "apply_url": merged.get("apply_url", ""),
                "source_url": merged.get("source_url", ""),
            }
        finally:
            self._ensure_entries_editable()
            self.after(50, self._clear_suppress_prompt)

    def _set_fill_busy(self, busy: bool) -> None:
        self._fill_busy = busy
        try:
            self._fill_btn.configure(state="disabled" if busy else "normal")
        except Exception:
            pass

    def _set_fill_status(self, text: str) -> None:
        try:
            self._fill_status.configure(text=text or "")
        except Exception:
            pass

    def _on_url_focus_out(self, _event: Any = None) -> None:
        if self._fill_busy or self._suppress_url_prompt:
            return
        cur = self.values()
        changed = False
        for key in ("apply_url", "source_url"):
            now = (cur.get(key) or "").strip()
            prev = (self._url_snapshot.get(key) or "").strip()
            if now and now != prev and (now.startswith("http://") or now.startswith("https://")):
                changed = True
                break
        if not changed:
            return
        # 更新快照，避免反复弹窗；询问是否识别
        self._url_snapshot = {
            "apply_url": cur.get("apply_url", ""),
            "source_url": cur.get("source_url", ""),
        }
        if messagebox.askyesno(
            "链接已变更",
            "网申/原文链接已修改，是否从链接识别并填充岗位信息？",
            parent=self.winfo_toplevel(),
        ):
            self.fill_from_link()

    def fill_from_link(self) -> None:
        """按钮 / 失焦确认：解析链接 → 选岗 → 预览 → 回填。"""
        if self._fill_busy:
            return
        vals = self.values()
        url = pick_fill_url(vals.get("apply_url"), vals.get("source_url"))
        if not url:
            messagebox.showwarning(
                "无法识别",
                "请先填写网申链接或原文链接",
                parent=self.winfo_toplevel(),
            )
            return
        company = vals.get("company") or ""
        self._set_fill_busy(True)
        self._set_fill_status("正在打开链接并识别…")
        root = self.winfo_toplevel()

        def worker() -> None:
            try:
                from app.config import load_config

                cfg = load_config()
                months = cfg.get("list_collect_months")

                def on_progress(msg: str) -> None:
                    self.after(0, lambda m=msg: self._set_fill_status(m))

                # 与岗位审核「重新识别」一致：局部重采入口
                from app.collector.fill_from_url import (
                    per_company_batch_size,
                    scan_limit_for_batch,
                )

                batch_n = per_company_batch_size(cfg)
                result = discover_portal_jobs_from_url(
                    url,
                    fetch=True,
                    keep_company=company or None,
                    list_collect_months=int(months) if months else None,
                    list_limit=scan_limit_for_batch(batch_n),
                    timeout=90.0,
                    progress=on_progress,
                )
            except Exception as exc:  # noqa: BLE001
                result = FillFromUrlResult(ok=False, error=f"识别失败：{exc}", page_url=url)
            self.after(0, lambda: self._on_resolve_done(result, keep_company=company, root=root))

        threading.Thread(target=worker, daemon=True).start()

    def _on_resolve_done(
        self,
        result: FillFromUrlResult,
        *,
        keep_company: str,
        root: Any,
    ) -> None:
        if not result.ok or not result.candidates:
            self._set_fill_busy(False)
            self._set_fill_status("")
            self._ensure_entries_editable()
            messagebox.showerror(
                "识别失败",
                result.error or "未能识别岗位信息",
                parent=root,
            )
            return

        from app.collector.fill_from_url import (
            per_company_batch_size,
            take_collect_batch,
        )
        from app.config import load_config

        batch_n = per_company_batch_size(load_config())
        # 编辑器无整库公司上下文时，仅按本批上限截断，避免一次勾数百
        candidates, batch_stats = take_collect_batch(
            list(result.candidates),
            known_urls=set(),
            known_titles=set(),
            batch_size=batch_n,
        )
        if not candidates:
            self._set_fill_busy(False)
            self._set_fill_status("")
            self._ensure_entries_editable()
            messagebox.showinfo("从链接识别", "本批无岗位可勾选。", parent=root)
            return
        outside_n = sum(1 for c in candidates if getattr(c, "outside_lookback", False))
        remain = int(batch_stats.get("remaining_after_batch") or 0)
        notice = (
            f"本批最多 {batch_n} 个（已列出 {len(candidates)}）；"
            f"扫描后约剩 {remain} 个可下次再识别。"
        )
        in_win = [
            i for i, c in enumerate(candidates) if not getattr(c, "outside_lookback", False)
        ]
        if len(candidates) == 1:
            preselect = [0]
        elif len(in_win) == 1:
            preselect = in_win
        else:
            preselect = None
        hint = (
            notice
            + (f" 其中 {outside_n} 个超出近半年。" if outside_n else "")
            + "\n勾选后将填充到表单；未勾选不会写入。"
        )
        self._set_fill_status(notice)
        chosen_list = pick_job_candidates(
            root,
            candidates,
            title="从链接识别 · 勾选岗位",
            preselect=preselect,
            hint=hint,
        )
        if chosen_list is None:
            self._set_fill_busy(False)
            self._set_fill_status("已取消")
            self._ensure_entries_editable()
            return
        if not chosen_list:
            self._set_fill_busy(False)
            self._set_fill_status("未勾选")
            self._ensure_entries_editable()
            return

        page_url = result.page_url or ""
        self._set_fill_status(f"正在抓取详情…（{len(chosen_list)}）")

        def enrich_worker() -> None:
            enriched: list[FillCandidate] = []
            for i, cand in enumerate(chosen_list):
                try:
                    enriched.append(
                        enrich_candidate_detail(
                            cand,
                            page_url=page_url,
                            keep_company=keep_company or None,
                            timeout=90.0,
                        )
                    )
                except Exception:
                    enriched.append(cand)
                if (i + 1) % 5 == 0 or i + 1 == len(chosen_list):
                    msg = f"详情进度 {i + 1}/{len(chosen_list)}"
                    self.after(0, lambda m=msg: self._set_fill_status(m))
            # 表单仅填第一条；多选提示走 hint
            first = enriched[0] if enriched else chosen_list[0]
            extra_hint = hint
            if len(chosen_list) > 1:
                extra_hint = (
                    hint
                    + f"\n（已勾选 {len(chosen_list)} 条，表单仅填充第一条；其余请用岗位审核导入）"
                )
            self.after(
                0,
                lambda: self._on_enrich_done(first, hint=extra_hint, root=root),
            )

        threading.Thread(target=enrich_worker, daemon=True).start()

    def _on_enrich_done(self, candidate: FillCandidate, *, hint: str, root: Any) -> None:
        self._set_fill_status("请确认预览…")
        preview = _FillPreviewDialog(
            root,
            candidate.summary,
            hint=hint or "确认后将写入表单，仍可再改并点「保存修改」。",
        )
        root.wait_window(preview)
        if not preview.confirmed:
            self._set_fill_busy(False)
            self._set_fill_status("已取消填充")
            self._ensure_entries_editable()
            return
        self.apply_fill_fields(candidate.fields)
        self._set_fill_busy(False)
        self._set_fill_status("已填充，请核对后保存")
        self._ensure_entries_editable()
        # 把焦点拉回表单，避免模态关闭后树控件抢焦点并重载侧栏
        try:
            title_w = self._entries.get("title")
            if title_w is not None:
                title_w.focus_set()
        except Exception:
            pass
        if hint and "共识别" in hint:
            messagebox.showinfo("已填充", hint, parent=root)
            self._ensure_entries_editable()
            try:
                title_w = self._entries.get("title")
                if title_w is not None:
                    title_w.focus_set()
            except Exception:
                pass


class JobEditDialog(ctk.CTkToplevel):
    """独立编辑对话框。"""

    def __init__(
        self,
        master: Any,
        data: dict[str, Any] | None = None,
        *,
        title: str = "编辑岗位",
        on_save: Callable[[dict[str, str]], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.title(title)
        self.geometry("560x640")
        self._on_save = on_save
        self.form = JobEditForm(self)
        self.form.pack(fill="both", expand=True, padx=10, pady=10)
        if data:
            self.form.load(data)
        btns = ctk.CTkFrame(self)
        btns.pack(fill="x", padx=10, pady=8)
        ctk.CTkButton(btns, text="取消", width=90, command=self._cancel).pack(side="right", padx=4)
        ctk.CTkButton(btns, text="保存", width=90, command=self._save).pack(side="right", padx=4)
        self.transient(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _cancel(self) -> None:
        _release_grab(self)
        self.destroy()

    def _save(self) -> None:
        vals = self.form.values()
        if self._on_save:
            self._on_save(vals)
        _release_grab(self)
        self.destroy()
