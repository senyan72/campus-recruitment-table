# Package Viewer with cloud-sync anon config and zip to Desktop.
# Usage (repo root):
#   powershell -ExecutionPolicy Bypass -File scripts\package_viewer_cloud.ps1
# Never packs service_role.

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

function Read-JsonFile([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path)) { return $null }
  return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
  $enc = New-Object System.Text.UTF8Encoding $false
  [System.IO.File]::WriteAllText($Path, $Text, $enc)
}

function Get-ViewerSafeCloudConfig {
  # Prefer real user LocalAppData (ignore polluted $env:LOCALAPPDATA from prior tests)
  $realLocal = [Environment]::GetFolderPath("LocalApplicationData")
  $candidates = @(
    (Join-Path $realLocal "campus-jobs\config.json"),
    (Join-Path $env:LOCALAPPDATA "campus-jobs\config.json"),
    (Join-Path (Get-Location) "data\config.json"),
    (Join-Path (Get-Location) "config.json")
  )
  foreach ($p in $candidates) {
    $j = Read-JsonFile $p
    if ($null -eq $j) { continue }
    $url = [string]$j.supabase_url
    $anon = [string]$j.supabase_anon_key
    if ($url -and $anon -and ($anon.Length -gt 80) -and ($anon.StartsWith("eyJ")) -and ($url -notmatch "xxxx\.supabase")) {
      Write-Host "Using cloud credentials from: $p"
      return @{
        supabase_url = $url.Trim()
        supabase_anon_key = $anon.Trim()
        source = $p
      }
    }
  }
  return $null
}

if (-not (Test-Path .venv\Scripts\pyinstaller.exe)) {
  if (-not (Test-Path .venv)) {
    python -m venv .venv
  }
  .\.venv\Scripts\python -m pip install -U pip
  .\.venv\Scripts\pip install -r requirements.txt
  .\.venv\Scripts\pip install pyinstaller
}

$entry = @'
from app.main import main
if __name__ == "__main__":
    raise SystemExit(main(["--mode", "viewer"]))
'@
Write-Utf8NoBom -Path ".\scripts\_entry_viewer.py" -Text $entry

$cloud = Get-ViewerSafeCloudConfig
$hasAnon = $null -ne $cloud

Write-Host "==> PyInstaller Viewer"
.\.venv\Scripts\pyinstaller --noconfirm --clean --windowed --paths . `
  --name CampusJobsViewer `
  --collect-all customtkinter `
  .\scripts\_entry_viewer.py

$dist = Join-Path (Get-Location) "dist\CampusJobsViewer"
$exePath = Join-Path $dist "CampusJobsViewer.exe"
if (-not (Test-Path -LiteralPath $exePath)) {
  throw "Build failed: CampusJobsViewer.exe missing"
}

$privateFiles = Get-ChildItem -Path $dist -Recurse -File -Include *.xlsx,*.xls,*.db,*.sqlite,*.sqlite3
if ($privateFiles) {
  throw "Private seed/database files must not be included in Viewer package: $($privateFiles.FullName -join ', ')"
}

$stamp = Get-Date -Format "yyyyMMdd"
$cfgPath = Join-Path $dist "config.json"
$readmePath = Join-Path $dist "README-viewer.txt"

if ($hasAnon) {
  $cfgObj = [ordered]@{
    mode = "viewer"
    supabase_url = $cloud.supabase_url
    supabase_anon_key = $cloud.supabase_anon_key
    supabase_service_role_key = ""
    sync_interval_minutes = 30
    last_cloud_sync_at = $null
    min_app_version = "0.2.0"
  }
  $json = ($cfgObj | ConvertTo-Json -Depth 5) + "`n"
  Write-Utf8NoBom -Path $cfgPath -Text $json
  Write-Host "Wrote viewer-safe config.json (anon only)"
} else {
  Copy-Item -LiteralPath "data\config.example.json" -Destination $cfgPath -Force
  Write-Host "WARN: no usable supabase_anon_key; shipped example config"
}

$readme = @"
Campus Jobs Viewer (cloud sync)

1. Extract the whole folder (keep _internal next to the exe)
2. Double-click CampusJobsViewer.exe (or start-viewer.bat)
3. First launch copies sibling config.json into:
   %LOCALAPPDATA%\campus-jobs\config.json
4. Click Sync (or wait) to pull cloud jobs

Notes:
- Package contains Supabase anon (read-only) only; never service_role
- Local apply progress stays on this PC
- If sync says unconfigured: edit sibling config.json (url + anon) and restart

Build date: $stamp
"@
Write-Utf8NoBom -Path $readmePath -Text $readme

$launcher = Join-Path $dist "start-viewer.bat"
Write-Utf8NoBom -Path $launcher -Text "@echo off`r`ncd /d `"%~dp0`"`r`nstart `"`" `"%~dp0CampusJobsViewer.exe`"`r`n"

$desktop = [Environment]::GetFolderPath("Desktop")
$env:CAMPUS_JOBS_VIEWER_DIST = $dist
$env:CAMPUS_JOBS_VIEWER_DESKTOP = $desktop
$env:CAMPUS_JOBS_VIEWER_STAMP = $stamp
$env:CAMPUS_JOBS_VIEWER_HAS_ANON = $(if ($hasAnon) { "1" } else { "0" })

Write-Host "==> Zip to Desktop"
.\.venv\Scripts\python scripts\_zip_viewer_desktop.py
if ($LASTEXITCODE -ne 0) { throw "zip helper failed" }

Write-Host "DONE"
Write-Host "HAS_ANON=$hasAnon"
if ($hasAnon) { Write-Host "CONFIG_SOURCE=$($cloud.source)" }
