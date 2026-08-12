const state = {
  token: localStorage.getItem("coach_token") || "",
  userId: localStorage.getItem("coach_user") || "",
  tab: "today",
  facts: [],
  versionId: localStorage.getItem("coach_version") || "",
  sessionId: "",
  selectedJob: null,
  jobs: [],
};

async function api(path, { method = "GET", body, formData } = {}) {
  const headers = {};
  if (!formData) headers["Content-Type"] = "application/json";
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const res = await fetch(path, {
    method,
    headers,
    body: formData || (body ? JSON.stringify(body) : undefined),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.message || res.statusText);
  return data;
}

function el(html) {
  const d = document.createElement("div");
  d.innerHTML = html.trim();
  return d.firstChild;
}

function asList(v) {
  if (Array.isArray(v)) return v;
  if (v == null || v === "") return [];
  return [String(v)];
}

function tierClass(tier) {
  if (tier === "优先投") return "tier-good";
  if (tier === "可以投") return "tier-ok";
  if (tier === "补充后投") return "tier-warn";
  return "tier-bad";
}

function render() {
  document.querySelectorAll(".tabs button").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === state.tab);
  });
  const root = document.getElementById("app");
  root.innerHTML = "";
  if (!state.token) {
    root.appendChild(loginPanel());
    return;
  }
  const map = {
    today: todayPanel,
    resume: resumePanel,
    match: matchPanel,
    interview: interviewPanel,
    me: mePanel,
  };
  root.appendChild(map[state.tab]());
}

function loginPanel() {
  const p = el(`<section class="panel">
    <h2>进入 AI 求职陪伴</h2>
    <p class="muted">仅 AI 陪伴 · 无人工教练 · 建议基于你已确认的事实</p>
    <label>开发账号</label>
    <input id="account" value="pilot_demo" />
    <button class="btn" id="loginBtn">开始</button>
    <div class="error" id="err"></div>
  </section>`);
  p.querySelector("#loginBtn").onclick = async () => {
    try {
      const account = p.querySelector("#account").value.trim();
      const data = await api("/v1/auth/dev-login", { method: "POST", body: { account } });
      state.token = data.token;
      state.userId = data.user_id;
      localStorage.setItem("coach_token", state.token);
      localStorage.setItem("coach_user", state.userId);
      render();
    } catch (e) {
      p.querySelector("#err").textContent = e.message;
    }
  };
  return p;
}

function todayPanel() {
  const p = el(`<section class="panel">
    <h2>今日最重要</h2>
    <div id="primary" class="hero muted">加载中…</div>
    <ol class="steps" id="steps"></ol>
    <div id="tasks"></div>
  </section>`);
  Promise.all([api("/v1/home/today"), api("/v1/facts").catch(() => ({ facts: [] }))])
    .then(([data, factsData]) => {
      const primary = data.primary || {};
      const facts = factsData.facts || [];
      const pending = facts.filter((f) => f.status === "pending").length;
      const confirmed = facts.filter((f) => f.status === "confirmed").length;
      p.querySelector("#primary").innerHTML = `
        <strong>${esc(primary.title || "建立证据基础")}</strong>
        <p class="muted">${esc(primary.reason || "")}</p>`;
      const steps = [
        { done: !!state.versionId, label: "上传简历", tab: "resume" },
        { done: pending === 0 && confirmed > 0, label: "确认事实", tab: "resume" },
        { done: confirmed >= 2, label: "岗位匹配", tab: "match" },
        { done: false, label: "模拟面试", tab: "interview" },
      ];
      p.querySelector("#steps").innerHTML = steps
        .map(
          (s, i) => `<li class="${s.done ? "done" : ""}">
            <button class="link" data-tab="${s.tab}">${i + 1}. ${s.label}${s.done ? " ✓" : ""}</button>
          </li>`
        )
        .join("");
      p.querySelectorAll("#steps .link").forEach((btn) => {
        btn.onclick = () => {
          state.tab = btn.dataset.tab;
          render();
        };
      });
      const tasks = data.tasks || [];
      p.querySelector("#tasks").innerHTML = tasks.length
        ? `<h3>待办</h3>${tasks.map((t) => `<div class="card">${esc(t.title)}<div class="muted">${esc(t.reason || "")}</div></div>`).join("")}`
        : `<p class="muted">已确认 ${confirmed} 条事实 · 待确认 ${pending} 条</p>`;
    })
    .catch((e) => {
      p.querySelector("#primary").innerHTML = `<div class="error">${esc(e.message)}</div>`;
    });
  return p;
}

