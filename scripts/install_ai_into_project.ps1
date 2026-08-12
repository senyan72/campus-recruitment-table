# Copy AI companion files into the campus jobs Viewer project (no git clone required).
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\install_ai_into_project.ps1 -Target "C:\path\to\project"
#
# Windows PowerShell 5.x cannot parse UTF-8 .ps1 files with Chinese literals reliably.
# This script uses ASCII-only source; pass Chinese paths via -Target / -LegacyDir on the command line.

param(
    [Parameter(Mandatory = $true)]
    [string]$Target,
    [string]$Source = "",
    [string]$LegacyDir = ""
)

$ErrorActionPreference = "Stop"

if (-not $Source) {
    $scriptDir = $PSScriptRoot
    if (-not $scriptDir) {
        $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    }
    if (-not $scriptDir) {
        throw "Cannot resolve script directory. Pass -Source explicitly."
    }
    $Source = Join-Path $scriptDir ".."
}

$Source = (Resolve-Path $Source).Path
if (-not (Test-Path -LiteralPath $Target)) {
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
}
$Target = (Resolve-Path -LiteralPath $Target).Path

Write-Host "Source (extracted repo): $Source"
Write-Host "Target (your project):   $Target"

function Copy-Tree {
    param(
        [string]$FromRel,
        [string]$Label
    )
    $src = Join-Path $Source $FromRel
    $dst = Join-Path $Target $FromRel
    if (-not (Test-Path -LiteralPath $src)) {
        Write-Warning "Skip (missing source): $FromRel"
        return
    }
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    Copy-Item -Path (Join-Path $src "*") -Destination $dst -Recurse -Force
    Write-Host "Copied: $Label"
}

function Copy-FileIfExists {
    param([string]$RelPath)
    $src = Join-Path $Source $RelPath
    $dst = Join-Path $Target $RelPath
    if (-not (Test-Path -LiteralPath $src)) {
        return
    }
    $parent = Split-Path $dst -Parent
    if ($parent) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    Copy-Item -LiteralPath $src -Destination $dst -Force
    Write-Host "Copied: $RelPath"
}

function Copy-IfMissing {
    param(
        [string]$FromPath,
        [string]$ToPath,
        [string]$Label
    )
    if ((Test-Path -LiteralPath $FromPath) -and -not (Test-Path -LiteralPath $ToPath)) {
        $parent = Split-Path $ToPath -Parent
        if ($parent) {
            New-Item -ItemType Directory -Force -Path $parent | Out-Null
        }
        Copy-Item -LiteralPath $FromPath -Destination $ToPath -Force
        Write-Host "Migrated $Label"
    }
}

function Find-LegacyProjectDir {
    param([string]$Hint, [string]$ParentOfTarget, [string]$CurrentTarget)
    if ($Hint -and (Test-Path -LiteralPath $Hint)) {
        $cfg = Join-Path $Hint "app\config.py"
        if (Test-Path -LiteralPath $cfg) {
            return (Resolve-Path -LiteralPath $Hint).Path
        }
    }
    foreach ($name in @("campus_recruitment", "campus-recruitment-table-ai")) {
        $legacy = Join-Path $ParentOfTarget $name
        $cfg = Join-Path $legacy "app\config.py"
        if ((Test-Path -LiteralPath $legacy) -and ($legacy -ne $CurrentTarget) -and (Test-Path -LiteralPath $cfg)) {
            return (Resolve-Path -LiteralPath $legacy).Path
        }
    }
    if (Test-Path -LiteralPath $ParentOfTarget) {
        Get-ChildItem -LiteralPath $ParentOfTarget -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            if ($_.FullName -eq $CurrentTarget) { return }
            $cfg = Join-Path $_.FullName "app\config.py"
            if (Test-Path -LiteralPath $cfg) {
                return $_.FullName
            }
        }
    }
    return $null
}

