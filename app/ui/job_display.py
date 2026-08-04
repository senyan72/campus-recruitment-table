"""岗位显示：已审核通过、可供推送/已推送的正常岗位。"""

from __future__ import annotations

from typing import Any, Callable

from app.db.local import LocalDB
from app.ui.job_review import (
    JobReviewPanel,
    cloud_upload_progress,
    _DISPLAY_COLS,
)

LogCb = Callable[[str], None]
ChangedCb = Callable[[], None]
PushCloudCb = Callable[[list[str] | None], None]

__all__ = [
    "JobDisplayPanel",
    "cloud_upload_progress",
    "_DISPLAY_COLS",
]


class JobDisplayPanel(JobReviewPanel):
    """岗位显示页：mode=display，含「云端上传进度」与「推送云端」。"""

    def __init__(
        self,
        master: Any,
        db: LocalDB,
        *,
        on_log: LogCb | None = None,
        on_changed: ChangedCb | None = None,
        on_push_cloud: PushCloudCb | None = None,
    ) -> None:
        super().__init__(
            master,
            db,
            on_log=on_log,
            on_changed=on_changed,
            on_push_cloud=on_push_cloud,
            mode="display",
        )
