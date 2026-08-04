"""Treeview 列表排序：公司名归一化聚拢 + 表头点击升/降序。"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from app.db.local import normalize_company_name

SortKeyFn = Callable[[Any], Any]
RowGetter = Callable[[str], Any]  # iid -> row dict / payload


def company_cluster_key(name: str | None) -> tuple[str, str]:
    """
    公司聚拢键：(前缀, 归一化名)。
    同名/去后缀后相同的公司会挨在一起；前缀相同的相近名称也会靠近（轻量，不做模糊聚类）。
    """
    norm = normalize_company_name(name or "") or ""
    prefix = norm[:6]
    return (prefix, norm)


def heading_label(base: str, *, active: bool, ascending: bool) -> str:
    if not active:
        return base
    return f"{base} {'↑' if ascending else '↓'}"


class TreeSortController:
    """
    管理当前排序列与方向；提供 sort_items / bind_heading。

    sortable: {col_key: (表头基础文案, key_fn)}
    默认列一般为 company：按公司聚拢后再按 title / updated。
    """

    def __init__(
        self,
        *,
        sortable: dict[str, tuple[str, SortKeyFn]],
        default_col: str = "company",
        default_ascending: bool = True,
        tie_breakers: Sequence[SortKeyFn] | None = None,
    ) -> None:
        self.sortable = dict(sortable)
        self.default_col = default_col if default_col in self.sortable else next(iter(self.sortable))
        self.col = self.default_col
        self.ascending = default_ascending
        self.tie_breakers = list(tie_breakers or [])
        self._base_titles = {k: v[0] for k, v in self.sortable.items()}

    def toggle(self, col: str) -> None:
        if col not in self.sortable:
            return
        if self.col == col:
            self.ascending = not self.ascending
        else:
            self.col = col
            # 公司默认升序聚拢；日期默认降序（新→旧）更符合直觉
            self.ascending = col not in {"updated_at", "created_at", "time"}

    def sort_key_for(self, item: Any) -> tuple:
        primary_fn = self.sortable[self.col][1]
        primary = primary_fn(item)
        extras = tuple(fn(item) for fn in self.tie_breakers)
        return (primary, *extras)

    def sort_items(self, items: list[Any]) -> list[Any]:
        keyed = [(self.sort_key_for(it), i, it) for i, it in enumerate(items)]
        keyed.sort(key=lambda t: (t[0], t[1]), reverse=not self.ascending)
        return [t[2] for t in keyed]

    def apply_heading_labels(
        self,
        tree: Any,
        *,
        all_cols: Sequence[tuple[str, str]] | None = None,
        on_sorted: Callable[[], None] | None = None,
    ) -> None:
        """更新可排序列表头文案（含 ↑↓）；若给 on_sorted 则同时保持 heading command。"""
        cols = list(all_cols) if all_cols else [(k, t) for k, t in self._base_titles.items()]
        for key, base in cols:
            if key in self.sortable:
                label = heading_label(
                    self._base_titles.get(key, base),
                    active=self.col == key,
                    ascending=self.ascending,
                )
                if on_sorted is not None:
                    tree.heading(
                        key,
                        text=label,
                        command=lambda c=key: self._on_heading(c, on_sorted),
                    )
                else:
                    # 保留已有 command，只改文案
                    tree.heading(key, text=label)
            else:
                tree.heading(key, text=base)

    def bind_headings(
        self,
        tree: Any,
        *,
        on_sorted: Callable[[], None],
        column_widths: Sequence[tuple[str, int, str]] | None = None,
    ) -> None:
        """
        为可排序列设置 heading command。
        column_widths: 与 job_review 一致的 (key, width, title) 列表时，一并设置 column。
        """
        if column_widths:
            for key, width, title in column_widths:
                if key in self.sortable:
                    self._base_titles[key] = title
                tree.column(key, width=width, stretch=True)
                if key in self.sortable:
                    tree.heading(
                        key,
                        text=heading_label(
                            title, active=self.col == key, ascending=self.ascending
                        ),
                        command=lambda c=key: self._on_heading(c, on_sorted),
                    )
                else:
                    tree.heading(key, text=title)
            return
        for key, (title, _fn) in self.sortable.items():
            tree.heading(
                key,
                text=heading_label(title, active=self.col == key, ascending=self.ascending),
                command=lambda c=key: self._on_heading(c, on_sorted),
            )

    def _on_heading(self, col: str, on_sorted: Callable[[], None]) -> None:
        self.toggle(col)
        on_sorted()


def make_company_title_updated_sort(
    *,
    company_key: str = "company",
    title_key: str = "title",
    updated_key: str = "updated_at",
    company_label: str = "公司",
    title_label: str = "岗位名称",
    updated_label: str = "岗位发布时间",
) -> TreeSortController:
    """岗位类列表：公司 / 岗位名 / 岗位发布时间（open_at）。"""

    def _company(item: Any) -> tuple[str, str]:
        if isinstance(item, dict):
            return company_cluster_key(item.get(company_key) or item.get("company_name"))
        return company_cluster_key(str(item or ""))

    def _title(item: Any) -> str:
        if isinstance(item, dict):
            return str(item.get(title_key) or "").lower()
        return ""

    def _updated(item: Any) -> str:
        if not isinstance(item, dict):
            return ""
        # 页面发布/更新日优先；不用本机 updated_at/created_at
        if updated_key and updated_key not in ("updated_at", "created_at"):
            val = item.get(updated_key)
            if val:
                return str(val)
        for key in ("open_at", "list_updated_at", "published_at"):
            val = item.get(key)
            if val:
                return str(val)
        return ""

    return TreeSortController(
        sortable={
            "company": (company_label, _company),
            "title": (title_label, _title),
            "updated_at": (updated_label, _updated),
        },
        default_col="company",
        default_ascending=True,
        tie_breakers=[_company, _title, _updated],
    )
