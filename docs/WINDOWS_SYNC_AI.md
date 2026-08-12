# Windows 本机同步 AI Viewer 指南

你的旧文件夹 `校招求职表应用程序最新版` **没有** `app/ui/coach_viewer.py`，因此看不到「AI 陪伴」页签。请用下面任一方式同步。

## 方式一：一键脚本（推荐）

在 **PowerShell** 中执行（把路径改成你的旧项目）：

```powershell
cd C:\Users\johan\Desktop\coding
git clone -b cursor/ai-companion-workflows-7a1d https://github.com/senyan72/campus-recruitment-table.git campus-recruitment-table-ai
cd campus-recruitment-table-ai
powershell -ExecutionPolicy Bypass -File scripts\sync_ai_viewer_to_windows.ps1 -OldProject "C:\Users\johan\Desktop\coding\校招求职表应用程序最新版"
```

脚本会：

1. 拉取 `cursor/ai-companion-workflows-7a1d` 分支  
2. 从旧项目复制 `.env`、`config.json`  
3. 安装依赖并校验 `coach_viewer.py`  

## 方式二：已有克隆目录时更新

```powershell
cd C:\Users\johan\Desktop\coding\campus-recruitment-table-ai
git fetch origin
git checkout cursor/ai-companion-workflows-7a1d
git pull origin cursor/ai-companion-workflows-7a1d
pip install -r requirements.txt
```

## 启动

```powershell
cd C:\Users\johan\Desktop\coding\campus-recruitment-table-ai
.\.venv\Scripts\Activate.ps1
python -m app.main --mode viewer
```

## 验证是否同步成功

```powershell
dir app\ui\coach_viewer.py
findstr "AI 陪伴" app\ui\viewer.py
```

两条都应成功。

## 注意

- **不要**只在旧文件夹里改几个文件；请使用完整新目录或 `git pull` 整仓更新。  
- 双击旧版 `CampusJobsViewer.exe` **不会**自动出现 AI；需用源码启动或重新打包 exe。
