# 可选用户知识包（覆盖内置）

将自定义内容放在此目录，或设置环境变量 `COACH_KNOWLEDGE_DIR`。

```text
knowledge/
  meta.yaml
  interview_questions/
    campus_general.yaml
    tech_backend.yaml   # 可选分族
  hr_basics/
    campus_process.md
```

同名 track 优先于 `app/coach/knowledge/builtin_campus_v1/`。
不提供也可运行（使用内置校招默认包）。
