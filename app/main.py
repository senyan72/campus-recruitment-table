"""双入口：viewer / admin。

用法:
  python -m app.main
  python -m app.main --mode viewer
  python -m app.main --mode admin
"""

from __future__ import annotations

import argparse
import sys


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="校招投递表桌面应用")
    p.add_argument(
        "--mode",
        choices=("viewer", "admin"),
        default=None,
        help="运行模式；默认读本机配置，再回退 viewer",
    )
    p.add_argument("--db", default=None, help="可选：指定 SQLite 路径（测试用）")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from app.config import load_config
    from app.db.local import LocalDB

    cfg = load_config()
    mode = args.mode or cfg.get("mode") or "viewer"
    db = LocalDB(args.db) if args.db else LocalDB()

    if mode == "admin":
        from app.ui.admin import run_admin

        run_admin(db)
    else:
        from app.ui.viewer import run_viewer

        run_viewer(db)
    return 0


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    raise SystemExit(main(sys.argv[1:]))
