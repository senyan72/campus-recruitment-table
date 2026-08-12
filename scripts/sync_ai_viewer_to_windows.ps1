# Pull/update AI companion code inside your campus jobs project directory.
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\sync_ai_viewer_to_windows.ps1 -ProjectDir "C:\path\to\project"
#
# ASCII-only for Windows PowerShell 5.x compatibility.

param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectDir,
    [string]$LegacyDir = "",
    [string]$Branch = "cursor/ai-companion-workflows-7a1d",
    [string]$RepoUrl = "https://github.com/senyan72/campus-recruitment-table.git"
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$Msg) {
    Write-Host ""
    Write-Host "==> $Msg" -ForegroundColor Cyan
}

function Copy-LegacyConfig {
    param(
        [string]$FromDir,
        [string]$ToDir
    )
    if (-not $FromDir -or -not (Test-Path -LiteralPath $FromDir)) {
        return
    }
    Write-Step "Migrate config: $FromDir -> $ToDir"
    $pairs = @(
        @(".env", ".env"),
        @("config.json", "config.json"),
        @("data\config.json", "data\config.json")
    )
    foreach ($pair in $pairs) {
        $src = Join-Path $FromDir $pair[0]
        $dst = Join-Path $ToDir $pair[1]
        if ((Test-Path -LiteralPath $src) -and -not (Test-Path -LiteralPath $dst)) {
            $parent = Split-Path $dst -Parent
            New-Item -ItemType Directory -Force -Path $parent | Out-Null
            Copy-Item -LiteralPath $src -Destination $dst -Force
            Write-Host "  migrated $($pair[0])"
        }
    }
}

$ProjectDir = (Resolve-Path -LiteralPath $ProjectDir).Path
Write-Step "Project dir: $ProjectDir"
Write-Step "Branch: $Branch"

$gitDir = Join-Path $ProjectDir ".git"
if (Test-Path -LiteralPath $gitDir) {
    Set-Location $ProjectDir
    git fetch origin
    git checkout $Branch
    git pull origin $Branch
} else {
    $hasApp = Test-Path (Join-Path $ProjectDir "app\main.py")
    if ($hasApp) {
        Write-Host "Project exists but is not a git repo. Use install_ai_into_project.ps1 to overlay files." -ForegroundColor Yellow
    } else {
        Write-Step "First-time clone into project dir"
        git clone -b $Branch $RepoUrl $ProjectDir
        Set-Location $ProjectDir
    }
}

if ($LegacyDir -and (Test-Path -LiteralPath $LegacyDir)) {
    Copy-LegacyConfig -FromDir $LegacyDir -ToDir $ProjectDir
} else {
    $parent = Split-Path -Parent $ProjectDir
    foreach ($name in @("campus_recruitment", "campus-recruitment-table-ai")) {
        $legacy = Join-Path $parent $name
        if ((Test-Path -LiteralPath $legacy) -and ($legacy -ne $ProjectDir)) {
            Copy-LegacyConfig -FromDir $legacy -ToDir $ProjectDir
            break
        }
    }
}

if (-not (Test-Path (Join-Path $ProjectDir ".env"))) {
    $example = Join-Path $ProjectDir ".env.example"
    if (Test-Path -LiteralPath $example) {
        Copy-Item -LiteralPath $example -Destination (Join-Path $ProjectDir ".env") -Force
        Write-Host "Tip: edit $ProjectDir\.env and add API keys"
    }
}

$coachViewer = Join-Path $ProjectDir "app\ui\coach_viewer.py"
$viewerPy = Join-Path $ProjectDir "app\ui\viewer.py"
if (-not (Test-Path -LiteralPath $coachViewer)) {
    throw "Missing app\ui\coach_viewer.py. Run install_ai_into_project.ps1 from the extracted ZIP."
}
$hit = Select-String -Path $viewerPy -Pattern "coach_companion" -Quiet
if (-not $hit) {
    $hit = Select-String -Path $viewerPy -Pattern "CoachCompanionFrame" -Quiet
}
if (-not $hit) {
    throw "app\ui\viewer.py does not include AI companion tab. Check branch $Branch."
}
Write-Host "  OK: coach_viewer.py and viewer AI tab ready" -ForegroundColor Green

Write-Step "Install Python dependencies"
$venvDir = Join-Path $ProjectDir "venv"
if (-not (Test-Path $venvDir)) {
    python -m venv $venvDir
}
& (Join-Path $venvDir "Scripts\python.exe") -m pip install -U pip
& (Join-Path $venvDir "Scripts\pip.exe") install -r (Join-Path $ProjectDir "requirements.txt")

Write-Step "Done"
Write-Host @"

Next:
  cd `"$ProjectDir`"
  .\venv\Scripts\Activate.ps1
  python -m app.main --mode viewer

You should see tabs: jobs | AI companion

"@ -ForegroundColor Yellow
