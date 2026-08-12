# 将 GitHub 上带 AI 陪伴的 Viewer 代码同步到本机 Windows
# 用法（PowerShell）：
#   powershell -ExecutionPolicy Bypass -File scripts\sync_ai_viewer_to_windows.ps1
# 或指定旧项目路径（自动复制 .env / config）：
#   powershell -ExecutionPolicy Bypass -File scripts\sync_ai_viewer_to_windows.ps1 -OldProject "C:\Users\johan\Desktop\coding\校招求职表应用程序最新版"

param(
    [string]$OldProject = "",
    [string]$TargetDir = "",
    [string]$Branch = "cursor/ai-companion-workflows-7a1d",
    [string]$RepoUrl = "https://github.com/senyan72/campus-recruitment-table.git"
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$Msg) {
    Write-Host ""
    Write-Host "==> $Msg" -ForegroundColor Cyan
}

if (-not $TargetDir) {
    $coding = Split-Path -Parent (Get-Location)
    if ((Split-Path -Leaf (Get-Location)) -eq "scripts") {
        $coding = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    }
    if ($OldProject) {
        $coding = Split-Path -Parent $OldProject
    }
    $TargetDir = Join-Path $coding "campus-recruitment-table-ai"
}

Write-Step "目标目录: $TargetDir"
Write-Step "分支: $Branch"

# 1) 获取代码
if (Test-Path (Join-Path $TargetDir ".git")) {
    Set-Location $TargetDir
    git fetch origin
    git checkout $Branch
    git pull origin $Branch
} else {
    if (Test-Path $TargetDir) {
        throw "目录已存在但不是 git 仓库: $TargetDir`n请删除后重试，或指定其他 -TargetDir"
    }
    git clone -b $Branch $RepoUrl $TargetDir
    Set-Location $TargetDir
}

# 2) 从旧项目复制配置
if ($OldProject -and (Test-Path -LiteralPath $OldProject)) {
    Write-Step "从旧项目复制配置: $OldProject"
    $oldEnv = Join-Path $OldProject ".env"
    $oldCfg = Join-Path $OldProject "config.json"
    $oldDataCfg = Join-Path $OldProject "data\config.json"
    if (Test-Path -LiteralPath $oldEnv) {
        Copy-Item -LiteralPath $oldEnv -Destination (Join-Path $TargetDir ".env") -Force
        Write-Host "  已复制 .env"
    }
    if (Test-Path -LiteralPath $oldCfg) {
        Copy-Item -LiteralPath $oldCfg -Destination (Join-Path $TargetDir "config.json") -Force
        Write-Host "  已复制 config.json"
    } elseif (Test-Path -LiteralPath $oldDataCfg) {
        Copy-Item -LiteralPath $oldDataCfg -Destination (Join-Path $TargetDir "data\config.json") -Force
        Write-Host "  已复制 data\config.json"
    }
} else {
    if (-not (Test-Path (Join-Path $TargetDir ".env"))) {
        Copy-Item -LiteralPath (Join-Path $TargetDir ".env.example") -Destination (Join-Path $TargetDir ".env") -Force -ErrorAction SilentlyContinue
        Write-Host "提示: 请编辑 $TargetDir\.env 填入 DASHSCOPE_API_KEY / DEEPSEEK_API_KEY"
    }
}

# 3) 校验 AI 文件
$coachViewer = Join-Path $TargetDir "app\ui\coach_viewer.py"
$viewerPy = Join-Path $TargetDir "app\ui\viewer.py"
if (-not (Test-Path -LiteralPath $coachViewer)) {
    throw "同步后仍缺少 app\ui\coach_viewer.py，请检查分支 $Branch"
}
$hit = Select-String -Path $viewerPy -Pattern "AI 陪伴" -SimpleMatch -Quiet
if (-not $hit) {
    throw "app\ui\viewer.py 未包含 AI 陪伴页签，分支可能不对"
}
Write-Host "  OK: coach_viewer.py 与 viewer AI 页签已就绪" -ForegroundColor Green

# 4) 安装依赖
Write-Step "安装 Python 依赖"
if (-not (Test-Path (Join-Path $TargetDir ".venv"))) {
    python -m venv .venv
}
& (Join-Path $TargetDir ".venv\Scripts\python.exe") -m pip install -U pip
& (Join-Path $TargetDir ".venv\Scripts\pip.exe") install -r (Join-Path $TargetDir "requirements.txt")

Write-Step "完成"
Write-Host @"

下一步：
  cd `"$TargetDir`"
  .\.venv\Scripts\Activate.ps1
  python -m app.main --mode viewer

登录后顶部应看到页签：岗位投递 | AI 陪伴

若需打包 exe（含 AI）：
  powershell -ExecutionPolicy Bypass -File scripts\package_viewer_cloud.ps1

"@ -ForegroundColor Yellow
