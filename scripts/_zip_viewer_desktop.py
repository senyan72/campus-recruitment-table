"""Zip dist/CampusJobsViewer to Desktop with Chinese filename (UTF-8 safe)."""

from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from pathlib import Path


def main() -> int:
    dist = Path(os.environ["CAMPUS_JOBS_VIEWER_DIST"])
    desktop = Path(os.environ["CAMPUS_JOBS_VIEWER_DESKTOP"])
    stamp = os.environ.get("CAMPUS_JOBS_VIEWER_STAMP") or ""
    has_anon = os.environ.get("CAMPUS_JOBS_VIEWER_HAS_ANON") == "1"

    if has_anon:
        folder_name = "校招投递表-用户端"
        zip_name = f"校招投递表-用户端-云同步-{stamp}.zip"
        zip_name_en = f"campus-jobs-viewer-cloud-{stamp}.zip"
    else:
        folder_name = "校招投递表-用户端"
        zip_name = f"校招投递表-用户端-待填anon-{stamp}.zip"
        zip_name_en = f"campus-jobs-viewer-need-anon-{stamp}.zip"

    with tempfile.TemporaryDirectory(prefix="cj-viewer-zip-") as tmp:
        stage_root = Path(tmp)
        stage = stage_root / folder_name
        shutil.copytree(dist, stage)
        for name in (zip_name, zip_name_en):
            out = desktop / name
            if out.exists():
                out.unlink()
            with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for path in stage.rglob("*"):
                    if path.is_file():
                        zf.write(path, arcname=str(path.relative_to(stage_root)))
            print(f"ZIP={out}")
            print(f"SIZE={out.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
