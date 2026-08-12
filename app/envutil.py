"""轻量加载项目根目录 .env（无第三方依赖）。"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path | None = None, *, override: bool = False) -> Path | None:
    """把 KEY=VALUE 写入 os.environ；默认不覆盖已有环境变量。"""
    if path is None:
        # 项目根：app/../
        root = Path(__file__).resolve().parents[1]
        path = root / ".env"
    env_path = Path(path)
    if not env_path.exists():
        return None
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if not key:
            continue
        if not override and key in os.environ and os.environ.get(key) != "":
            continue
        os.environ[key] = val
    return env_path