async function refreshFacts(container) {
  const data = await api("/v1/facts");
  state.facts = data.facts || [];
  const pending = state.facts.filter((f) => f.status === "pending");
  const confirmed = state.facts.filter((f) => f.status === "confirmed");
  container.querySelector("#factPending").innerHTML = pending.length
    ? pending
        .map(
          (f) => `<label class="fact"><input type="checkbox" value="${esc(f.id)}" checked />
        <span>${esc(f.text)}</span></label>`
        )
        .join("")
    : `<p class="muted">暂无待确认事实</p>`;
  container.querySelector("#factConfirmed").innerHTML = confirmed.length
    ? confirmed.map((f) => `<div class="chip">${esc(f.text)}</div>`).join("")
    : `<p class="muted">确认事实后，建议与匹配才更可靠</p>`;
}

function resumePanel() {
  const p = el(`<section class="panel">
    <h2>简历与证据</h2>
    <p class="muted">步骤 1：粘贴简历 → 提取事实 → 勾选确认</p>
    <label>简历文本</label>
    <textarea id="resumeText" placeholder="粘贴简历全文…"></textarea>
    <button class="btn" id="upload">提取证据</button>
    <h3>待确认事实</h3>
    <div id="factPending"></div>
    <button class="btn soft" id="confirm">确认选中事实</button>
    <h3>已确认事实</h3>
    <div id="factConfirmed"></div>
    <hr />
    <p class="muted">步骤 2：基于已确认事实生成建议</p>
    <label>建议场景</label>
    <select id="scenario">
      <option value="resume.suggest">总体建议</option>
      <option value="resume.suggest.bullets">成就化 bullet</option>
      <option value="resume.suggest.quantify">量化核验</option>
      <option value="resume.suggest.keywords">岗位关键词</option>
    </select>
    <label>目标岗位</label>
    <input id="jobTitle" value="数据分析实习生" />
    <button class="btn soft" id="suggest">生成建议</button>
    <div id="suggestions"></div>
    <div class="error" id="err"></div>
  </section>`);
  refreshFacts(p).catch((e) => {
    p.querySelector("#err").textContent = e.message;
  });
  p.querySelector("#upload").onclick = async () => {
    try {
      const text = p.querySelector("#resumeText").value;
      const data = await api("/v1/resumes/upload-text", { method: "POST", body: { text } });
      state.versionId = data.version_id;
      localStorage.setItem("coach_version", state.versionId);
      await refreshFacts(p);
      p.querySelector("#err").textContent = "";
    } catch (e) {
      p.querySelector("#err").textContent = e.message;
    }
  };
  p.querySelector("#confirm").onclick = async () => {
    try {
      const ids = [...p.querySelectorAll("#factPending input:checked")].map((x) => x.value);
      if (!ids.length) throw new Error("请至少选择一条待确认事实");
      await api("/v1/facts/confirm", { method: "POST", body: { fact_ids: ids } });
      await refreshFacts(p);
    } catch (e) {
      p.querySelector("#err").textContent = e.message;
    }
  };
  p.querySelector("#suggest").onclick = async () => {
    try {
      if (!state.versionId) throw new Error("请先上传简历");
      const name = p.querySelector("#scenario").value;
      const data = await api(`/v1/workflows/${name}/run`, {
        method: "POST",
        body: {
          payload: {
            version_id: state.versionId,
            job: {
              title: p.querySelector("#jobTitle").value,
              description: p.querySelector("#jobTitle").value,
            },
          },
        },
      });
      const items = data.result?.suggestions || [];
      const engine = data.result?.engine || "rules";
      p.querySelector("#suggestions").innerHTML = `
        <p class="muted">引擎：${esc(engine)}</p>
        ${items
          .map(
            (s) => `<div class="card ${s.needs_proof ? "needs-proof" : ""}">
            <div class="tag">${esc(s.section || "")}${s.needs_proof ? " · 需补证据" : ""}</div>
            <div><strong>建议</strong> ${esc(s.suggestion)}</div>
            <div class="muted">${esc(s.reason || "")}</div>
          </div>`
          )
          .join("")}`;
    } catch (e) {
      p.querySelector("#err").textContent = e.message;
    }
  };
  return p;
}

