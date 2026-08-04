"""Treeview 首列勾选框：与 selection 同步，支持点格切换与表头全选/取消。"""

from __future__ import annotations

from typing import Any, Sequence

CHECK_OFF = "☐"
CHECK_ON = "☑"
CHECK_COL = "check"


def check_mark(selected: bool) -> str:
    return CHECK_ON if selected else CHECK_OFF


def with_check(values: Sequence[Any], *, selected: bool = False) -> tuple[Any, ...]:
    """在行 values 前插入勾选符号。"""
    return (check_mark(selected), *values)


def sync_tree_checks(tree: Any) -> None:
    """按当前 selection 刷新每行 ☐/☑，并更新表头状态。"""
    selected = set(tree.selection())
    for iid in tree.get_children(""):
        vals = list(tree.item(iid, "values"))
        if not vals:
            continue
        mark = check_mark(iid in selected)
        if vals[0] != mark:
            vals[0] = mark
            tree.item(iid, values=vals)
    kids = tree.get_children("")
    all_on = bool(kids) and all(i in selected for i in kids)
    try:
        tree.heading(CHECK_COL, text=CHECK_ON if all_on else CHECK_OFF)
    except Exception:  # noqa: BLE001
        pass


def toggle_select_all(tree: Any) -> None:
    """表头：已全选则取消，否则全选。"""
    kids = tree.get_children("")
    if not kids:
        return
    selected = set(tree.selection())
    if selected.issuperset(kids):
        tree.selection_remove(*kids)
    else:
        tree.selection_set(kids)
    sync_tree_checks(tree)


def toggle_row_check(tree: Any, iid: str) -> None:
    """切换单行是否在选中集合中（不清除其余选中）。"""
    if iid in tree.selection():
        tree.selection_remove(iid)
    else:
        tree.selection_add(iid)
    sync_tree_checks(tree)


def setup_check_column(tree: Any, *, width: int = 36) -> None:
    """配置首列勾选：窄列、居中、表头点击全选/取消。"""
    tree.heading(CHECK_COL, text=CHECK_OFF, command=lambda: toggle_select_all(tree))
    tree.column(CHECK_COL, width=width, minwidth=28, stretch=False, anchor="center")


def bind_check_column_click(tree: Any, *, on_after: Any = None) -> None:
    """为未自定义 Button-1 的 Treeview 绑定：点首列切换勾选。

    须在其它 Button-1 绑定之前调用。非勾选列返回 None，交给 Treeview 默认多选；
    勾选列 return "break"，避免默认「只选中该行」覆盖切换逻辑。
    """

    def _on_button1(event: Any) -> str | None:
        region = tree.identify_region(event.x, event.y)
        if region == "heading":
            # 首列表头由 heading(command=...) 处理全选/取消
            if on_after and tree.identify_column(event.x) == "#1":
                tree.after(1, on_after)
            return None
        if tree.identify_column(event.x) != "#1":
            return None
        row = tree.identify_row(event.y)
        if not row:
            return "break"
        toggle_row_check(tree, row)
        if on_after:
            on_after()
        return "break"

    tree.bind("<Button-1>", _on_button1)


def is_check_cell(tree: Any, event: Any) -> bool:
    """是否点在数据区首列（勾选列）。"""
    if tree.identify_region(event.x, event.y) == "heading":
        return False
    return tree.identify_column(event.x) == "#1"
