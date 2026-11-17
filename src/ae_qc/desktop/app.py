"""主窗口装配（任务 46）：顶栏 + 三栏 + 状态栏。

* 三栏宽度走设计 token：左 240 / 中 flex / 右 360；
* ``build_app(root=None, data_source=None)`` 返回组件引用 dict，便于无显示环境测试；
* **绝不创建 Tk() 顶层实例、绝不强制 mainloop**：root=None 或 fake root 时用
  StubFactory 装配（纯 Python widget 替身），真实 Tk root 时才用 ttk 工厂。
  两条路径共用同一套视图代码，视图逻辑可在无 DISPLAY 下被真实驱动。
"""
from __future__ import annotations

from typing import Any

from . import states
from .data_source import DesktopDataSource
from .theme import LAYOUT, SPACING, StubFactory, apply_theme
from .views.batch_list import BatchListView
from .views.confirm_panel import ConfirmPanel
from .views.row_table import RowTableView

WINDOW: dict[str, int] = {"width": 1180, "height": 720,
                          "min_width": 960, "min_height": 600}
APP_TITLE = "不良事件上报数据质检 · 质控工作台"


class _NullDataSource:
    """无数据源装配时的 Null Object：界面骨架照常工作，显示空态、不连台账。

    不是产品桩——所有取数方法都返回明确的空结果，让 ``refresh()`` 在无显示
    环境下也能真实跑一遍渲染逻辑。
    """

    def list_batches(self) -> list[dict]:
        return []

    def unexpected_rows(self, *args: Any, **kwargs: Any) -> list[dict]:
        return []

    def pending_count(self) -> int:
        return 0

    def pending_writeback_count(self) -> int:
        return 0

    def status_numbers(self) -> dict[str, int]:
        return {"pending_count": 0, "pending_writeback_count": 0}

    def batch_stats(self, batch_ref: str) -> dict:
        return {}


class _RealFactory:
    """真实 Tk root 的 ttk 工厂；tkinter 只在实例化时惰性导入。"""

    def __init__(self, root: Any):
        import tkinter as tk
        import tkinter.ttk as ttk
        self._tk = tk
        self._ttk = ttk
        self._root = root

    @staticmethod
    def _pick(kw: dict, allowed: set[str]) -> dict:
        return {k: v for k, v in kw.items() if k in allowed}

    def frame(self, parent: Any = None, **kw: Any) -> Any:
        return self._ttk.Frame(parent or self._root,
                               **self._pick(kw, {"width", "height", "padding", "style",
                                                 "relief", "borderwidth"}))

    def labelframe(self, parent: Any = None, **kw: Any) -> Any:
        return self._ttk.LabelFrame(parent or self._root,
                                    **self._pick(kw, {"text", "width", "height", "padding",
                                                      "style", "labelanchor"}))

    def label(self, parent: Any = None, **kw: Any) -> Any:
        return self._ttk.Label(parent or self._root,
                               **self._pick(kw, {"text", "style", "anchor", "width",
                                                 "justify", "font", "wraplength"}))

    def button(self, parent: Any = None, **kw: Any) -> Any:
        return self._ttk.Button(parent or self._root,
                                **self._pick(kw, {"text", "command", "style", "width"}))

    def entry(self, parent: Any = None, **kw: Any) -> Any:
        return self._ttk.Entry(parent or self._root,
                               **self._pick(kw, {"width", "style", "textvariable"}))

    def canvas(self, parent: Any = None, **kw: Any) -> Any:
        # 原生 Canvas 画通过率细条（ttk 无 Canvas）
        return self._tk.Canvas(parent or self._root,
                               **self._pick(kw, {"width", "height", "bg",
                                                 "highlightthickness", "relief", "bd"}))

    def treeview(self, parent: Any = None, **kw: Any) -> Any:
        return self._ttk.Treeview(parent or self._root,
                                  **self._pick(kw, {"columns", "show", "height",
                                                    "style", "selectmode"}))


def _looks_like_tk(root: Any) -> bool:
    """真实 Tk root 带 ``tk`` 对象且有 call；fake / stub 不具备，绝不误判。"""
    tk_obj = getattr(root, "tk", None)
    return tk_obj is not None and hasattr(tk_obj, "call") and not callable(tk_obj)


