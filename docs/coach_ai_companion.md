# AI 求职陪伴（仅 AI，无人工 coach）

## 范围

- **做**：证据/优势、准备度、岗位匹配解释、简历多场景建议、今日任务、文字模拟面试、故事库与反馈、可选知识包。
- **不做**：人工教练求助/派单/市场、听录音人工批改。
- **暂缓**：PostgreSQL、对象存储、云 STT/OCR 生产、爬虫、代投、薪资谈判。

## 启动

```powershell
pip install -r requirements.txt
$env:COACH_FORCE_RULES="1"
python -m app.main --mode coach --host 127.0.0.1 --port 8787
```

- H5：http://127.0.0.1:8787/coach  
- 健康检查：`GET /v1/health`  
- Workflow 列表：`GET /v1/workflows`

## Workflows（P0）

| name | 说明 |
|---|---|
| `evidence.extract` / `strengths.mine` | 经历资产与优势链 |
| `readiness.assess` / `readiness.revise` | 准备度 |
| `resume.suggest` / `.bullets` / `.quantify` / `.keywords` | 简历多场景 |
| `resume.generate` / `diff` / `export` | 生成/对比/导出 |
| `match.explain` | 四档 + 证据表 |
| `interview.storybank` / `feedback` / `mock.*` / `debrief` | 面试陪伴 |

## 知识包 / 内部知识库 API

- 内置：`app/coach/knowledge/builtin_campus_v1/`
- 用户可写：项目根 `knowledge/` 或 `COACH_KNOWLEDGE_DIR`（同名 track 优先）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/knowledge/packs` | 内置 + 用户包概览 |
| GET | `/v1/knowledge/interview?track=&stage=` | 面试题上下文 |
| GET | `/v1/knowledge/hr?topic=` | 人事常识 |
| GET | `/v1/knowledge/search?q=` | 简易检索 |
| GET | `/v1/knowledge/context?track=&q=` | 给 LLM 的组装上下文 |
| PUT | `/v1/knowledge/interview/{track}` | 写入/合并题库（可选 `X-Coach-Admin-Token`） |
| PUT | `/v1/knowledge/hr/{topic}` | 写入人事短文 |
| PUT | `/v1/knowledge/meta` | 更新用户包 meta |

生产环境请设置 `COACH_ADMIN_TOKEN`，写入接口需带请求头 `X-Coach-Admin-Token`。

## LLM 外接（可直接接入）

`get_model_adapter()` 支持 OpenAI 兼容协议（通义 DashScope / DeepSeek / 自定义网关）。

```powershell
$env:COACH_FORCE_RULES="0"
$env:COACH_LLM_PROVIDER="qwen"   # 或 deepseek / openai / openai_compatible
$env:COACH_LLM_API_KEY="你的Key"
# 可选覆盖：
# $env:COACH_LLM_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
# $env:COACH_LLM_MODEL="qwen-max"
# 可选第二路由：
# $env:COACH_LLM_FALLBACK_PROVIDER="deepseek"
# $env:COACH_LLM_FALLBACK_API_KEY="..."
```

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/llm/status` | 是否就绪、provider/model（不回传密钥） |
| POST | `/v1/llm/complete-json` | 登录后直接试调 JSON 生成；可 `include_knowledge=true` 注入知识库 |

系统提示默认强制：**无已确认事实不编造、禁止录用概率话术**。LLM 不可用时 workflow 自动回退规则引擎。

环境变量：`COACH_LLM_API_KEY`、`COACH_LLM_PROVIDER`、`COACH_LLM_BASE_URL`、`COACH_LLM_MODEL`、`COACH_FORCE_RULES`
