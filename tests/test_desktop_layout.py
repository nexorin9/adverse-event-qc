"""任务 46：三栏宽度符合 token；批次列表按期间分组；通过率细条绘制被调用。

无显示环境：build_app(root=None) 全程用 StubFactory 的 fake widget，绝不建 Tk()。
"""
from __future__ import annotations

import pytest

from ae_qc.desktop.app import build_app
from ae_qc.desktop.theme import LAYOUT, StubFactory
from ae_qc.desktop.views.batch_list import BatchListView, draw_pass_bar
from ae_qc.desktop.views.confirm_panel import ConfirmPanel
from ae_qc.desktop.views.row_table import COLUMNS, RowTableView
from ae_qc.engine.aggregate import aggregate, numbers_for_display


class RecordingFactory(StubFactory):
    """fake widget 工厂：额外收集建出来的 Canvas，便于断言细条绘制。"""

    def __init__(self):
        super().__init__()
        self.canvases: list = []

    def canvas(self, parent=None, **kw):
        widget = super().canvas(parent, **kw)
        self.canvases.append(widget)
        return widget


def _aggregate():
    """一份真实聚合结果（含 1 条 skipped，验证分母口径）。"""
    results = [
        {"rule_id": "R1", "status": "executed", "success": True,
         "unexpected_samples": [], "kind": "set_membership"},
        {"rule_id": "R2", "status": "executed", "success": True,
         "unexpected_samples": [], "kind": "not_null"},
        {"rule_id": "R3", "status": "executed", "success": False,
         "unexpected_samples": [{"row_no": 2, "dept_code": "D01"}], "kind": "time_order"},
        {"rule_id": "R4", "status": "skipped", "success": False, "unexpected_samples": []},
    ]
    return aggregate(results, [])


def test_three_columns_exist_and_widths_follow_tokens():
    app = build_app(root=None)
    assert app["layout"] == {"left": 240, "center": "flex", "right": 360}
    assert LAYOUT == app["layout"]
    assert isinstance(app["left"], BatchListView)
    assert isinstance(app["center"], RowTableView)
    assert isinstance(app["right"], ConfirmPanel)
    assert app["left"].width == 240
    assert app["right"].width == 360
    assert "width" not in app["center"].container.options   # 中栏 flex，无固定宽
    assert app["topbar"]["header"] is not None
    assert app["statusbar"]["label"] is not None
    assert app["root"] is None                               # 未创建 Tk 顶层实例
    assert app["theme"]["tk_applied"] is False


def test_batch_list_groups_by_period_and_draws_pass_bars():
    agg = _aggregate()
    assert numbers_for_display(agg)["pass_rate"] == pytest.approx(2 / 3)
    batches = [
        {"batch_ref": "D01-2026-01", "period": "2026-01", "pass_rate": agg["pass_rate"]},
        {"batch_ref": "D02-2026-01", "period": "2026-01", "pass_rate": 1.0},
        {"batch_ref": "D01-2026-02", "period": "2026-02", "pass_rate": 0.5},
    ]
    factory = RecordingFactory()
    view = BatchListView(factory=factory)
    view.set_aggregate(agg)
    out = view.render(batches)
    assert set(out["groups"]) == {"2026-01", "2026-02"}
    assert out["groups"]["2026-01"] == ["D01-2026-01", "D02-2026-01"]
    assert out["groups"]["2026-02"] == ["D01-2026-02"]
    assert out["bar_count"] == 3
    assert view.aggregate_pass_rate() == pytest.approx(2 / 3)
    # 每个批次的细条都真实调用了 create_rectangle（底条 + 前景条）
    assert len(factory.canvases) == 3
    for canvas in factory.canvases:
        rects = [d for d in canvas.drawn if d[0] == "rectangle"]
        assert len(rects) >= 2
    # 通过率数值来自聚合结果、不另算：渲染行 rate 与输入批次值一致
    assert {r["rate"] for r in out["rows"]} == {agg["pass_rate"], 1.0, 0.5}


def test_draw_pass_bar_uses_given_rate_without_recomputing():
    factory = RecordingFactory()
    canvas = factory.canvas()
    result = draw_pass_bar(canvas, 0.75, width=120, height=8)
    assert result["rate"] == 0.75
    assert result["filled"] == 90
    assert result["color"] == "#B26A00"
    rects = [d for d in canvas.drawn if d[0] == "rectangle"]
    assert len(rects) == 2
    assert rects[1][1][2] == 90                 # 前景长度 = 宽度 × 通过率
    assert draw_pass_bar(factory.canvas(), 2.0)["rate"] == 1.0
    assert draw_pass_bar(factory.canvas(), -1)["rate"] == 0.0


def test_row_table_columns_filter_and_select_event():
    rows = [
        {"dept_code": "D01", "row_no": 2, "rule_id": "AE-SEV", "value": "Z",
         "attribution": "待确认"},
        {"dept_code": "D02", "row_no": 3, "rule_id": "AE-TIME", "value": "倒挂",
         "attribution": "拼写漂移"},
        {"dept_code": "D01", "row_no": 4, "rule_id": "AE-TIME", "value": "倒挂",
         "attribution": ""},
    ]
    view = RowTableView(factory=RecordingFactory())
    assert COLUMNS == ("科室", "行号", "规则", "越界值", "归因")
    assert len(view.set_rows(rows)) == 3
    assert "<<TreeviewSelect>>" in view.table.bindings
    assert len(view.table.inserted) == 3
    assert [r["row_no"] for r in view.filter(dept_code="D01")] == [2, 4]
    assert [r["row_no"] for r in view.filter(rule_id="AE-TIME")] == [3, 4]
    assert [r["row_no"] for r in view.filter(dept_code="D01", rule_id="AE-TIME")] == [4]
    assert len(view.filter()) == 3
    assert view.cell_values(rows[0]) == ("D01", "2", "AE-SEV", "Z", "待确认")
    seen = []
    view.on_select(lambda row, index: seen.append((row["row_no"], index)))
    view.select(0)
    assert seen == [(2, 0)]
