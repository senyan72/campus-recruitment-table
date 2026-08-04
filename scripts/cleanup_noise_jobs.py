"""本地清理噪声/重复/关停·内推岗位（软删）。用法: python -m scripts.cleanup_noise_jobs"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.collector.cleanup import cleanup_noise_and_duplicate_jobs
from app.db.local import LocalDB


def main() -> None:
    db = LocalDB()
    result = cleanup_noise_and_duplicate_jobs(db)
    print(result.get("text") or result)
    print(f"deleted_ids={len(result.get('deleted_ids') or [])}")


if __name__ == "__main__":
    main()