function matchPanel() {
  const p = el(`<section class="panel">
    <h2>岗位匹配解释</h2>
    <p class="muted">从岗位库选择，或手动填写 JD</p>
    <button class="btn soft" id="loadJobs">加载岗位库</button>
    <select id="jobPick"><option value="">手动填写</option></select>
    <label>岗位名称</label><input id="title" value="数据分析实习生" />
    <label>城市</label><input id="city" value="上海" />
    <label>JD 文本</label><textarea id="jd">负责数据分析、沟通协作与项目交付</textarea>
    <button class="btn" id="run">生成匹配解释</button>
    <div id="result"></div>
    <div class="error" id="err"></div>
  </section>`);
  p.querySelector("#loadJobs").onclick = async () => {
    try {
      const data = await api("/v1/jobs?limit=20");
      state.jobs = data.jobs || [];
      const sel = p.querySelector("#jobPick");
      sel.innerHTML =
        `<option value="">手动填写</option>` +
        state.jobs
          .map(
            (j, i) =>
              `<option value="${i}">${esc(j.company || "")} · ${esc(j.title || "")} · ${esc(j.city || "")}</option>`
          )
          .join("");
    } catch (e) {
      p.querySelector("#err").textContent = e.message;
    }
  };
  p.querySelector("#jobPick").onchange = () => {
    const idx = p.querySelector("#jobPick").value;
    if (idx === "") return;
    const job = state.jobs[Number(idx)];
    if (!job) return;
    state.selectedJob = job;
    p.querySelector("#title").value = job.title || "";
    p.querySelector("#city").value = job.city || "";
    p.querySelector("#jd").value = job.description || "";
  };
  p.querySelector("#run").onclick = async () => {
    try {
      const payload = {
        job: {
          id: state.selectedJob?.id,
          title: p.querySelector("#title").value,
          city: p.querySelector("#city").value,
          description: p.querySelector("#jd").value,
        },
      };
      if (state.selectedJob?.id) payload.job_id = state.selectedJob.id;
      const data = await api("/v1/workflows/match.explain/run", {
        method: "POST",
        body: { payload },
      });
      const r = data.result || {};
      const table = asList(r.requirement_evidence_table)
        .map(
          (row) => `<tr class="${row.status === "matched" ? "ok" : "gap"}">
          <td>${esc(row.requirement)}</td>
          <td>${esc(row.evidence || "—")}</td>
          <td>${row.status === "matched" ? "已覆盖" : "缺口"}</td>
        </tr>`
        )
        .join("");
      p.querySelector("#result").innerHTML = `
        <div class="tier ${tierClass(r.tier)}">${esc(r.tier)}</div>
        <p class="muted">引擎：${esc(r.engine || "rules")} · 下一步：${esc(r.next_action || "")}</p>
        <div class="cols">
          <div><h4>为什么值得投</h4><ul>${asList(r.why_apply).map((x) => `<li>${esc(typeof x === "string" ? x : JSON.stringify(x))}</li>`).join("")}</ul></div>
          <div><h4>需要注意</h4><ul>${asList(r.why_not).map((x) => `<li>${esc(typeof x === "string" ? x : JSON.stringify(x))}</li>`).join("")}</ul></div>
        </div>
        <table class="ev-table"><thead><tr><th>要求</th><th>证据</th><th>状态</th></tr></thead><tbody>${table}</tbody></table>`;
    } catch (e) {
      p.querySelector("#err").textContent = e.message;
    }
  };
  return p;
}

