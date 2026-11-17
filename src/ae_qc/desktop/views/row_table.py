"""中栏越界行表（任务 46）：科室/行号/规则/越界值/归因 + 过滤 + 行选中事件。

数据来自 ``DesktopDataSource.unexpected_rows`` → ``ledger.queries``；本模块只做
展示层映射与交互，不自行拼查询、不自行算通过率。
"""
from __future__ import annotations

from typing import Any

from ..theme import ROW_HEIGHT, StubFactory

COLUMNS = ("科室", "行号", "规则", "越界值", "归因")


class RowTableView:
    """中栏 flex：可过滤的越界行表；选中行触发回调供右栏确认面板联动。"""

    def __init__(self, parent: Any = None, *, factory: Any = None):
        self.factory = factory or StubFactory()
        self.container = self.factory.frame(parent)   # flex 宽度，随窗口伸缩
        self.table = self.factory.treeview(self.container, columns=COLUMNS,
                                           show="headings", height=ROW_HEIGHT)
        for col in COLUMNS:
            self.table.heading(col, text=col)
            self.table.column(col, width=96)
        self._all_rows: list[dict] = []
        self._visible_rows: list[dict] = []
        self._select_callbacks: list[Any] = []
        # 行选中事件：绑定到表格，事件到来时触发回调（fake widget 亦可驱动）
        self.table.bind("<<TreeviewSelect>>", self._on_tree_select)

    @staticmethod
    def cell_values(row: dict) -> tuple[str, ...]:
        """把台账行级证据映射为五列展示值（脱敏后的字段，不含患者明细）。"""
        row = row or {}
        dept = row.get("dept_code") or row.get("_dept_code") or ""
        no = row.get("row_no")
        if no is None:
            no = row.get("_row_no", "")
        value = row.get("value")
        if value is None:
            value = row.get("present_value") or row.get("observed_value") or ""
        attribution = (row.get("attribution") or row.get("attribution_draft")
                       or row.get("cause_type") or "")
        return (str(dept), str(no), str(row.get("rule_id", "") or ""),
                str(value), str(attribution))

    def set_rows(self, rows: list[dict]) -> list[dict]:
        """装入全量越界行并重绘（展示条数 == 全量行级明细条数）。"""
        self._all_rows = list(rows or [])
        self._visible_rows = list(self._all_rows)
        self._render()
        return list(self._visible_rows)

    def filter(self, dept_code: str | None = None,
               rule_id: str | None = None) -> list[dict]:
        """按科室与规则过滤；两者均空时回到全量。"""
        def match(row: dict) -> bool:
            row = row or {}
            dept_ok = (not dept_code
                       or str(row.get("dept_code") or row.get("_dept_code") or "") == dept_code)
            rule_ok = not rule_id or str(row.get("rule_id", "") or "") == rule_id
            return dept_ok and rule_ok

        self._visible_rows = [r for r in self._all_rows if match(r)]
        self._render()
        return list(self._visible_rows)

    def _render(self) -> None:
        self.table.delete(*self.table.get_children())
        for index, row in enumerate(self._visible_rows):
            self.table.insert("", "end", iid=str(index), values=self.cell_values(row))

    def on_select(self, callback: Any) -> Any:
        """注册行选中回调：选中时收到 ``(row, index)``。"""
        self._select_callbacks.append(callback)
        return callback

    def select(self, index: int) -> dict:
        """程序化选中一行并触发所有选中回调（供无显示环境驱动逻辑）。"""
        row = self._visible_rows[index]
        self.table.selection_set(str(index))
        for callback in list(self._select_callbacks):
            callback(row, index)
        return row

    def _on_tree_select(self, event: Any = None) -> Any:
        sel = self.table.selection()
        if not sel:
            return None
        return self.select(int(sel[0]))


__all__ = ["RowTableView", "COLUMNS"]
