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
| GET | `/v1/knowledge/documents` | 已上传文档列表 |
| POST | `/v1/knowledge/documents/upload` | 上传 PDF/DOCX/MD/TXT（解析+切片） |
| GET | `/v1/knowledge/documents/search?q=` | 检索文档切片 |
| GET | `/v1/jobs` | 校招表岗位库（薄集成） |
| GET | `/v1/jobs/{job_id}` | 单岗位详情 |

生产环境请设置 `COACH_ADMIN_TOKEN`，写入接口需带请求头 `X-Coach-Admin-Token`。

## 文档知识库（选项 3）

支持上传任意 `PDF / DOCX / MD / TXT`，服务端解析文本后按标题切片存入 SQLite，供 `build_companion_context` 与 `/v1/knowledge/context` 检索注入 LLM。

```powershell
pip install pypdf python-docx python-multipart
```

上传后可在 H5「我的」页管理，或通过 API `POST /v1/knowledge/documents/upload`（multipart `file` 字段）。

## LLM 外接（Qwen / DeepSeek）

`get_model_adapter()` 支持 OpenAI 兼容协议。**默认规则引擎**；配置 Key 并关闭 `COACH_FORCE_RULES` 后，简历建议 / 匹配 / 准备度走 LLM。

### 快速配置

1. 复制 `.env.example` 为项目根 `.env`，填入 Key  
2. 或直接设置环境变量后启动

**通义千问（推荐中文主路径）**

```powershell
# 申请：https://bailian.console.aliyun.com/ → API-KEY
$env:COACH_FORCE_RULES="0"
$env:COACH_LLM_PROVIDER="qwen"
$env:COACH_LLM_API_KEY="sk-你的百炼Key"
# 可选模型：qwen-plus（默认）/ qwen-max / qwen3.7-plus
# $env:COACH_LLM_MODEL="qwen-plus"
```

**DeepSeek（推荐推理 / 成本敏感）**

```powershell
# 申请：https://platform.deepseek.com/
$env:COACH_FORCE_RULES="0"
$env:COACH_LLM_PROVIDER="deepseek"
$env:COACH_LLM_API_KEY="sk-你的DeepSeekKey"
# 默认 deepseek-v4-flash；更强：deepseek-v4-pro
# 注意：旧名 deepseek-chat / deepseek-reasoner 已退役，代码会自动迁移
```

### 双厂商一起调用（推荐）

同时配置两家 Key 后，系统会：

1. **按任务分流**：简历/面试 → Qwen；匹配/准备度 → DeepSeek  
2. **失败互备**：首选失败自动切另一家  
3. **再失败** → 规则引擎

```powershell
# 项目根目录创建 .env（可复制 .env.example）
COACH_FORCE_RULES=0
COACH_LLM_MODE=dual
DASHSCOPE_API_KEY=sk-你的百炼Key
DEEPSEEK_API_KEY=sk-你的DeepSeekKey
COACH_LLM_PROVIDER=qwen
COACH_LLM_FALLBACK_PROVIDER=deepseek
```

验证：

```text
GET  /v1/llm/status   → dual.enabled=true, ready=true
POST /v1/llm/ping     → 两家 all_ok=true（登录后调用，会产生微量费用）
```

只填一家 Key 时自动降级为单厂商；设 `COACH_LLM_MODE=single` 可强制单厂商。


| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/llm/status` | 是否就绪、provider/model、申请与计费指引 |
| POST | `/v1/llm/complete-json` | 登录后试调 JSON；可 `include_knowledge=true` |

系统提示强制：**无已确认事实不编造、禁止录用概率话术**。LLM 不可用时 workflow 自动回退规则引擎。调用日志写入 `llm_call_logs`（含 token 用量）。

### 如何收费（按量 Token，以官网为准）

双方都是 **先充值/开通，再按调用 Token 计费**；网页聊天免费 ≠ API 免费。

#### 通义千问（阿里云百炼，人民币）

控制台：https://bailian.console.aliyun.com/  
价目：https://help.aliyun.com/zh/model-studio/model-pricing  

| 模型 | 输入 | 输出 | 说明 |
|---|---|---|---|
| `qwen-plus`（默认） | 约 **0.8 元**/百万 tokens | 约 **2 元**/百万 tokens | ≤128K、非思考模式；新用户常有免费额度 |
| `qwen-max` | 约 **2.4 元**/百万 tokens | 约 **9.6 元**/百万 tokens | 质量更高、更贵 |

长上下文有阶梯加价；思考模式输出更贵。正式账单以控制台为准。

**粗算**：一次简历建议约 2k–6k tokens，用 `qwen-plus` 大约 **不到 0.01 元/次** 量级。

#### DeepSeek（美元）

控制台：https://platform.deepseek.com/  
价目：https://api-docs.deepseek.com/quick_start/pricing  

| 模型 | 输入（cache miss） | 输入（cache hit） | 输出 |
|---|---|---|---|
| `deepseek-v4-flash`（默认） | **$0.14**/百万 | **$0.0028**/百万 | **$0.28**/百万 |
| `deepseek-v4-pro` | **$0.435**/百万 | **$0.003625**/百万 | **$0.87**/百万 |

**粗算**：同样一次建议用 Flash，大约 **不到 $0.002/次** 量级。

#### 产品建议

| 场景 | 建议 |
|---|---|
| 日常中文陪伴主路径 | **Qwen `qwen-plus`** |
| 证据链/结构化推理、控成本 | **DeepSeek `deepseek-v4-flash`** |
| 质量优先试点 | Qwen `qwen-max` 或 DeepSeek `deepseek-v4-pro` |
| 稳妥上线 | 主 Qwen + Fallback DeepSeek |

价格可能调整，上线前请再核对官网价目表。
