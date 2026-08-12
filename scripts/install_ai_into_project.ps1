# Copy AI companion files into the campus jobs Viewer project (no git clone required).
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\install_ai_into_project.ps1 -Target "C:\path\to\project"
#
# Windows PowerShell 5.x cannot parse UTF-8 .ps1 files with Chinese literals reliably.
# This script uses ASCII-only source; pass Chinese paths via -Target / -LegacyDir on the command line.

param(
    [Parameter(Mandatory = $true)]
    [string]$Target,
    [string]$Source = (Join-Path $PSScriptRoot ".."),
    [string]$LegacyDir = ""
)

$ErrorActionPreference = "Stop"

$Source = (Resolve-Path $Source).Path
if (-not (Test-Path -LiteralPath $Target)) {
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
}
$Target = (Resolve-Path -LiteralPath $Target).Path

Write-Host "Source (extracted repo): $Source"
Write-Host "Target (your project):   $Target"

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
    if (-not (Test-Path -LiteralPath $src)) {
        Write-Warning "Skip (missing source): $d"
        continue
    }
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    Copy-Item -Path (Join-Path $src "*") -Destination $dst -Recurse -Force
    Write-Host "Copied: $d"
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
    if (Test-Path -LiteralPath $src) {
        $parent = Split-Path $dst -Parent
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        Copy-Item -LiteralPath $src -Destination $dst -Force
        Write-Host "Copied: $f"
    }
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
        Write-Host "Migrated $Label from legacy dir"
    }
}

if ($LegacyDir -and (Test-Path -LiteralPath $LegacyDir)) {
    Write-Host "Legacy dir: $LegacyDir"
    Copy-IfMissing (Join-Path $LegacyDir ".env") (Join-Path $Target ".env") ".env"
    Copy-IfMissing (Join-Path $LegacyDir "config.json") (Join-Path $Target "config.json") "config.json"
    Copy-IfMissing (Join-Path $LegacyDir "data\config.json") (Join-Path $Target "data\config.json") "data\config.json"
} else {
    $parent = Split-Path -Parent $Target
    foreach ($name in @("campus_recruitment", "campus-recruitment-table-ai")) {
        $legacy = Join-Path $parent $name
        if ((Test-Path -LiteralPath $legacy) -and ($legacy -ne $Target)) {
            Write-Host "Legacy dir: $legacy"
            Copy-IfMissing (Join-Path $legacy ".env") (Join-Path $Target ".env") ".env"
            Copy-IfMissing (Join-Path $legacy "config.json") (Join-Path $Target "config.json") "config.json"
            Copy-IfMissing (Join-Path $legacy "data\config.json") (Join-Path $Target "data\config.json") "data\config.json"
            break
        }
    }
}

$envPath = Join-Path $Target ".env"
if (-not (Test-Path -LiteralPath $envPath)) {
    $example = Join-Path $Source ".env.example"
    if (Test-Path -LiteralPath $example) {
        Copy-Item -LiteralPath $example -Destination $envPath -Force
        Write-Host "Created .env from .env.example (add DASHSCOPE_API_KEY / DEEPSEEK_API_KEY)"
    }
}

Write-Host ""
Write-Host "Done. Next steps:"
Write-Host "  cd `"$Target`""
Write-Host "  python -m venv venv"
Write-Host "  .\venv\Scripts\Activate.ps1"
Write-Host "  pip install -r requirements.txt"
Write-Host "  python -m app.main --mode viewer"
Write-Host ""
Write-Host "Verify: dir app\ui\coach_viewer.py"
