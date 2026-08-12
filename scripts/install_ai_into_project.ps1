# 将 AI 陪伴相关文件覆盖到「已有」校招求职表项目（无需 git clone）
# 用法：
#   1) 浏览器下载 ZIP：https://github.com/senyan72/campus-recruitment-table/archive/refs/heads/cursor/ai-companion-workflows-7a1d.zip
#   2) 解压到任意目录，例如 C:\Users\johan\Desktop\coding\campus-recruitment-table-ai
#   3) 在 PowerShell 中执行：
#      powershell -ExecutionPolicy Bypass -File "C:\...\campus-recruitment-table-ai\scripts\install_ai_into_project.ps1" -Target "C:\Users\johan\Desktop\coding\校招求职表应用程序最新版"

param(
    [Parameter(Mandatory = $true)]
    [string]$Target,
    [string]$Source = $PSScriptRoot + "\.."
)

$ErrorActionPreference = "Stop"
$Source = (Resolve-Path $Source).Path
$Target = (Resolve-Path $Target).Path

Write-Host "源（解压后的仓库）: $Source"
Write-Host "目标（你的旧项目）: $Target"

$dirs = @(
    "app\coach",
    "app\ui",
    "app\schemas",
    "tests",
    "docs"
)
foreach ($d in $dirs) {
    $src = Join-Path $Source $d
    $dst = Join-Path $Target $d
    if (-not (Test-Path $src)) {
        Write-Warning "跳过（源不存在）: $d"
        continue
    }
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    Copy-Item -Path (Join-Path $src "*") -Destination $dst -Recurse -Force
    Write-Host "已复制: $d"
}

$files = @(
    "app\envutil.py",
    "app\main.py",
    "requirements.txt",
    ".env.example",
    "README.md"
)
foreach ($f in $files) {
    $src = Join-Path $Source $f
    $dst = Join-Path $Target $f
    if (Test-Path $src) {
        $parent = Split-Path $dst -Parent
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        Copy-Item $src $dst -Force
        Write-Host "已复制: $f"
    }
}

$envPath = Join-Path $Target ".env"
if (-not (Test-Path $envPath)) {
    Copy-Item (Join-Path $Source ".env.example") $envPath
    Write-Host "已创建 .env（请填入 DASHSCOPE_API_KEY / DEEPSEEK_API_KEY）"
}

Write-Host ""
Write-Host "完成。请在本项目目录执行："
Write-Host "  python -m venv venv"
Write-Host "  .\venv\Scripts\Activate.ps1"
Write-Host "  pip install -r requirements.txt"
Write-Host "  python -m app.main --mode viewer"
Write-Host ""
Write-Host "验证：findstr /C:`"AI 陪伴`" app\ui\viewer.py"
