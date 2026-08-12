# Windows 本机同步 AI Viewer 指南

**唯一正式项目目录**：`校招求职表应用程序`（例如 `C:\Users\johan\Desktop\coding\校招求职表应用程序`）

**已废弃、请勿再使用**：

- `campus_recruitment`
- `campus-recruitment-table-ai`
- `校招求职表应用程序最新版`（请把数据迁到正式目录后删除或归档）

所有 AI 陪伴相关修改都应落在 **校招求职表应用程序** 内，不要另建旁路文件夹。

---

## 情况一：`git clone` 连不上 GitHub（常见）

报错类似：`Failed to connect to github.com:443`

### 推荐：ZIP + 覆盖安装到正式目录

1. 用浏览器打开（可挂代理/VPN）：
   - https://github.com/senyan72/campus-recruitment-table/archive/refs/heads/cursor/ai-companion-workflows-7a1d.zip
2. 解压到**临时目录**（任意英文名，例如 `C:\Users\johan\Desktop\coding\_zip_tmp`）
3. 进入解压后的文件夹，执行（**-Target 必填**，路径用你本机正式项目目录）：

```powershell
cd "C:\Users\johan\Desktop\coding\校招求职表应用程序最新版\campus-recruitment-table-cursor-ai-companion-workflows-7a1d"
powershell -ExecutionPolicy Bypass -File ".\scripts\install_ai_into_project.ps1" -Target "C:\Users\johan\Desktop\coding\校招求职表应用程序"
```

> **说明**：`.ps1` 脚本源码为纯英文（ASCII），避免 Windows PowerShell 5 中文乱码导致语法错误。中文路径通过命令行 `-Target` 传入即可。

4. 在正式目录启动：

```powershell
cd "C:\Users\johan\Desktop\coding\校招求职表应用程序"
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
# 若尚无 .env：copy .env.example .env 并填入 API Key
python -m app.main --mode viewer
```

5. 验证：

```powershell
findstr /C:"AI 陪伴" app\ui\viewer.py
dir app\ui\coach_viewer.py
```

启动后顶部应有 **岗位投递 | AI 陪伴** 两个页签。

### 从废弃目录迁移配置

若你之前在 `campus_recruitment` 或 `校招求职表应用程序最新版` 里有 `.env` / `config.json`，安装脚本会自动尝试迁移到正式目录（仅当正式目录尚无对应文件时）。

也可手动指定旧目录：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_ai_into_project.ps1 -LegacyDir "C:\Users\johan\Desktop\coding\campus_recruitment"
```

---

## 情况二：网络正常 — 在正式目录内 git 同步

```powershell
cd C:\Users\johan\Desktop\coding
powershell -ExecutionPolicy Bypass -File "校招求职表应用程序\scripts\sync_ai_viewer_to_windows.ps1"
```

或首次克隆到正式目录：

```powershell
cd C:\Users\johan\Desktop\coding
git clone -b cursor/ai-companion-workflows-7a1d https://github.com/senyan72/campus-recruitment-table.git "校招求职表应用程序"
cd "校招求职表应用程序"
powershell -ExecutionPolicy Bypass -File scripts\sync_ai_viewer_to_windows.ps1
```

脚本会：

1. 在 `校招求职表应用程序` 内拉取/更新分支  
2. 从废弃目录迁移 `.env`、`config.json`（若存在）  
3. 安装依赖并校验 `coach_viewer.py`  

## 已有正式目录时更新

```powershell
cd "C:\Users\johan\Desktop\coding\校招求职表应用程序"
git fetch origin
git checkout cursor/ai-companion-workflows-7a1d
git pull origin cursor/ai-companion-workflows-7a1d
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m app.main --mode viewer
```

## 注意

- **始终**在 `校招求职表应用程序` 内开发与运行，不要回到 `campus_recruitment`。  
- 双击旧版 `CampusJobsViewer.exe` **不会**自动出现 AI；需用源码启动或重新打包 exe。  
- Git 代理示例（端口按实际修改）：

```powershell
git config --global http.proxy http://127.0.0.1:7890
git config --global https.proxy http://127.0.0.1:7890
```