function interviewPanel() {
  const p = el(`<section class="panel">
    <h2>面试陪伴</h2>
    <button class="btn soft" id="stories">生成故事库</button>
    <button class="btn" id="start">开始模拟面试</button>
    <div id="q" class="question muted"></div>
    <textarea id="answer" placeholder="一次只答一题，尽量用 STAR 结构…"></textarea>
    <button class="btn" id="send">提交回答</button>
    <div id="feedback"></div>
    <div class="error" id="err"></div>
  </section>`);
  p.querySelector("#stories").onclick = async () => {
    try {
      const data = await api("/v1/workflows/interview.storybank/run", { method: "POST", body: { payload: {} } });
      const stories = data.result?.stories || [];
      p.querySelector("#feedback").innerHTML = stories.length
        ? stories
            .map(
              (s) => `<div class="card"><strong>${esc(s.title || s.theme || "故事")}</strong>
            <div class="muted">${esc(s.summary || s.situation || "")}</div></div>`
            )
            .join("")
        : `<pre>${esc(JSON.stringify(data, null, 2))}</pre>`;
    } catch (e) {
      p.querySelector("#err").textContent = e.message;
    }
  };
  p.querySelector("#start").onclick = async () => {
    try {
      const data = await api("/v1/workflows/interview.mock.start/run", { method: "POST", body: { payload: {} } });
      state.sessionId = data.result.session_id;
      p.querySelector("#q").textContent = data.result.question?.question || "准备开始…";
      p.querySelector("#feedback").innerHTML = "";
    } catch (e) {
      p.querySelector("#err").textContent = e.message;
    }
  };
  p.querySelector("#send").onclick = async () => {
    try {
      const data = await api("/v1/workflows/interview.mock.answer/run", {
        method: "POST",
        body: { payload: { session_id: state.sessionId, answer: p.querySelector("#answer").value } },
      });
      const fb = data.result?.feedback || {};
      p.querySelector("#q").textContent =
        data.result.next_question?.question || (data.result.done ? "本场结束，查看反馈" : "");
      p.querySelector("#feedback").innerHTML = `
        <div class="card"><strong>面试官听到</strong><p>${esc(fb.what_heard || "")}</p></div>
        <div class="card"><strong>缺口</strong><ul>${(fb.gaps || []).map((g) => `<li>${esc(g)}</li>`).join("")}</ul></div>
        <div class="card"><strong>可改进</strong><ul>${(fb.improvements || []).map((g) => `<li>${esc(g)}</li>`).join("")}</ul></div>`;
      p.querySelector("#answer").value = "";
    } catch (e) {
      p.querySelector("#err").textContent = e.message;
    }
  };
  return p;
}

function mePanel() {
  const p = el(`<section class="panel">
    <h2>我的</h2>
    <p class="muted">账号 ${esc(state.userId)}</p>
    <h3>知识文档</h3>
    <p class="muted">上传 PDF/DOCX/MD 作为内部知识（解析后供 LLM 检索）</p>
    <input type="file" id="docFile" accept=".pdf,.docx,.md,.txt" />
    <button class="btn soft" id="uploadDoc">上传文档</button>
    <div id="docs"></div>
    <button class="btn ghost" id="logout">退出登录</button>
    <div class="error" id="err"></div>
  </section>`);
  api("/v1/knowledge/documents")
    .then((data) => {
      const docs = data.documents || [];
      p.querySelector("#docs").innerHTML = docs.length
        ? docs
            .map(
              (d) => `<div class="chip">${esc(d.filename)} · ${d.meta?.chunk_count || 0} 切片</div>`
            )
            .join("")
        : `<p class="muted">暂无上传文档</p>`;
    })
    .catch(() => {});
  p.querySelector("#uploadDoc").onclick = async () => {
    try {
      const file = p.querySelector("#docFile").files[0];
      if (!file) throw new Error("请选择文件");
      const fd = new FormData();
      fd.append("file", file);
      await api("/v1/knowledge/documents/upload", { method: "POST", formData: fd });
      render();
    } catch (e) {
      p.querySelector("#err").textContent = e.message;
    }
  };
  p.querySelector("#logout").onclick = () => {
    state.token = "";
    localStorage.clear();
    render();
  };
  return p;
}

document.getElementById("tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tab]");
  if (!btn) return;
  state.tab = btn.dataset.tab;
  render();
});

render();
