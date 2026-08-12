const state = {
  token: localStorage.getItem("coach_token") || "",
  userId: localStorage.getItem("coach_user") || "",
  tab: "today",
  lastFacts: [],
  versionId: localStorage.getItem("coach_version") || "",
  sessionId: "",
};

async function api(path, { method = "GET", body } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const res = await fetch(path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
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
    <h2>开发登录</h2>
    <p class="muted">当前范围：仅 AI 陪伴，无人工教练入口。</p>
    <label>账号</label>
    <input id="account" value="pilot_demo" />
    <button class="btn" id="loginBtn">进入陪伴</button>
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
  const p = el(`<section class="panel"><h2>今日最重要</h2><div id="box" class="muted">加载中…</div></section>`);
  api("/v1/home/today").then((data) => {
    const primary = data.primary || {};
    p.querySelector("#box").innerHTML = `<strong>${primary.title || ""}</strong>
      <p class="muted">${primary.reason || ""}</p>
      <pre>${JSON.stringify(data.tasks || [], null, 2)}</pre>`;
  }).catch((e) => { p.querySelector("#box").innerHTML = `<div class="error">${e.message}</div>`; });
  return p;
}

function resumePanel() {
  const p = el(`<section class="panel">
    <h2>简历与证据</h2>
    <label>粘贴简历文本</label>
    <textarea id="resumeText" placeholder="粘贴简历…"></textarea>
    <button class="btn" id="upload">提取证据</button>
    <button class="btn soft" id="confirm">确认全部待确认事实</button>
    <label>建议场景</label>
    <select id="scenario">
      <option value="resume.suggest">general</option>
      <option value="resume.suggest.bullets">bullets</option>
      <option value="resume.suggest.quantify">quantify</option>
      <option value="resume.suggest.keywords">keywords</option>
    </select>
    <label>目标岗位名（关键词场景用）</label>
    <input id="jobTitle" value="数据分析实习生" />
    <button class="btn soft" id="suggest">生成建议</button>
    <button class="btn soft" id="strengths">挖掘优势</button>
    <pre id="out"></pre>
    <div class="error" id="err"></div>
  </section>`);
  const out = (x) => { p.querySelector("#out").textContent = JSON.stringify(x, null, 2); };
  p.querySelector("#upload").onclick = async () => {
    try {
      const text = p.querySelector("#resumeText").value;
      const data = await api("/v1/resumes/upload-text", { method: "POST", body: { text } });
      state.versionId = data.version_id;
      localStorage.setItem("coach_version", state.versionId);
      state.lastFacts = (data.extract?.assets || []).map((a) => a.fact_id).filter(Boolean);
      out(data);
    } catch (e) { p.querySelector("#err").textContent = e.message; }
  };
  p.querySelector("#confirm").onclick = async () => {
    try {
      const facts = await api("/v1/facts");
      const pending = (facts.facts || []).filter((f) => f.status === "pending").map((f) => f.id);
      const data = await api("/v1/facts/confirm", { method: "POST", body: { fact_ids: pending } });
      out(data);
    } catch (e) { p.querySelector("#err").textContent = e.message; }
  };
  p.querySelector("#suggest").onclick = async () => {
    try {
      const name = p.querySelector("#scenario").value;
      const data = await api(`/v1/workflows/${name}/run`, {
        method: "POST",
        body: {
          payload: {
            version_id: state.versionId,
            job: { title: p.querySelector("#jobTitle").value, description: p.querySelector("#jobTitle").value },
          },
        },
      });
      out(data);
    } catch (e) { p.querySelector("#err").textContent = e.message; }
  };
  p.querySelector("#strengths").onclick = async () => {
    try {
      const data = await api("/v1/workflows/strengths.mine/run", { method: "POST", body: { payload: {} } });
      out(data);
    } catch (e) { p.querySelector("#err").textContent = e.message; }
  };
  return p;
}

function matchPanel() {
  const p = el(`<section class="panel">
    <h2>岗位匹配解释</h2>
    <label>岗位名称</label><input id="title" value="数据分析实习生" />
    <label>城市</label><input id="city" value="上海" />
    <label>JD 文本</label><textarea id="jd">负责数据分析、沟通协作与项目交付</textarea>
    <button class="btn" id="run">生成四档解释</button>
    <pre id="out"></pre>
    <div class="error" id="err"></div>
  </section>`);
  p.querySelector("#run").onclick = async () => {
    try {
      const data = await api("/v1/workflows/match.explain/run", {
        method: "POST",
        body: {
          payload: {
            job: {
              title: p.querySelector("#title").value,
              city: p.querySelector("#city").value,
              description: p.querySelector("#jd").value,
            },
          },
        },
      });
      p.querySelector("#out").textContent = JSON.stringify(data, null, 2);
    } catch (e) { p.querySelector("#err").textContent = e.message; }
  };
  return p;
}

function interviewPanel() {
  const p = el(`<section class="panel">
    <h2>面试陪伴</h2>
    <button class="btn" id="stories">生成故事库</button>
    <button class="btn soft" id="start">开始模拟（文字）</button>
    <div id="q" class="muted"></div>
    <textarea id="answer" placeholder="一次只答一题…"></textarea>
    <button class="btn" id="send">提交回答</button>
    <pre id="out"></pre>
    <div class="error" id="err"></div>
  </section>`);
  const out = (x) => { p.querySelector("#out").textContent = JSON.stringify(x, null, 2); };
  p.querySelector("#stories").onclick = async () => {
    try { out(await api("/v1/workflows/interview.storybank/run", { method: "POST", body: { payload: {} } })); }
    catch (e) { p.querySelector("#err").textContent = e.message; }
  };
  p.querySelector("#start").onclick = async () => {
    try {
      const data = await api("/v1/workflows/interview.mock.start/run", { method: "POST", body: { payload: {} } });
      state.sessionId = data.result.session_id;
      p.querySelector("#q").textContent = data.result.question?.question || "";
      out(data);
    } catch (e) { p.querySelector("#err").textContent = e.message; }
  };
  p.querySelector("#send").onclick = async () => {
    try {
      const data = await api("/v1/workflows/interview.mock.answer/run", {
        method: "POST",
        body: { payload: { session_id: state.sessionId, answer: p.querySelector("#answer").value } },
      });
      p.querySelector("#q").textContent = data.result.next_question?.question || (data.result.done ? "本场结束" : "");
      out(data);
    } catch (e) { p.querySelector("#err").textContent = e.message; }
  };
  return p;
}

function mePanel() {
  const p = el(`<section class="panel">
    <h2>我的</h2>
    <p class="muted">user: ${state.userId}</p>
    <button class="btn soft" id="wf">查看 workflows</button>
    <button class="btn soft" id="kn">知识包预览</button>
    <button class="btn ghost" id="logout">退出</button>
    <pre id="out"></pre>
  </section>`);
  p.querySelector("#wf").onclick = async () => {
    p.querySelector("#out").textContent = JSON.stringify(await api("/v1/workflows"), null, 2);
  };
  p.querySelector("#kn").onclick = async () => {
    p.querySelector("#out").textContent = JSON.stringify(await api("/v1/knowledge/interview"), null, 2);
  };
  p.querySelector("#logout").onclick = () => {
    state.token = ""; localStorage.clear(); render();
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
