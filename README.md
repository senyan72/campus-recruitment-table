
# campus-recruitment-table
设计分为admin端和viewer端，admin端完成数据采集，审核并同步到云端；viewer端主要是查看岗位信息并手动更新岗位投递进度

# 校招求职表（Windows 桌面版）

用于收集、整理和查看校园招聘/实习岗位的 Windows 桌面应用。可完全离线运行，也可连接你自己创建的 Supabase 项目进行同步。

## 功能

- **Viewer**：查看岗位、筛选、打开原始链接、维护本机投递进度和导出 CSV。
- **Admin**：导入种子表、探测招聘来源、采集岗位、审核并发布到云端、管理 Viewer 账号。
- **深度采集限额**：启动深度采集时可输入本次最多处理的企业数，默认 20 家；可分批运行，减少卡顿。
- **本地优先**：没有云端配置时仍可使用本地 SQLite；个人投递记录默认不会上传。

## 环境要求与安装

- Windows 10/11
- Python 3.12+

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 第一次使用（先创建自己的数据库）

1. 按 [`supabase/README.md`](supabase/README.md) 在自己的 Supabase 项目中执行 `schema.sql` 和 migrations。
2. 复制 `data/config.example.json` 为 `%LOCALAPPDATA%\campus-jobs\config.json`，填入自己的 Project URL 和 anon key。
3. 仅在本机 Admin 配置 service role key；绝不要提交、分享或打包该密钥。
4. 运行应用：

```powershell
python -m app.main --mode viewer
python -m app.main --mode admin
```

5. 在 Admin 中创建至少一个 Viewer 账号并推送到云端，再把同一项目的 URL/anon key 提供给其他使用者。

配置也可以通过环境变量提供：`CAMPUS_JOBS_SUPABASE_URL`、`CAMPUS_JOBS_SUPABASE_ANON_KEY`、`CAMPUS_JOBS_SUPABASE_SERVICE_ROLE_KEY`。

## 导入、采集与打包

在 Admin 中选择本地 `.xlsx` 种子表，先小批量试导入，确认数据无误后再完整采集/发布。种子表、SQLite 数据库、日志和导出的 CSV 都属于本地数据，不要提交到公开仓库。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
```

生成的 `dist/` 目录已被 Git 忽略；Viewer 打包流程只允许包含 anon key，不会携带 service role key。

## 安全与隐私

- 本仓库不包含作者个人资料、真实岗位表、账号密码或 Supabase 密钥。
- 发布前请运行 `git status` 和敏感信息搜索，确认没有误加入 `config.json`、`.db`、`.xlsx`、日志或构建产物。
- 岗位信息来自第三方页面，使用时请遵守来源网站条款，不要批量转售或公开发布个人信息。

## 测试

```powershell
python -m pytest -q
```

## 目录结构

```text
app/                 应用代码
data/                脱敏后的配置和种子示例
supabase/            数据库 schema、迁移和初始化说明
scripts/             打包及自检脚本
tests/               自动化测试
```
