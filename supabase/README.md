# Supabase 初始化与后续流程

每位部署者都应创建自己的 Supabase 项目。不要复用他人的项目 URL、密钥或数据。

## 1. 创建项目

在 <https://supabase.com> 新建项目，记下 Project URL、`anon`（publishable）key 和仅供本机 Admin 使用的 `service_role` key。

## 2. 执行 SQL

打开 Dashboard → **SQL Editor**，按顺序执行：

1. `schema.sql`
2. `migrations/20260803_schema_compat.sql`
3. `migrations/20260803_sync_tombstones.sql`
4. `migrations/20260803_viewer_sessions.sql`
5. `migrations/20260804_account_expiry.sql`

每个文件在同一项目执行一次即可。重复执行前建议先备份。

## 3. 配置应用

复制 `data/config.example.json` 到 `%LOCALAPPDATA%\campus-jobs\config.json`，填写 `supabase_url` 和 `supabase_anon_key`；仅本机 Admin 填写 `supabase_service_role_key`。service role key 具有绕过 RLS 的权限，不能放进 Git、Viewer 压缩包、截图或聊天记录。

## 4. 创建账号并发布数据

启动 Admin，在“账号管理”中创建 Viewer 账号并推送到云端；导入自己的岗位种子表，审核采集结果后再推送。Viewer 通过登录 RPC 获取短期会话，不直接读取 `jobs`/`companies` 表。

## 5. 邀请其他用户

只向可信用户提供 Viewer、Project URL、anon key、账号和临时密码。首次登录后修改密码，并按需设置到期日。不要共享 Admin 或 service role key。

## 6. 备份、轮换与删除

- 在 Supabase **Database → Backups** 设置备份策略并定期导出必要数据。
- 怀疑泄露时，立即在 Project Settings → API 轮换 key，并更新本地配置。
- 停止使用时，删除云端数据/项目，再删除 `%LOCALAPPDATA%\campus-jobs` 下的数据库、备份和日志。

## 7. 提交前检查

```powershell
git status --short
rg -n -i "service_role|eyJ[a-zA-Z0-9_-]{20,}|\.supabase\.co|C:\\\\Users\\\\" --glob '!dist/**' --glob '!build/**'
```

确认没有真实密钥、项目域名、本机用户名、个人路径或生产数据后，再提交到 GitHub。
