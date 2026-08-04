# AI 维护交接说明

本项目已移除原始种子表、用户数据库、账号、密钥和作者本机路径。后续让 AI 更新项目时，应只提供公开仓库中的脱敏代码、示例配置、测试夹具和本说明；不要上传真实 Excel、SQLite 数据库、日志、Supabase 密钥或用户导出文件。

## 可以让 AI 做什么

- 根据 `README.md`、`supabase/README.md`、代码和测试修复问题、添加功能、更新文档。
- 根据 `data/seeds.example.csv` 设计导入格式或编写新的脱敏示例。
- 为自己的 Supabase 项目补充迁移 SQL，并同步更新 `supabase/README.md` 的执行顺序。
- 在不读取生产数据的前提下，改进招聘来源适配、过滤规则和界面。

## 向 AI 提问时应说明

1. 目标用户是 Viewer、Admin，还是两者。
2. 需要修改的模块、期望行为和验收方式。
3. 是否需要数据库变更；如需要，要求 AI 新建带日期的 migration，并说明升级步骤。
4. 不要粘贴真实 URL、密钥、用户名、密码、邮箱、电话号码、简历或完整岗位原始数据。

示例：`请为 Admin 增加一个岗位字段。同步更新 SQLite、Supabase schema/migration、导入逻辑、界面、测试和文档；不要使用任何真实项目配置或种子数据。`

## AI 修改后的必做检查

```powershell
python -m pytest -q
git status --short
rg -n -i "service_role|eyJ[a-zA-Z0-9_-]{20,}|\.supabase\.co|C:\\\\Users\\\\" --glob '!dist/**' --glob '!build/**'
```

提交前应人工检查变更，特别是 SQL 的 RLS/RPC 权限、打包脚本是否只带 Viewer 可用配置，以及 `.gitignore` 是否继续排除私有数据。
