"""左栏批次列表：按期间分组 + 通过率细条（任务 46）。

通过率数值来自 ``engine.aggregate`` 的唯一聚合结果：台账 ``save_batch`` 落库的
``pass_rate`` 就是该聚合口径。本模块只用这个值画原生 Canvas 细条，**不另算**
通过率（重算会造成「界面 92%、导出 47 条」这类口径不一致）。
"""
from __future__ import annotations

from typing import Any

from ...engine.aggregate import numbers_for_display
from ..theme import COLORS, FONTS, LAYOUT, PASS_BAR, StubFactory


def draw_pass_bar(canvas: Any, rate: float, width: int | None = None,
                  height: int | None = None) -> dict[str, Any]:
    """在原生 Canvas 上画一条通过率细条。

    ``rate`` 已是聚合结果里的 ``pass_rate``（0..1），此处只做「按比例取长度」，
    不重新计算通过率。底条用分隔线色，前景按高低取成功/警告/错误色，并叠加
    百分比文本；返回绘制参数便于界面与测试断言。
    """
    w = int(width or PASS_BAR["width"])
    h = int(height or PASS_BAR["height"])
    r = max(0.0, min(1.0, float(rate)))
    color = (COLORS["success"] if r >= 0.8
             else COLORS["warning"] if r >= 0.5 else COLORS["error"])
    # 底条
    canvas.create_rectangle(0, 0, w, h, fill=COLORS["divider"], outline=COLORS["divider"])
    # 前景条：长度 = 宽度 × 通过率（数值来自 aggregate，不另算）
    filled = int(round(w * r))
    canvas.create_rectangle(0, 0, filled, h, fill=color, outline=color)
    canvas.create_text(w // 2, h // 2, text=f"{r * 100:.0f}%",
                       fill=COLORS["text"], font=(FONTS["family"][0], 9))
    return {"rate": r, "width": w, "height": h, "filled": filled, "color": color}


class BatchListView:
    """左栏 240px：按期间分组渲染批次，每个批次一条通过率细条。"""

    def __init__(self, parent: Any = None, *, factory: Any = None):
        self.factory = factory or StubFactory()
        self.width = LAYOUT["left"]
        self.container = self.factory.frame(parent, width=self.width)
        self.header = self.factory.label(self.container, text="批次列表（按期间）")
        self.bars: list[Any] = []                 # 已绘制细条的 Canvas
        self._groups: dict[str, list[dict]] = {}
        self._rows: list[dict[str, Any]] = []
        self._display_numbers: dict[str, Any] | None = None
        self.on_select: Any = None

    def set_aggregate(self, agg: dict[str, Any]) -> dict[str, Any]:
        """注入 engine.aggregate 的聚合结果；界面数字一律取自此唯一取数函数。"""
        self._display_numbers = numbers_for_display(agg)
        return self._display_numbers

    def aggregate_pass_rate(self) -> float | None:
        """供界面顶部与本类断言：通过率恒取聚合结果，不另算。"""
        return self._display_numbers["pass_rate"] if self._display_numbers else None

    @staticmethod
    def group_by_period(batches: list[dict]) -> dict[str, list[dict]]:
        """按期间分组，保持输入顺序（台账已按 period DESC 排好）。"""
        groups: dict[str, list[dict]] = {}
        for batch in batches or []:
            period = str((batch or {}).get("period") or "未分期")
            groups.setdefault(period, []).append(batch)
        return groups

    def render(self, batches: list[dict]) -> dict[str, Any]:
        """分组渲染：最新期间在前；每个批次画一条细条、一个可选中的按钮。"""
        self._groups = self.group_by_period(batches)
        self.bars = []
        self._rows = []
        for period in sorted(self._groups, reverse=True):
            group = self.factory.labelframe(self.container, text=f"期间 {period}")
            for batch in self._groups[period]:
                batch = batch or {}
                # rate 直接取台账中来自 aggregate 的口径值，绝不在此处重算
                rate = float(batch.get("pass_rate", 0.0) or 0.0)
                canvas = self.factory.canvas(group, width=PASS_BAR["width"],
                                             height=PASS_BAR["height"])
                draw_pass_bar(canvas, rate)
                self.bars.append(canvas)
                ref = str(batch.get("batch_ref", "") or "")
                button = self.factory.button(
                    group, text=f"{ref}  通过率 {rate * 100:.0f}%",
                    command=(lambda r=ref: self._select(r)))
                button.grid(sticky="we")
                self._rows.append({"period": period, "batch_ref": ref, "rate": rate})
        return {
            "groups": {p: [str((b or {}).get("batch_ref", "")) for b in items]
                       for p, items in self._groups.items()},
            "rows": list(self._rows),
            "bar_count": len(self.bars),
        }

    def set_on_select(self, callback: Any) -> Any:
        self.on_select = callback
        return callback

    def _select(self, batch_ref: str) -> str:
        if callable(self.on_select):
            self.on_select(batch_ref)
        return batch_ref


__all__ = ["BatchListView", "draw_pass_bar"]
