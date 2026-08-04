"""Admin 账号管理面板。"""

from __future__ import annotations

import threading
from datetime import date, timedelta
from tkinter import messagebox, ttk
from typing import Any, Callable

import customtkinter as ctk

from app.auth.license import expiry_label, is_expired, normalize_expiry
from app.db.local import LocalDB
from app.sync.supabase import SupabaseSync


def _release_grab(win: Any) -> None:
    try:
        win.grab_release()
    except Exception:
        pass


class AccountEditDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master: Any,
        *,
        account: str = "",
        notes: str = "",
        enabled: bool = True,
        expires_at: str | None = None,
        is_edit: bool = False,
        on_save: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(master)
        self._on_save = on_save
        self._is_edit = is_edit
        self.title("修改账号" if is_edit else "新增账号")
        self.geometry("460x350")
        self.resizable(False, False)

        form = ctk.CTkFrame(self)
        form.pack(fill="both", expand=True, padx=16, pady=12)

        ctk.CTkLabel(form, text="账号").grid(row=0, column=0, sticky="w", pady=8)
        self.account_entry = ctk.CTkEntry(form, width=260)
        self.account_entry.grid(row=0, column=1, padx=(8, 0), pady=8)
        if account:
            self.account_entry.insert(0, account)
        if is_edit:
            self.account_entry.configure(state="disabled")

        ctk.CTkLabel(form, text="密码").grid(row=1, column=0, sticky="w", pady=8)
        self.password_entry = ctk.CTkEntry(form, width=260, show="*")
        self.password_entry.grid(row=1, column=1, padx=(8, 0), pady=8)
        hint = "留空则不修改" if is_edit else "必填"
        ctk.CTkLabel(form, text=hint, text_color="#666").grid(row=2, column=1, sticky="w")

        ctk.CTkLabel(form, text="备注").grid(row=3, column=0, sticky="w", pady=8)
        self.notes_entry = ctk.CTkEntry(form, width=260)
        self.notes_entry.grid(row=3, column=1, padx=(8, 0), pady=8)
        if notes:
            self.notes_entry.insert(0, notes)

        ctk.CTkLabel(form, text="到期日期").grid(row=4, column=0, sticky="w", pady=8)
        self.expires_entry = ctk.CTkEntry(
            form, width=260, placeholder_text="YYYY-MM-DD；留空=不设期限"
        )
        self.expires_entry.grid(row=4, column=1, padx=(8, 0), pady=8)
        if expires_at:
            self.expires_entry.insert(0, expires_at)

        self.enabled_var = ctk.BooleanVar(value=enabled)
        ctk.CTkCheckBox(form, text="启用账号", variable=self.enabled_var).grid(
            row=5, column=1, sticky="w", pady=8
        )

        btns = ctk.CTkFrame(self)
        btns.pack(fill="x", padx=16, pady=(0, 12))
        ctk.CTkButton(btns, text="取消", width=90, command=self._cancel).pack(side="right", padx=4)
        ctk.CTkButton(btns, text="保存", width=90, command=self._save).pack(side="right", padx=4)

        self.transient(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _cancel(self) -> None:
        _release_grab(self)
        self.destroy()

    def _save(self) -> None:
        account = self.account_entry.get().strip()
        password = self.password_entry.get()
        notes = self.notes_entry.get().strip()
        expires_raw = self.expires_entry.get().strip()
        enabled = bool(self.enabled_var.get())
        if not account:
            messagebox.showwarning("提示", "账号不能为空", parent=self)
            return
        if not self._is_edit and not password.strip():
            messagebox.showwarning("提示", "新建账号必须设置密码", parent=self)
            return
        try:
            expires_at = normalize_expiry(expires_raw)
        except ValueError as exc:
            messagebox.showwarning("日期格式错误", str(exc), parent=self)
            return
        if self._on_save:
            self._on_save(
                {
                    "account": account,
                    "password": password,
                    "notes": notes,
                    "enabled": enabled,
                    # 空字符串在编辑时表示清除期限；数据库层用 None 表示不设期限。
                    "expires_at": expires_at if expires_at is not None else "",
                }
            )
        _release_grab(self)
        self.destroy()


class AccountAdminPanel(ctk.CTkFrame):
    def __init__(
        self,
        master: Any,
        db: LocalDB,
        sync: SupabaseSync,
        *,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.db = db
        self.sync = sync
        self._on_log = on_log or (lambda _m: None)
        self._selected_account: str | None = None
        self._build()
        self.refresh_list()

    def _log(self, msg: str) -> None:
        self._on_log(msg)

    def _build(self) -> None:
        top = ctk.CTkFrame(self)
        top.pack(fill="x", padx=8, pady=8)
        ctk.CTkButton(top, text="新增", width=80, command=self.add_account).pack(side="left", padx=4)
        ctk.CTkButton(top, text="修改", width=80, command=self.edit_account).pack(side="left", padx=4)
        ctk.CTkButton(top, text="删除", width=80, fg_color="#a33", command=self.delete_account).pack(
            side="left", padx=4
        )
        ctk.CTkButton(top, text="推送到云端", width=100, command=self.push_accounts_async).pack(
            side="left", padx=12
        )

        table_frame = ctk.CTkFrame(self)
        table_frame.pack(fill="both", expand=True, padx=8, pady=4)
        cols = ("account", "notes", "enabled", "expires_at", "updated_at")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=18)
        headers = {
            "account": "账号",
            "notes": "备注",
            "enabled": "启用",
            "expires_at": "到期日期",
            "updated_at": "更新时间",
        }
        widths = {
            "account": 150,
            "notes": 240,
            "enabled": 60,
            "expires_at": 130,
            "updated_at": 160,
        }
        for c in cols:
            self.tree.heading(c, text=headers[c])
            self.tree.column(c, width=widths[c], anchor="w")
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        tip = (
            "账号为主键不可重复；密码可重复。密码仅以哈希同步到云端，不会明文存储。\n"
            "建议每个账号设置到期日期；到期后本地登录、云端登录和同步都会停止。\n"
            "账号仅授予个人求职使用：不得转售、批量导出、公开发布或反向抓取；变更后请「推送到云端」。"
        )
        ctk.CTkLabel(self, text=tip, justify="left", text_color="#666").pack(
            anchor="w", padx=12, pady=8
        )

    def _on_select(self, _event=None) -> None:
        sel = self.tree.selection()
        self._selected_account = sel[0] if sel else None

    def refresh_list(self) -> None:
        for i in self.tree.get_children():
            self.tree.delete(i)
        for row in self.db.list_app_users():
            self.tree.insert(
                "",
                "end",
                iid=row["account"],
                values=(
                    row["account"],
                    row.get("notes") or "",
                    "是" if int(row.get("enabled") or 0) else "否",
                    (
                        f"已过期 {expiry_label(row.get('expires_at'))}"
                        if is_expired(row.get("expires_at"))
                        else expiry_label(row.get("expires_at"))
                    ),
                    str(row.get("updated_at") or "").replace("T", " ")[:19],
                ),
            )
        self._selected_account = None

    def add_account(self) -> None:
        def on_save(data: dict[str, Any]) -> None:
            try:
                self.db.create_app_user(
                    data["account"],
                    data["password"],
                    notes=data.get("notes") or "",
                    enabled=bool(data.get("enabled", True)),
                    expires_at=data.get("expires_at"),
                )
            except ValueError as exc:
                messagebox.showerror("新增失败", str(exc))
                return
            self.refresh_list()
            self._log(f"已新增账号：{data['account']}")
            self.push_accounts_async()

        default_expiry = (date.today() + timedelta(days=90)).isoformat()
        AccountEditDialog(
            self.winfo_toplevel(),
            expires_at=default_expiry,
            is_edit=False,
            on_save=on_save,
        )

    def edit_account(self) -> None:
        acct = self._selected_account
        if not acct:
            messagebox.showinfo("提示", "请先选择要修改的账号")
            return
        row = self.db.get_app_user(acct)
        if not row:
            messagebox.showwarning("提示", "账号不存在，请刷新列表")
            self.refresh_list()
            return

        def on_save(data: dict[str, Any]) -> None:
            try:
                pwd = data.get("password") or ""
                self.db.update_app_user(
                    acct,
                    password=pwd if pwd.strip() else None,
                    notes=data.get("notes"),
                    enabled=bool(data.get("enabled", True)),
                    expires_at=data.get("expires_at"),
                )
            except ValueError as exc:
                messagebox.showerror("修改失败", str(exc))
                return
            self.refresh_list()
            self._log(f"已修改账号：{acct}")
            self.push_accounts_async()

        AccountEditDialog(
            self.winfo_toplevel(),
            account=acct,
            notes=row.get("notes") or "",
            enabled=bool(int(row.get("enabled") or 0)),
            expires_at=row.get("expires_at") or "",
            is_edit=True,
            on_save=on_save,
        )

    def delete_account(self) -> None:
        acct = self._selected_account
        if not acct:
            messagebox.showinfo("提示", "请先选择要删除的账号")
            return
        if not messagebox.askyesno("确认删除", f"确定删除账号「{acct}」？此操作会同步到云端。"):
            return
        if not self.db.delete_app_user(acct):
            messagebox.showwarning("提示", "删除失败，账号可能已不存在")
            self.refresh_list()
            return
        self.refresh_list()
        self._log(f"已删除账号：{acct}")
        self.push_accounts_async()

    def push_accounts_async(self) -> None:
        threading.Thread(target=self._push_worker, daemon=True).start()

    def _push_worker(self) -> None:
        try:
            self.sync.require_write_config()
            n = self.sync.push_app_users(self.db)
            self.after(0, lambda: self._log(f"账号已推送到云端（{n} 个）"))
        except Exception as exc:  # noqa: BLE001
            self.after(0, lambda m=str(exc): messagebox.showerror("推送失败", m))
