"""Viewer 登录对话框。"""

from __future__ import annotations

from dataclasses import dataclass
from tkinter import messagebox
from typing import Any

import customtkinter as ctk

from app.auth.license import is_expired
from app.config import load_config, save_config
from app.db.local import LocalDB
from app.sync.supabase import ViewerSessionError


@dataclass(frozen=True)
class ViewerLoginResult:
    account: str
    session_token: str | None = None
    cloud_authenticated: bool = False


def _release_grab(win: Any) -> None:
    try:
        win.grab_release()
    except Exception:
        pass


def _has_login_accounts(db: LocalDB, sync: Any | None) -> bool:
    if db.count_app_users() > 0:
        return True
    if sync is not None and getattr(sync, "enabled", False):
        try:
            return bool(sync.viewer_has_accounts())
        except Exception:
            pass
    return False


def verify_viewer_credentials(db: LocalDB, sync: Any | None, account: str, password: str) -> bool:
    """兼容旧调用方的布尔登录接口。"""
    return authenticate_viewer(db, sync, account, password) is not None


def authenticate_viewer(
    db: LocalDB,
    sync: Any | None,
    account: str,
    password: str,
) -> ViewerLoginResult | None:
    """登录并优先建立云端会话；网络不可用时仅允许离线本地数据。"""
    acct = (account or "").strip()
    if not acct or not password:
        return None
    if sync is not None and getattr(sync, "enabled", False):
        try:
            create_session = getattr(sync, "create_viewer_session", None)
            if create_session is not None:
                token = create_session(acct, password)
                if token:
                    return ViewerLoginResult(acct, str(token), True)
                return None
            if sync.verify_viewer_login(acct, password):
                return ViewerLoginResult(acct, None, False)
        except ViewerSessionError:
            # 云端未部署新迁移或暂时不可达时，允许已缓存账号打开本地数据；
            # 没有会话令牌的 Viewer 不会读取云端岗位。
            pass
        except Exception:
            pass
    if db.verify_app_user_login(acct, password):
        return ViewerLoginResult(acct)
    return None


class LoginDialog(ctk.CTkToplevel):
    """阻塞式登录；成功时设置 self.logged_in_account。"""

    def __init__(self, master: Any, db: LocalDB, sync: Any | None = None) -> None:
        super().__init__(master)
        self.db = db
        self.sync = sync
        self.cfg = load_config()
        self.logged_in_account: str | None = None
        self.session_token: str | None = None

        self.title("登录")
        self.geometry("440x290")
        self.resizable(False, False)

        ctk.CTkLabel(self, text="校招投递表 Viewer", font=ctk.CTkFont(size=16, weight="bold")).pack(
            pady=(16, 8)
        )
        ctk.CTkLabel(
            self,
            text="个人求职许可：仅限本人使用，不得转售、批量导出或公开发布。",
            text_color="#666",
            wraplength=390,
        ).pack(pady=(0, 8))
        form = ctk.CTkFrame(self)
        form.pack(fill="x", padx=24, pady=4)

        ctk.CTkLabel(form, text="账号").grid(row=0, column=0, sticky="w", pady=6)
        self.account_entry = ctk.CTkEntry(form, width=240)
        self.account_entry.grid(row=0, column=1, padx=(8, 0), pady=6)
        last = str(self.cfg.get("_last_login_account") or "").strip()
        if last:
            self.account_entry.insert(0, last)

        ctk.CTkLabel(form, text="密码").grid(row=1, column=0, sticky="w", pady=6)
        self.password_entry = ctk.CTkEntry(form, width=240, show="*")
        self.password_entry.grid(row=1, column=1, padx=(8, 0), pady=6)

        btns = ctk.CTkFrame(self)
        btns.pack(fill="x", padx=24, pady=12)
        ctk.CTkButton(btns, text="退出", width=90, command=self._cancel).pack(side="right", padx=4)
        ctk.CTkButton(btns, text="登录", width=90, command=self._login).pack(side="right", padx=4)

        self.transient(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.account_entry.bind("<Return>", lambda _e: self.password_entry.focus_set())
        self.password_entry.bind("<Return>", lambda _e: self._login())
        self.after(100, self.account_entry.focus_set)
        # Fix for CTkToplevel visibility issue with withdrawn master
        self.after(200, self._fix_visibility)

    def _fix_visibility(self) -> None:
        """Force dialog to be visible."""
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
            self.update()
        except Exception:
            pass

    def _cancel(self) -> None:
        self.logged_in_account = None
        _release_grab(self)
        self.destroy()

    def _login(self) -> None:
        account = self.account_entry.get().strip()
        password = self.password_entry.get()
        if not account:
            messagebox.showwarning("提示", "请输入账号", parent=self)
            return
        if not password:
            messagebox.showwarning("提示", "请输入密码", parent=self)
            return
        result = authenticate_viewer(self.db, self.sync, account, password)
        if result is None:
            local = self.db.get_app_user(account)
            if local and is_expired(local.get("expires_at")):
                messagebox.showerror("账号已到期", "该账号的使用期限已结束，请联系管理员续期。", parent=self)
            else:
                messagebox.showerror("登录失败", "账号或密码错误，或账号已停用", parent=self)
            self.password_entry.delete(0, "end")
            self.password_entry.focus_set()
            return
        self.cfg["_last_login_account"] = account
        save_config(self.cfg)
        self.logged_in_account = result.account
        self.session_token = result.session_token
        _release_grab(self)
        self.destroy()


def require_viewer_login(
    db: LocalDB,
    sync: Any | None = None,
) -> ViewerLoginResult | None:
    """启动前检查账号并登录；无账号时返回 None 并提示。"""
    if not _has_login_accounts(db, sync):
        root = ctk.CTk()
        root.withdraw()
        messagebox.showerror(
            "无法使用 Viewer",
            "尚未配置登录账号。\n请管理员在 Admin 端「账号管理」创建账号并推送到云端后，再同步登录。",
            parent=root,
        )
        root.destroy()
        return None

    root = ctk.CTk()
    root.withdraw()
    dlg = LoginDialog(root, db, sync=sync)
    root.wait_window(dlg)
    account = dlg.logged_in_account
    token = dlg.session_token
    root.destroy()
    return ViewerLoginResult(account, token, bool(token)) if account else None
