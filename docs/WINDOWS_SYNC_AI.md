# Windows 本机同步 AI Viewer 指南

你的旧文件夹 `校招求职表应用程序最新版` **没有** `app/ui/coach_viewer.py`，因此看不到「AI 陪伴」页签。

---

## 情况一：`git clone` 连不上 GitHub（你当前的问题）

报错类似：`Failed to connect to github.com:443`

### 方案 A：浏览器下载 ZIP（常比 git 更稳）

1. 用浏览器打开（可挂代理/VPN）：
   - https://github.com/senyan72/campus-recruitment-table/archive/refs/heads/cursor/ai-companion-workflows-7a1d.zip
2. 解压到例如：`C:\Users\johan\Desktop\coding\campus-recruitment-table-ai`
3. 进入解压后的文件夹（名字里带 `cursor-ai-companion-workflows-7a1d`）
4. 执行：

```powershell
pip install -r requirements.txt
copy .env.example .env
# 编辑 .env 填入 API Key
python -m app.main --mode viewer
```

### 方案 B：只升级【现有】旧项目（推荐，不用整仓替换）

1. 浏览器下载 ZIP（同方案 A 链接），解压到例如 `C:\Users\johan\Desktop\coding\campus-recruitment-table-ai`
2. **注意**：解压后文件夹名可能是 `campus-recruitment-table-cursor-ai-companion-workflows-7a1d`，先 `cd` 进去再执行：

```powershell
cd "C:\Users\johan\Desktop\coding\campus-recruitment-table-cursor-ai-companion-workflows-7a1d"
powershell -ExecutionPolicy Bypass -File scripts\install_ai_into_project.ps1 -Target "C:\Users\johan\Desktop\coding\校招求职表应用程序最新版"
cd "C:\Users\johan\Desktop\coding\校招求职表应用程序最新版"
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
# 若尚无 .env：copy .env.example .env 并填入 API Key
python -m app.main --mode viewer
```

3. 验证是否成功：

```powershell
findstr /C:"AI 陪伴" app\ui\viewer.py
```

应能看到匹配行；启动 Viewer 后顶部有 **岗位投递 | AI 陪伴** 两个页签。

### 方案 C：配置 Git 代理后再 clone

```powershell
# 若你有本地代理（示例端口 7890，按实际修改）
git config --global http.proxy http://127.0.0.1:7890
git config --global https.proxy http://127.0.0.1:7890
git clone -b cursor/ai-companion-workflows-7a1d https://github.com/senyan72/campus-recruitment-table.git campus-recruitment-table-ai
```

---

## 情况二：网络正常 — 一键脚本


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
