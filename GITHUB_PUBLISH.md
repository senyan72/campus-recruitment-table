# 发布到 GitHub（安全步骤）

请把**当前项目目录**作为独立 Git 仓库发布，不要在它的上级目录执行 `git add .`。上级目录可能含有其他项目、原始 Excel 或个人文件。

## 1. 发布前检查

```powershell
cd "校招求职表应用程序"
git status --short
rg -n -i "service_role|eyJ[a-zA-Z0-9_-]{20,}|\.supabase\.co|C:\\\\Users\\\\" --glob '!dist/**' --glob '!build/**'
```

确认不包含真实密钥、项目域名、本机路径、账号、数据库、Excel、日志和构建产物。

## 2. 建立独立仓库

若该目录尚未独立初始化：

```powershell
git init
git add README.md AI_HANDOFF.md GITHUB_PUBLISH.md .gitignore app data supabase scripts tests requirements.txt requirements-ocr.txt pytest.ini MAINTENANCE_NOTES.md pyinstaller_tk.py pyinstaller_hooks CampusJobs*.spec
git status --short
git commit -m "Initial public release"
```

在 GitHub 创建一个空仓库后，按 GitHub 页面给出的命令添加远程仓库并推送。请不要勾选“添加 README”，避免和本地 README 冲突。

## 3. 上传后复查

在 GitHub 文件列表中确认没有 `config.json`、`.db`、`.xlsx`、`dist/`、`build/`、`__pycache__/` 或真实密钥。若误传过密钥，即使后来删除文件也应立刻轮换密钥，因为 Git 历史仍可能保留它。
