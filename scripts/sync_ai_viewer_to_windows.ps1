# 在校招求职表应用程序目录内拉取/更新 AI 陪伴代码（唯一正式项目目录）
# 用法（PowerShell）：
#   powershell -ExecutionPolicy Bypass -File scripts\sync_ai_viewer_to_windows.ps1
# 或指定项目路径：
#   powershell -ExecutionPolicy Bypass -File scripts\sync_ai_viewer_to_windows.ps1 -ProjectDir "C:\Users\johan\Desktop\coding\校招求职表应用程序"
#
# 已废弃、请勿再使用：campus_recruitment、campus-recruitment-table-ai、校招求职表应用程序最新版 等旁路目录。
# 若 git 连不上 GitHub，请用 scripts\install_ai_into_project.ps1（见 docs\WINDOWS_SYNC_AI.md）。

param(
    [string]$ProjectDir = "",
    [string]$LegacyDir = "",
    [string]$Branch = "cursor/ai-companion-workflows-7a1d",
    [string]$RepoUrl = "https://github.com/senyan72/campus-recruitment-table.git"
)

$ErrorActionPreference = "Stop"
$DefaultProjectName = "校招求职表应用程序"

function Write-Step([string]$Msg) {
    Write-Host ""
    Write-Host "==> $Msg" -ForegroundColor Cyan
}

function Resolve-ProjectDir {
    param([string]$Hint)
    if ($Hint) {
        return $Hint
    }
    $here = Get-Location
    $leaf = Split-Path -Leaf $here.Path
    if ($leaf -eq $DefaultProjectName) {
        return $here.Path
    }
    if ($leaf -eq "scripts") {
        $parent = Split-Path -Parent $PSScriptRoot
        if ((Split-Path -Leaf $parent) -eq $DefaultProjectName) {
            return $parent
        }
        $coding = Split-Path -Parent $parent
        return Join-Path $coding $DefaultProjectName
    }
  $coding = if ($LegacyDir) { Split-Path -Parent $LegacyDir } else { Split-Path -Parent $here.Path }
    return Join-Path $coding $DefaultProjectName
}

function Copy-LegacyConfig {
    param(
        [string]$FromDir,
        [string]$ToDir
    )
    if (-not $FromDir -or -not (Test-Path -LiteralPath $FromDir)) {
        return
    }
    Write-Step "从旧目录迁移配置: $FromDir -> $ToDir"
    $pairs = @(
        @(".env", ".env"),
        @("config.json", "config.json"),
        @("data\config.json", "data\config.json")
    )
    foreach ($pair in $pairs) {
        $src = Join-Path $FromDir $pair[0]
        $dst = Join-Path $ToDir $pair[1]
        if (Test-Path -LiteralPath $src) {
            $parent = Split-Path $dst -Parent
            New-Item -ItemType Directory -Force -Path $parent | Out-Null
            Copy-Item -LiteralPath $src -Destination $dst -Force
            Write-Host "  已迁移 $($pair[0])"
        }
    }
}

$ProjectDir = Resolve-ProjectDir -Hint $ProjectDir
Write-Step "正式项目目录: $ProjectDir"
Write-Step "分支: $Branch"

if (-not (Test-Path -LiteralPath $ProjectDir)) {
    New-Item -ItemType Directory -Force -Path $ProjectDir | Out-Null
    Write-Host "已创建目录: $ProjectDir"
}

$gitDir = Join-Path $ProjectDir ".git"
if (Test-Path -LiteralPath $gitDir) {
    Set-Location $ProjectDir
    git fetch origin
    git checkout $Branch
    git pull origin $Branch
} else {
    $hasApp = Test-Path (Join-Path $ProjectDir "app\main.py")
    if ($hasApp) {
        Write-Host "目录已有应用代码但不是 git 仓库，跳过 clone（可用 install_ai_into_project.ps1 覆盖更新）" -ForegroundColor Yellow
    } else {
        Write-Step "首次克隆到正式目录"
        git clone -b $Branch $RepoUrl $ProjectDir
        Set-Location $ProjectDir
    }
}

$legacyCandidates = @()
if ($LegacyDir) { $legacyCandidates += $LegacyDir }
$legacyCandidates += @(
    (Join-Path (Split-Path -Parent $ProjectDir) "campus_recruitment"),
    (Join-Path (Split-Path -Parent $ProjectDir) "campus-recruitment-table-ai"),
    (Join-Path (Split-Path -Parent $ProjectDir) "校招求职表应用程序最新版")
)
foreach ($legacy in $legacyCandidates) {
    if ($legacy -and (Test-Path -LiteralPath $legacy) -and ($legacy -ne $ProjectDir)) {
        Copy-LegacyConfig -FromDir $legacy -ToDir $ProjectDir
        break
    }
}

if (-not (Test-Path (Join-Path $ProjectDir ".env"))) {
    $example = Join-Path $ProjectDir ".env.example"
    if (Test-Path -LiteralPath $example) {
        Copy-Item -LiteralPath $example -Destination (Join-Path $ProjectDir ".env") -Force
        Write-Host "提示: 请编辑 $ProjectDir\.env 填入 DASHSCOPE_API_KEY / DEEPSEEK_API_KEY"
    }
}

$coachViewer = Join-Path $ProjectDir "app\ui\coach_viewer.py"
$viewerPy = Join-Path $ProjectDir "app\ui\viewer.py"
if (-not (Test-Path -LiteralPath $coachViewer)) {
    throw @"
缺少 app\ui\coach_viewer.py。
若 git clone 失败，请改用浏览器下载 ZIP 后执行：
  powershell -ExecutionPolicy Bypass -File scripts\install_ai_into_project.ps1 -Target `"$ProjectDir`"
详见 docs\WINDOWS_SYNC_AI.md
"@
}
$hit = Select-String -Path $viewerPy -Pattern "AI 陪伴" -SimpleMatch -Quiet
if (-not $hit) {
    throw "app\ui\viewer.py 未包含 AI 陪伴页签，请确认分支 $Branch 或使用 install_ai_into_project.ps1"
}
Write-Host "  OK: coach_viewer.py 与 viewer AI 页签已就绪" -ForegroundColor Green

Write-Step "安装 Python 依赖"
$venvDir = Join-Path $ProjectDir "venv"
if (-not (Test-Path $venvDir)) {
    python -m venv $venvDir
}
& (Join-Path $venvDir "Scripts\python.exe") -m pip install -U pip
& (Join-Path $venvDir "Scripts\pip.exe") install -r (Join-Path $ProjectDir "requirements.txt")

Write-Step "完成"
Write-Host @"

下一步（均在正式目录内操作）：
  cd `"$ProjectDir`"
  .\venv\Scripts\Activate.ps1
  python -m app.main --mode viewer

登录后顶部应看到页签：岗位投递 | AI 陪伴

若需打包 exe（含 AI）：
  powershell -ExecutionPolicy Bypass -File scripts\package_viewer_cloud.ps1

"@ -ForegroundColor Yellow
