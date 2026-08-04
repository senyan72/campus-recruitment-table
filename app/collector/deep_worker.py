"""Run deep collection outside the Tk process and stream messages back."""

from __future__ import annotations

import multiprocessing as mp
import queue
import traceback
from pathlib import Path
from typing import Any

Message = tuple[str, Any]


def _emit(message_queue: Any, kind: str, payload: Any, *, critical: bool = False) -> None:
    message: Message = (kind, payload)
    try:
        if critical:
            message_queue.put(message, timeout=5.0)
        else:
            message_queue.put_nowait(message)
    except (queue.Full, ValueError, OSError):
        # Progress is best-effort. SQLite remains the durable run-state source.
        if critical:
            try:
                message_queue.put(message, timeout=1.0)
            except Exception:
                pass


def deep_collect_process_main(
    db_path: str,
    options: dict[str, Any],
    message_queue: Any,
) -> None:
    """Child-process entry point; module scope is required by Windows spawn."""
    try:
        from app.collector.pipeline import run_deep_collect_pipeline
        from app.db.local import LocalDB

        db = LocalDB(Path(db_path))
        result = run_deep_collect_pipeline(
            db,
            **options,
            progress=lambda msg: _emit(message_queue, "progress", str(msg)),
        )
        _emit(message_queue, "result", result, critical=True)
    except BaseException as exc:  # noqa: BLE001 - report child failures to the UI
        _emit(
            message_queue,
            "error",
            {
                "message": str(exc) or exc.__class__.__name__,
                "traceback": traceback.format_exc(limit=20),
            },
            critical=True,
        )
        raise
    finally:
        _emit(message_queue, "finished", None, critical=True)


def start_deep_collect_process(
    db_path: str | Path,
    options: dict[str, Any],
) -> tuple[mp.Process, Any]:
    """Create an isolated worker and a bounded progress/result queue."""
    ctx = mp.get_context("spawn")
    message_queue = ctx.Queue(maxsize=256)
    process = ctx.Process(
        target=deep_collect_process_main,
        args=(str(Path(db_path)), dict(options), message_queue),
        name="campus-jobs-deep-collect",
        daemon=True,
    )
    process.start()
    return process, message_queue


def drain_deep_messages(message_queue: Any, *, limit: int = 100) -> list[Message]:
    """Read currently available messages without blocking the Tk event loop."""
    messages: list[Message] = []
    for _ in range(max(1, int(limit))):
        try:
            item = message_queue.get_nowait()
        except queue.Empty:
            break
        except (EOFError, OSError, ValueError):
            break
        if isinstance(item, tuple) and len(item) == 2:
            messages.append((str(item[0]), item[1]))
    return messages
