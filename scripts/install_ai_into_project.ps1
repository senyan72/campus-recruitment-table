# 将 AI 陪伴相关文件覆盖到「校招求职表应用程序」（无需 git clone）
# 用法：
#   1) 浏览器下载 ZIP：https://github.com/senyan72/campus-recruitment-table/archive/refs/heads/cursor/ai-companion-workflows-7a1d.zip
#   2) 解压到临时目录（任意英文名即可，用完可删）
#   3) 在 PowerShell 中执行（-Target 默认为正式项目目录）：
#      powershell -ExecutionPolicy Bypass -File scripts\install_ai_into_project.ps1
#
# 已废弃：campus_recruitment、campus-recruitment-table-ai 等旁路目录，请勿再使用。

param(
    [string]$Target = "",
    [string]$Source = $PSScriptRoot + "\..",
    [string]$LegacyDir = ""
)

$ErrorActionPreference = "Stop"
$DefaultProjectName = "校招求职表应用程序"

if (-not $Target) {
    $coding = Split-Path -Parent (Resolve-Path $Source).Path
    if ($LegacyDir) {
        $coding = Split-Path -Parent $LegacyDir
    }
    $Target = Join-Path $coding $DefaultProjectName
}

$Source = (Resolve-Path $Source).Path
if (-not (Test-Path -LiteralPath $Target)) {
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
}
$Target = (Resolve-Path $Target).Path

Write-Host "源（解压后的仓库）: $Source"
Write-Host "目标（正式项目）: $Target"

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
    "README.md",
    "scripts\sync_ai_viewer_to_windows.ps1",
    "scripts\install_ai_into_project.ps1"
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

# 从废弃目录迁移 .env / config（若正式目录尚无）
$legacyCandidates = @()
if ($LegacyDir) { $legacyCandidates += $LegacyDir }
$legacyCandidates += @(
    (Join-Path (Split-Path -Parent $Target) "campus_recruitment"),
    (Join-Path (Split-Path -Parent $Target) "campus-recruitment-table-ai"),
    (Join-Path (Split-Path -Parent $Target) "校招求职表应用程序最新版")
)
foreach ($legacy in $legacyCandidates) {
    if (-not $legacy -or -not (Test-Path -LiteralPath $legacy) -or ($legacy -eq $Target)) {
        continue
    }
    $legacyEnv = Join-Path $legacy ".env"
    $targetEnv = Join-Path $Target ".env"
    if ((Test-Path -LiteralPath $legacyEnv) -and -not (Test-Path -LiteralPath $targetEnv)) {
        Copy-Item -LiteralPath $legacyEnv -Destination $targetEnv -Force
        Write-Host "已从废弃目录迁移 .env: $legacy"
    }
    foreach ($pair in @(@("config.json", "config.json"), @("data\config.json", "data\config.json"))) {
        $src = Join-Path $legacy $pair[0]
        $dst = Join-Path $Target $pair[1]
        if ((Test-Path -LiteralPath $src) -and -not (Test-Path -LiteralPath $dst)) {
            $parent = Split-Path $dst -Parent
            New-Item -ItemType Directory -Force -Path $parent | Out-Null
            Copy-Item -LiteralPath $src -Destination $dst -Force
            Write-Host "已从废弃目录迁移 $($pair[0]): $legacy"
        }
    }
    break
}

$envPath = Join-Path $Target ".env"
if (-not (Test-Path $envPath)) {
    Copy-Item (Join-Path $Source ".env.example") $envPath
    Write-Host "已创建 .env（请填入 DASHSCOPE_API_KEY / DEEPSEEK_API_KEY）"
}

Write-Host ""
Write-Host "完成。请在正式项目目录执行："
Write-Host "  cd `"$Target`""
Write-Host "  python -m venv venv"
Write-Host "  .\venv\Scripts\Activate.ps1"
Write-Host "  pip install -r requirements.txt"
Write-Host "  python -m app.main --mode viewer"
Write-Host ""
Write-Host "验证：findstr /C:`"AI 陪伴`" app\ui\viewer.py"