def _select_factory(root: Any) -> Any:
    if root is None or not _looks_like_tk(root):
        return StubFactory()          # 无显示 / 无 root：纯 Python 装配
    try:
        return _RealFactory(root)
    except Exception:
        return StubFactory()


def build_app(root: Any = None, data_source: DesktopDataSource | None = None) -> dict[str, Any]:
    """装配主窗口并返回组件引用 dict；不创建 Tk()、不 mainloop。

    返回的 ``refresh`` 是可调用对象：从数据源取数 → 渲染三栏 → 更新状态栏与
    待写红条，无显示环境下也能被真实调用（配合 StubFactory）。
    """
    factory = _select_factory(root)
    theme_result = apply_theme(root)
    ds: Any = data_source if data_source is not None else _NullDataSource()

    if root is not None:
        try:
            root.title(APP_TITLE)
            root.geometry(f"{WINDOW['width']}x{WINDOW['height']}")
            root.minsize(WINDOW["min_width"], WINDOW["min_height"])
            root.rowconfigure(1, weight=1)
            root.columnconfigure(0, weight=1)
        except Exception:
            pass  # fake root 无这些方法（或不支持），装配继续

    # ---- 顶栏 ----
    topbar = factory.frame(root)
    topbar.grid(row=0, column=0, sticky="we", padx=SPACING["sm"], pady=SPACING["xs"])
    header = factory.label(topbar, text=APP_TITLE)
    header.grid(row=0, column=0, sticky="w")
    banner = factory.label(topbar, text=states.pending_writeback_banner(0))
    banner.grid(row=0, column=1, sticky="w", padx=SPACING["md"])
    refresh_button = factory.button(topbar, text="刷新")
    refresh_button.grid(row=0, column=2, sticky="e")

    # ---- 三栏：240 / flex / 360，间距走 8px 栅格 ----
    content = factory.frame(root)
    content.grid(row=1, column=0, sticky="nsew")
    content.columnconfigure(0, minsize=LAYOUT["left"], weight=0)
    content.columnconfigure(1, weight=1)                  # flex 中栏
    content.columnconfigure(2, minsize=LAYOUT["right"], weight=0)
    content.rowconfigure(0, weight=1)

    left = BatchListView(content, factory=factory)
    center = RowTableView(content, factory=factory)
    right = ConfirmPanel(content, factory=factory,
                         data_source=ds if hasattr(ds, "queries") else None)
    left.container.grid(row=0, column=0, sticky="ns", padx=SPACING["xs"],
                        pady=SPACING["xs"])
    center.container.grid(row=0, column=1, sticky="nsew", padx=SPACING["xs"],
                          pady=SPACING["xs"])
    right.container.grid(row=0, column=2, sticky="ns", padx=SPACING["xs"],
                         pady=SPACING["xs"])
    center.on_select(lambda row, index: right.set_row(row))  # 行选中 → 确认面板联动

    # ---- 状态栏 ----
    statusbar = factory.frame(root)
    statusbar.grid(row=2, column=0, sticky="we", padx=SPACING["sm"],
                   pady=SPACING["xs"])
    status_label = factory.label(statusbar, text="待确认 0 条 · 待写队列 0 条")
    status_label.grid(row=0, column=0, sticky="w")

    def refresh() -> dict[str, Any]:
        batches = ds.list_batches()
        rows = ds.unexpected_rows()
        numbers = (ds.status_numbers() if hasattr(ds, "status_numbers")
                   else {"pending_count": ds.pending_count(),
                         "pending_writeback_count": ds.pending_writeback_count()})
        left.render(batches)
        center.set_rows(rows)
        banner.configure(text=states.pending_writeback_banner(numbers["pending_writeback_count"]))
        status_label.configure(
            text=f"待确认 {numbers['pending_count']} 条 · "
                 f"待写队列 {numbers['pending_writeback_count']} 条")
        return {"batches": len(batches), "rows": len(rows), **numbers}

    refresh_button.configure(command=refresh)

    return {
        "root": root, "factory": factory, "theme": theme_result,
        "window": dict(WINDOW), "layout": dict(LAYOUT),
        "topbar": {"frame": topbar, "header": header, "banner": banner,
                   "refresh_button": refresh_button},
        "content": content,
        "left": left, "center": center, "right": right,
        "statusbar": {"frame": statusbar, "label": status_label},
        "data_source": ds, "refresh": refresh,
    }


__all__ = ["build_app", "WINDOW", "APP_TITLE"]
