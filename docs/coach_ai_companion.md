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

## 知识包

- 内置：`app/coach/knowledge/builtin_campus_v1/`
- 用户可选：项目根 `knowledge/` 或 `COACH_KNOWLEDGE_DIR`，同名 track 优先覆盖内置。

## LLM 外接

`app/coach/ai/__init__.py`：`get_model_adapter()`  
环境变量：`COACH_LLM_API_KEY`、`COACH_LLM_PROVIDER`、`COACH_LLM_BASE_URL`、`COACH_LLM_MODEL`、`COACH_FORCE_RULES=1`
