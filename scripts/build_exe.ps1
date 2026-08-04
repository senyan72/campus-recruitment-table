$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path .venv)) {
  python -m venv .venv
}
\.venv\Scripts\python -m pip install -U pip
\.venv\Scripts\pip install -r requirements.txt

@'
from app.main import main
if __name__ == "__main__":
    raise SystemExit(main(["--mode", "viewer"]))
'@ | Set-Content -Encoding UTF8 .\scripts\_entry_viewer.py

@'
from app.main import main
if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main(["--mode", "admin"]))
'@ | Set-Content -Encoding UTF8 .\scripts\_entry_admin.py

Write-Host "==> Building Viewer"
\.venv\Scripts\pyinstaller --noconfirm --clean .\CampusJobsViewer.spec

Write-Host "==> Building Admin"
\.venv\Scripts\pyinstaller --noconfirm --clean .\CampusJobsAdmin.spec

Get-ChildItem -Path .\dist -Recurse -File -Include *.xlsx,*.xls,*.db,*.sqlite,*.sqlite3 | ForEach-Object {
  throw "Private data must not be included in a release package: $($_.FullName)"
}

Write-Host "Done: dist\CampusJobsViewer\ and dist\CampusJobsAdmin\"