function Bootstrap-FromDir {
    param(
        [string]$FromDir,
        [string]$Label
    )
    if (-not $FromDir -or -not (Test-Path -LiteralPath $FromDir)) {
        return
    }
    Write-Host "Bootstrap base app from ${Label}: $FromDir"
    foreach ($rel in @("app", "data", "supabase")) {
        $src = Join-Path $FromDir $rel
        $dst = Join-Path $Target $rel
        if (Test-Path -LiteralPath $src) {
            New-Item -ItemType Directory -Force -Path $dst | Out-Null
            Copy-Item -Path (Join-Path $src "*") -Destination $dst -Recurse -Force
            Write-Host "  copied $rel"
        }
    }
    foreach ($rel in @("requirements.txt", "requirements-ocr.txt", "pytest.ini", ".env.example", "README.md")) {
        $src = Join-Path $FromDir $rel
        $dst = Join-Path $Target $rel
        if ((Test-Path -LiteralPath $src) -and -not (Test-Path -LiteralPath $dst)) {
            Copy-Item -LiteralPath $src -Destination $dst -Force
            Write-Host "  copied $rel"
        }
    }
}

$targetConfig = Join-Path $Target "app\config.py"
if (-not (Test-Path -LiteralPath $targetConfig)) {
    Write-Host "Target is missing app\config.py (incomplete project)." -ForegroundColor Yellow
    $parent = Split-Path -Parent $Target
    $legacyProject = Find-LegacyProjectDir -Hint $LegacyDir -ParentOfTarget $parent -CurrentTarget $Target
    if ($legacyProject) {
        Bootstrap-FromDir -FromDir $legacyProject -Label "legacy project"
    } else {
        Write-Host "No legacy project found; copying full app tree from ZIP source." -ForegroundColor Yellow
        Copy-Tree "app" "app (full)"
        Copy-Tree "data" "data"
        Copy-Tree "supabase" "supabase"
        Copy-FileIfExists "requirements.txt"
        Copy-FileIfExists "requirements-ocr.txt"
        Copy-FileIfExists "pytest.ini"
        Copy-FileIfExists ".env.example"
        Copy-FileIfExists "README.md"
    }
}

# Overlay AI + latest app modules from extracted repo
Copy-Tree "app\coach" "app\coach"
Copy-Tree "app\ui" "app\ui"
Copy-Tree "app\schemas" "app\schemas"
Copy-Tree "tests" "tests"
Copy-Tree "docs" "docs"
Copy-Tree "scripts" "scripts"

foreach ($rel in @(
    "app\envutil.py",
    "app\main.py",
    "app\config.py",
    "app\timeutil.py",
    "app\__init__.py",
    "requirements.txt",
    ".env.example",
    "README.md"
)) {
    Copy-FileIfExists $rel
}

$legacyProject = Find-LegacyProjectDir -Hint $LegacyDir -ParentOfTarget (Split-Path -Parent $Target) -CurrentTarget $Target
if ($legacyProject) {
    Write-Host "Legacy project for config: $legacyProject"
    Copy-IfMissing (Join-Path $legacyProject ".env") (Join-Path $Target ".env") ".env"
    Copy-IfMissing (Join-Path $legacyProject "config.json") (Join-Path $Target "config.json") "config.json"
    Copy-IfMissing (Join-Path $legacyProject "data\config.json") (Join-Path $Target "data\config.json") "data\config.json"
}

$envPath = Join-Path $Target ".env"
if (-not (Test-Path -LiteralPath $envPath)) {
    $example = Join-Path $Source ".env.example"
    if (Test-Path -LiteralPath $example) {
        Copy-Item -LiteralPath $example -Destination $envPath -Force
        Write-Host "Created .env from .env.example (add DASHSCOPE_API_KEY / DEEPSEEK_API_KEY)"
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $Target "app\config.py"))) {
    throw "Install incomplete: app\config.py still missing. Copy your old project folder first, or pass -LegacyDir pointing to a complete install."
}

Write-Host ""
Write-Host "Done. Next steps:"
Write-Host "  cd `"$Target`""
Write-Host "  python -m venv venv"
Write-Host "  .\venv\Scripts\Activate.ps1"
Write-Host "  pip install -r requirements.txt"
Write-Host "  python -m app.main --mode viewer"
Write-Host ""
Write-Host "Verify: dir app\config.py"
Write-Host "Verify: dir app\ui\coach_viewer.py"
