"""Viewer 桌面端 AI 陪伴面板（PRD：证据→简历→匹配→面试，无人工 coach）。"""

from __future__ import annotations

import threading
from typing import Any, Callable

import customtkinter as ctk
from tkinter import messagebox

from app.coach.ai.llm_client import llm_status
from app.coach.database import CoachDB
from app.coach.workflows import get_registry
from app.envutil import load_dotenv
from app.timeutil import utc_now_iso


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None or value == "":
        return []
    return [value]


def job_payload_from_campus(job: dict[str, Any] | None) -> dict[str, Any]:
    """把校招表岗位行转成 match.explain 所需结构。"""
    if not job:
        return {}
    return {
        "id": job.get("id"),
        "title": job.get("title") or "",
        "company": job.get("company") or "",
        "city": job.get("work_location") or "",
        "education": job.get("education") or "",
        "description": job.get("jd_text") or "",
        "apply_url": job.get("apply_url") or "",
    }


class CoachCompanionFrame(ctk.CTkFrame):
    """嵌入 Viewer 的 AI 陪伴主界面。"""

    def __init__(
        self,
        master: Any,
        *,
        account: str,
        get_selected_job: Callable[[], dict[str, Any] | None] | None = None,
        coach_db: CoachDB | None = None,
    ) -> None:
        super().__init__(master, fg_color="transparent")
        load_dotenv()
        self.account = (account or "viewer").strip() or "viewer"
        self.get_selected_job = get_selected_job or (lambda: None)
        self.coach_db = coach_db or CoachDB()
        self.user = self.coach_db.ensure_user(self.account)
        self.user_id = self.user["id"]
        self.version_id: str | None = None
        self.session_id: str | None = None
        self._busy = False

        self._build()
        self.after(100, self.refresh_today)

    def _build(self) -> None:
        head = ctk.CTkFrame(self, fg_color="#ffffff", corner_radius=10, border_width=1, border_color="#dbe3ec")
        head.pack(fill="x", padx=4, pady=(0, 8))
        ctk.CTkLabel(
            head,
            text="AI 求职陪伴",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color="#162235",
        ).pack(side="left", padx=16, pady=12)
        ctk.CTkLabel(
            head,
            text="仅 AI · 基于已确认事实 · 无人工教练",
            font=ctk.CTkFont(size=12),
            text_color="#64748b",
        ).pack(side="left", pady=12)
        self.llm_label = ctk.CTkLabel(head, text="", text_color="#64748b", anchor="e")
        self.llm_label.pack(side="right", padx=16, pady=12)
        self._refresh_llm_badge()

        self.tabs = ctk.CTkTabview(self, fg_color="#ffffff", border_width=1, border_color="#dbe3ec")
        self.tabs.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        for name in ("今日", "简历与证据", "岗位匹配", "面试陪伴"):
            self.tabs.add(name)

        self._build_today(self.tabs.tab("今日"))
        self._build_resume(self.tabs.tab("简历与证据"))
        self._build_match(self.tabs.tab("岗位匹配"))
        self._build_interview(self.tabs.tab("面试陪伴"))

        self.status_var = ctk.StringVar(value="就绪")
        foot = ctk.CTkFrame(self, fg_color="#e8eef5", corner_radius=8)
        foot.pack(fill="x", padx=4, pady=(4, 0))
        ctk.CTkLabel(foot, textvariable=self.status_var, anchor="w", text_color="#475569").pack(
            fill="x", padx=12, pady=7
        )

    def _refresh_llm_badge(self) -> None:
        try:
            st = llm_status()
            if st.get("ready") and st.get("dual", {}).get("enabled"):
                text = f"LLM 双厂商就绪 · {st['primary']['provider']}+{(st.get('fallback') or {}).get('provider')}"
            elif st.get("ready"):
                text = f"LLM 就绪 · {st['primary']['provider']}:{st['primary']['model']}"
            else:
                text = "规则引擎模式（未配置 LLM 或 FORCE_RULES=1）"
            self.llm_label.configure(text=text)
        except Exception:
            self.llm_label.configure(text="LLM 状态未知")

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _run_async(self, title: str, fn: Callable[[], Any], on_ok: Callable[[Any], None]) -> None:
        if self._busy:
            messagebox.showinfo("请稍候", "正在处理上一任务，请稍后再试。")
            return
        self._busy = True
        self._set_status(f"{title}…")

        def worker() -> None:
            try:
                result = fn()
                self.after(0, lambda: self._finish_ok(on_ok, result, title))
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: self._finish_err(title, exc))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_ok(self, on_ok: Callable[[Any], None], result: Any, title: str) -> None:
        self._busy = False
        try:
            on_ok(result)
            self._set_status(f"{title}完成")
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"{title}展示失败: {exc}")
            messagebox.showerror(title, str(exc))

    def _finish_err(self, title: str, exc: Exception) -> None:
        self._busy = False
        self._set_status(f"{title}失败: {exc}")
        messagebox.showerror(title, str(exc))

    def _ctx(self) -> dict[str, Any]:
        return {"db": self.coach_db, "user": self.user}

    def _run_wf(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        body.setdefault("user_id", self.user_id)
        return get_registry().run(name, body, context=self._ctx())

    # ----- 今日 -----
    def _build_today(self, parent: Any) -> None:
        ctk.CTkLabel(
            parent,
            text="今日最重要",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="#162235",
        ).pack(anchor="w", padx=12, pady=(12, 4))
        self.today_primary = ctk.CTkLabel(
            parent, text="加载中…", wraplength=900, justify="left", text_color="#334155"
        )
        self.today_primary.pack(anchor="w", padx=12, pady=4)
        self.today_steps = ctk.CTkLabel(parent, text="", justify="left", text_color="#64748b")
        self.today_steps.pack(anchor="w", padx=12, pady=4)
        ctk.CTkButton(parent, text="刷新今日", width=100, command=self.refresh_today).pack(
            anchor="w", padx=12, pady=8
        )
        self.today_box = ctk.CTkTextbox(parent, height=280, fg_color="#f8fafc", border_width=1, border_color="#e2e8f0")
        self.today_box.pack(fill="both", expand=True, padx=12, pady=(4, 12))

    def refresh_today(self) -> None:
        facts = self.coach_db.fetchall(
            "SELECT * FROM facts WHERE user_id=? ORDER BY created_at",
            (self.user_id,),
        )
        pending = [f for f in facts if f.get("status") == "pending"]
        confirmed = [f for f in facts if f.get("status") == "confirmed"]
        tasks = self.coach_db.fetchall(
            "SELECT * FROM action_tasks WHERE user_id=? AND status='open' ORDER BY created_at LIMIT 5",
            (self.user_id,),
        )
        if not self.version_id and not confirmed:
            primary = "上传简历并确认事实"
            reason = "AI 陪伴需基于已确认证据，才能给简历/面试建议"
        elif pending:
            primary = f"确认 {len(pending)} 条待确认事实"
            reason = "未确认事实不会进入简历改写与匹配解释"
        elif confirmed and not tasks:
            primary = "选择岗位做匹配解释，或开始模拟面试"
            reason = f"已确认 {len(confirmed)} 条事实"
        else:
            primary = (tasks[0].get("title") if tasks else "继续完善证据与投递") or "继续完善"
            reason = (tasks[0].get("reason") if tasks else "") or ""

        steps = [
            ("上传简历", bool(self.version_id) or bool(facts)),
            ("确认事实", bool(confirmed) and not pending),
            ("岗位匹配", False),
            ("模拟面试", bool(self.session_id)),
        ]
        step_text = " → ".join(("✓ " if ok else "") + name for name, ok in steps)
        self.today_primary.configure(text=f"{primary}\n{reason}")
        self.today_steps.configure(text=step_text)
        self.today_box.configure(state="normal")
        self.today_box.delete("1.0", "end")
        lines = [f"账号：{self.account}", f"已确认事实：{len(confirmed)}", f"待确认：{len(pending)}", ""]
        if confirmed:
            lines.append("【已确认】")
            lines.extend(f"- {f.get('text')}" for f in confirmed[:8])
            lines.append("")
        if pending:
            lines.append("【待确认】")
            lines.extend(f"- {f.get('text')}" for f in pending[:8])
        if tasks:
            lines.append("")
            lines.append("【待办】")
            lines.extend(f"- {t.get('title')}: {t.get('reason') or ''}" for t in tasks)
        self.today_box.insert("1.0", "\n".join(lines))
        self.today_box.configure(state="disabled")
        self._refresh_llm_badge()

    # ----- 简历 -----
    def _build_resume(self, parent: Any) -> None:
        ctk.CTkLabel(
            parent,
            text="粘贴简历文本 → 提取证据 → 确认事实 → 生成建议",
            text_color="#64748b",
        ).pack(anchor="w", padx=12, pady=(12, 4))
        self.resume_text = ctk.CTkTextbox(parent, height=160, fg_color="#f8fafc", border_width=1, border_color="#e2e8f0")
        self.resume_text.pack(fill="x", padx=12, pady=4)

        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=6)
        ctk.CTkButton(row, text="1. 提取证据", width=110, command=self.extract_evidence).pack(side="left", padx=3)
        ctk.CTkButton(row, text="2. 确认全部待确认", width=140, command=self.confirm_pending_facts).pack(
            side="left", padx=3
        )
        ctk.CTkButton(row, text="挖掘优势", width=100, fg_color="#0f766e", command=self.mine_strengths).pack(
            side="left", padx=3
        )

        row2 = ctk.CTkFrame(parent, fg_color="transparent")
        row2.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(row2, text="建议场景").pack(side="left")
        self.scenario_var = ctk.StringVar(value="bullets")
        ctk.CTkOptionMenu(
            row2,
            values=["general", "bullets", "quantify", "keywords"],
            variable=self.scenario_var,
            width=120,
        ).pack(side="left", padx=8)
        ctk.CTkButton(row2, text="3. 生成简历建议", width=130, command=self.suggest_resume).pack(side="left", padx=3)

        self.facts_box = ctk.CTkTextbox(parent, height=120, fg_color="#f8fafc", border_width=1, border_color="#e2e8f0")
        self.facts_box.pack(fill="x", padx=12, pady=6)
        self.resume_out = ctk.CTkTextbox(parent, height=220, fg_color="#f8fafc", border_width=1, border_color="#e2e8f0")
        self.resume_out.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._reload_facts_box()

    def _reload_facts_box(self) -> None:
        rows = self.coach_db.fetchall(
            "SELECT * FROM facts WHERE user_id=? ORDER BY created_at",
            (self.user_id,),
        )
        self.facts_box.configure(state="normal")
        self.facts_box.delete("1.0", "end")
        if not rows:
            self.facts_box.insert("1.0", "暂无事实。请先粘贴简历并提取证据。")
        else:
            lines = []
            for f in rows:
                mark = "✓" if f.get("status") == "confirmed" else "○"
                lines.append(f"{mark} [{f.get('status')}] {f.get('text')}")
            self.facts_box.insert("1.0", "\n".join(lines))
        self.facts_box.configure(state="disabled")

    def extract_evidence(self) -> None:
        text = self.resume_text.get("1.0", "end").strip()
        if len(text) < 10:
            messagebox.showinfo("提示", "请先粘贴至少一段简历文本")
            return

        def work() -> dict[str, Any]:
            now = utc_now_iso()
            doc_id = self.coach_db.new_id("doc_")
            ver_id = self.coach_db.new_id("rv_")
            self.coach_db.execute(
                "INSERT INTO resume_documents(id, user_id, filename, parse_status, text_content, created_at) VALUES(?,?,?,?,?,?)",
                (doc_id, self.user_id, "viewer_paste.txt", "parsed", text, now),
            )
            self.coach_db.execute(
                "INSERT INTO resume_versions(id, document_id, user_id, parent_id, label, sections_json, created_at) VALUES(?,?,?,?,?,?,?)",
                (ver_id, doc_id, self.user_id, None, "原始版", self.coach_db.dumps({"全文": text}), now),
            )
            extracted = self._run_wf("evidence.extract", {"text": text, "source": "resume"})
            return {"version_id": ver_id, "extract": extracted}

        def done(result: dict[str, Any]) -> None:
            self.version_id = result["version_id"]
            self._reload_facts_box()
            self.refresh_today()
            assets = (result.get("extract") or {}).get("assets") or []
            self.resume_out.configure(state="normal")
            self.resume_out.delete("1.0", "end")
            self.resume_out.insert(
                "1.0",
                f"已提取 {len(assets)} 条经历资产（待确认）。\n请点击「确认全部待确认」后再生成建议。\n\n"
                + "\n".join(f"- {a.get('text')}" for a in assets[:12]),
            )
            self.resume_out.configure(state="disabled")

        self._run_async("提取证据", work, done)

    def confirm_pending_facts(self) -> None:
        pending = self.coach_db.fetchall(
            "SELECT id FROM facts WHERE user_id=? AND status='pending'",
            (self.user_id,),
        )
        if not pending:
            messagebox.showinfo("提示", "没有待确认事实")
            return
        for row in pending:
            self.coach_db.execute(
                "UPDATE facts SET status='confirmed' WHERE id=? AND user_id=?",
                (row["id"], self.user_id),
            )
        self._reload_facts_box()
        self.refresh_today()
        messagebox.showinfo("已确认", f"已确认 {len(pending)} 条事实")

    def mine_strengths(self) -> None:
        def work() -> dict[str, Any]:
            return self._run_wf("strengths.mine", {})

        def done(result: dict[str, Any]) -> None:
            strengths = result.get("strengths") or []
            self.resume_out.configure(state="normal")
            self.resume_out.delete("1.0", "end")
            lines = ["【优势链】"]
            for s in strengths[:10]:
                if isinstance(s, dict):
                    lines.append(f"- {s.get('capability') or s.get('name') or ''}: {s.get('evidence') or s}")
                else:
                    lines.append(f"- {s}")
            self.resume_out.insert("1.0", "\n".join(lines) or "暂无优势")
            self.resume_out.configure(state="disabled")

        self._run_async("挖掘优势", work, done)

    def suggest_resume(self) -> None:
        if not self.version_id:
            messagebox.showinfo("提示", "请先提取证据，生成简历版本")
            return
        scenario = self.scenario_var.get()
        name = "resume.suggest" if scenario == "general" else f"resume.suggest.{scenario}"
        job = job_payload_from_campus(self.get_selected_job())

        def work() -> dict[str, Any]:
            return self._run_wf(name, {"version_id": self.version_id, "job": job or {"title": "目标岗位"}})

        def done(result: dict[str, Any]) -> None:
            suggestions = result.get("suggestions") or []
            engine = result.get("engine") or "rules"
            lines = [f"引擎：{engine} · 场景：{scenario}", ""]
            for s in suggestions:
                flag = "需补证据" if s.get("needs_proof") else "可用"
                lines.append(f"[{flag}] {s.get('section') or ''}")
                lines.append(f"  建议：{s.get('suggestion')}")
                if s.get("reason"):
                    lines.append(f"  原因：{s.get('reason')}")
                lines.append("")
            self.resume_out.configure(state="normal")
            self.resume_out.delete("1.0", "end")
            self.resume_out.insert("1.0", "\n".join(lines) or "无建议")
            self.resume_out.configure(state="disabled")

        self._run_async("生成简历建议", work, done)

    # ----- 匹配 -----
    def _build_match(self, parent: Any) -> None:
        ctk.CTkLabel(
            parent,
            text="使用左侧「岗位投递」页选中的岗位，或手动填写 JD",
            text_color="#64748b",
        ).pack(anchor="w", padx=12, pady=(12, 4))
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=4)
        ctk.CTkButton(row, text="从当前选中岗位填入", width=160, command=self.fill_job_from_selection).pack(
            side="left", padx=3
        )
        ctk.CTkButton(row, text="生成匹配解释", width=120, command=self.run_match).pack(side="left", padx=3)

        form = ctk.CTkFrame(parent, fg_color="transparent")
        form.pack(fill="x", padx=12, pady=4)
        self.match_title = ctk.CTkEntry(form, placeholder_text="岗位名称", width=280)
        self.match_title.pack(side="left", padx=3)
        self.match_city = ctk.CTkEntry(form, placeholder_text="城市", width=120)
        self.match_city.pack(side="left", padx=3)
        self.match_jd = ctk.CTkTextbox(parent, height=140, fg_color="#f8fafc", border_width=1, border_color="#e2e8f0")
        self.match_jd.pack(fill="x", padx=12, pady=6)
        self.match_out = ctk.CTkTextbox(parent, height=320, fg_color="#f8fafc", border_width=1, border_color="#e2e8f0")
        self.match_out.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def fill_job_from_selection(self) -> None:
        job = self.get_selected_job()
        payload = job_payload_from_campus(job)
        if not payload:
            messagebox.showinfo("提示", "请先在「岗位投递」页选中一条岗位")
            return
        self.match_title.delete(0, "end")
        self.match_title.insert(0, str(payload.get("title") or ""))
        self.match_city.delete(0, "end")
        self.match_city.insert(0, str(payload.get("city") or ""))
        self.match_jd.delete("1.0", "end")
        self.match_jd.insert("1.0", str(payload.get("description") or ""))

    def run_match(self) -> None:
        job = {
            "title": self.match_title.get().strip(),
            "city": self.match_city.get().strip(),
            "description": self.match_jd.get("1.0", "end").strip(),
        }
        selected = job_payload_from_campus(self.get_selected_job())
        if selected.get("id"):
            job["id"] = selected["id"]
            job["company"] = selected.get("company") or ""
        if not job["title"] and not job["description"]:
            messagebox.showinfo("提示", "请填写岗位或从选中岗位填入")
            return

        def work() -> dict[str, Any]:
            return self._run_wf("match.explain", {"job": job, "job_id": job.get("id")})

        def done(result: dict[str, Any]) -> None:
            why = _as_list(result.get("why_apply"))
            why_not = _as_list(result.get("why_not"))
            table = result.get("requirement_evidence_table") or []
            lines = [
                f"档位：{result.get('tier')}",
                f"引擎：{result.get('engine') or 'rules'}",
                f"下一步：{result.get('next_action') or ''}",
                "",
                "【为什么值得投】",
                *[f"- {x}" for x in why],
                "",
                "【需要注意】",
                *[f"- {x}" for x in why_not],
                "",
                "【要求↔证据】",
            ]
            for row in table:
                if isinstance(row, dict):
                    lines.append(
                        f"- {row.get('requirement')}: {row.get('status')} | {row.get('evidence') or '—'}"
                    )
            self.match_out.configure(state="normal")
            self.match_out.delete("1.0", "end")
            self.match_out.insert("1.0", "\n".join(lines))
            self.match_out.configure(state="disabled")
            # 缺口 → 行动任务
            for gap in _as_list(result.get("gaps"))[:3]:
                title = f"补齐缺口：{gap}"
                exists = self.coach_db.fetchone(
                    "SELECT id FROM action_tasks WHERE user_id=? AND title=? AND status='open'",
                    (self.user_id, title),
                )
                if not exists:
                    self.coach_db.execute(
                        "INSERT INTO action_tasks(id, user_id, title, reason, status, source, estimated_minutes, created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            self.coach_db.new_id("task_"),
                            self.user_id,
                            title,
                            "来自 match.explain 缺口",
                            "open",
                            "match.explain",
                            60,
                            utc_now_iso(),
                        ),
                    )
            self.refresh_today()

        self._run_async("岗位匹配解释", work, done)

    # ----- 面试 -----
    def _build_interview(self, parent: Any) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(12, 4))
        ctk.CTkButton(row, text="生成故事库", width=110, command=self.build_storybank).pack(side="left", padx=3)
        ctk.CTkButton(row, text="开始模拟面试", width=120, command=self.start_mock).pack(side="left", padx=3)
        ctk.CTkButton(row, text="提交本轮回答", width=120, command=self.answer_mock).pack(side="left", padx=3)

        self.question_label = ctk.CTkLabel(
            parent, text="点击「开始模拟面试」获取第一题", wraplength=900, justify="left", text_color="#162235"
        )
        self.question_label.pack(anchor="w", padx=12, pady=8)
        self.answer_box = ctk.CTkTextbox(parent, height=120, fg_color="#f8fafc", border_width=1, border_color="#e2e8f0")
        self.answer_box.pack(fill="x", padx=12, pady=4)
        self.interview_out = ctk.CTkTextbox(parent, height=320, fg_color="#f8fafc", border_width=1, border_color="#e2e8f0")
        self.interview_out.pack(fill="both", expand=True, padx=12, pady=(4, 12))

    def build_storybank(self) -> None:
        def work() -> dict[str, Any]:
            return self._run_wf("interview.storybank", {})

        def done(result: dict[str, Any]) -> None:
            stories = result.get("stories") or []
            lines = ["【故事库】"]
            for s in stories[:8]:
                if isinstance(s, dict):
                    lines.append(f"- {s.get('title') or s.get('theme') or '故事'}")
                    lines.append(f"  {s.get('summary') or s.get('situation') or ''}")
                else:
                    lines.append(f"- {s}")
            self.interview_out.configure(state="normal")
            self.interview_out.delete("1.0", "end")
            self.interview_out.insert("1.0", "\n".join(lines) or "暂无故事")
            self.interview_out.configure(state="disabled")

        self._run_async("生成故事库", work, done)

    def start_mock(self) -> None:
        job = job_payload_from_campus(self.get_selected_job())

        def work() -> dict[str, Any]:
            return self._run_wf("interview.mock.start", {"job": job})

        def done(result: dict[str, Any]) -> None:
            self.session_id = result.get("session_id")
            q = (result.get("question") or {}).get("question") or "（无题目）"
            self.question_label.configure(text=f"题目：{q}")
            self.answer_box.delete("1.0", "end")
            self.interview_out.configure(state="normal")
            self.interview_out.delete("1.0", "end")
            self.interview_out.insert("1.0", "请作答后点击「提交本轮回答」。一次只答一题。")
            self.interview_out.configure(state="disabled")
            self.refresh_today()

        self._run_async("开始模拟面试", work, done)

    def answer_mock(self) -> None:
        if not self.session_id:
            messagebox.showinfo("提示", "请先开始模拟面试")
            return
        answer = self.answer_box.get("1.0", "end").strip()
        if len(answer) < 5:
            messagebox.showinfo("提示", "请输入本轮回答")
            return

        def work() -> dict[str, Any]:
            return self._run_wf(
                "interview.mock.answer",
                {"session_id": self.session_id, "answer": answer},
            )

        def done(result: dict[str, Any]) -> None:
            fb = result.get("feedback") or {}
            nxt = (result.get("next_question") or {}).get("question")
            done_flag = result.get("done")
            lines = [
                "【面试官听到】",
                str(fb.get("what_heard") or ""),
                "",
                "【缺口】",
                *[f"- {g}" for g in _as_list(fb.get("gaps"))],
                "",
                "【可改进】",
                *[f"- {g}" for g in _as_list(fb.get("improvements"))],
            ]
            self.interview_out.configure(state="normal")
            self.interview_out.delete("1.0", "end")
            self.interview_out.insert("1.0", "\n".join(lines))
            self.interview_out.configure(state="disabled")
            if done_flag:
                self.question_label.configure(text="本场结束。可重新开始，或查看上方反馈。")
            elif nxt:
                self.question_label.configure(text=f"下一题：{nxt}")
                self.answer_box.delete("1.0", "end")

        self._run_async("提交面试回答", work, done)
