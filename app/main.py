"""入口：viewer / admin / coach（AI 陪伴 Web）。

用法:
  python -m app.main
  python -m app.main --mode viewer
  python -m app.main --mode admin
  python -m app.main --mode coach --host 127.0.0.1 --port 8787
"""

from __future__ import annotations

import argparse
import sys


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="校招投递表桌面应用 + AI 陪伴")
    p.add_argument(
        "--mode",
        choices=("viewer", "admin", "coach"),
        default=None,
        help="运行模式；默认读本机配置，再回退 viewer",
    )
    p.add_argument("--db", default=None, help="可选：指定 SQLite 路径（测试用）")
    p.add_argument("--host", default="127.0.0.1", help="coach 模式监听地址")
    p.add_argument("--port", type=int, default=8787, help="coach 模式端口")
    p.add_argument("--coach-db", default=None, help="可选：CoachDB 路径")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from app.envutil import load_dotenv

    load_dotenv()
    from app.config import load_config

    cfg = load_config()
    mode = args.mode or cfg.get("mode") or "viewer"

    if mode == "coach":
        import uvicorn

        from app.coach.api import create_coach_app

        app = create_coach_app(db_path=args.coach_db)
        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    from app.db.local import LocalDB

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
