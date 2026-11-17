"""桌面形态设计 token 与基础样式（任务 45）。

观感对标医院中后台 B 端工具：表格优先、信息密度高，不做消费级大卡片风。
只使用 ttk 样式 / 颜色 / 字体 / 间距 / 原生 Canvas 能做到的效果——不画动画
图表、WebGL 等现有依赖做不到的东西。

无显示环境纪律
--------------
* 本模块顶层不 import tkinter，也不创建任何 Tk() 顶层实例；ttk 只在
  ``apply_theme`` 函数内部惰性导入。
* ``StubWidget`` / ``StubFactory`` 提供 duck typing 的空实现（configure / grid /
  bind / create_rectangle ...），让视图逻辑能在无 DISPLAY 的 pytest 里被驱动。
  真实窗口由 ``app.build_app`` 注入 ttk 工厂；两条路径共用同一套视图代码。
"""
from __future__ import annotations

from typing import Any

# ---- 设计 token ----
COLORS: dict[str, str] = {
    "base": "#F5F7FA",           # 底色
    "panel": "#FFFFFF",          # 面板
    "primary": "#1F5FA9",        # 主色
    "success": "#2E7D4F",        # 成功
    "warning": "#B26A00",        # 警告
    "error": "#C0392B",          # 错误
    "text": "#1F2933",           # 主文字
    "text_secondary": "#5B6770", # 次文字
    "divider": "#E3E8EF",        # 分隔线
}

# 8px 间距栅格
SPACING: dict[str, int] = {"xs": 8, "sm": 16, "md": 24, "lg": 32}
GRID = (8, 16, 24, 32)

FONTS: dict[str, Any] = {
    "family": ("PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC"),
    "page_header": 15,   # 页头
    "panel_title": 13,   # 面板标题
    "table_body": 12,    # 表格正文
    "caption": 11,       # 辅助说明
}

ROW_HEIGHT = 32

# 三栏宽度：左 240 / 中 flex / 右 360（token 化，布局据此装配）
LAYOUT: dict[str, Any] = {"left": 240, "center": "flex", "right": 360}

PASS_BAR: dict[str, int] = {"width": 120, "height": 8}

TOKENS: dict[str, Any] = {
    "colors": COLORS,
    "spacing": SPACING,
    "grid": GRID,
    "fonts": FONTS,
    "row_height": ROW_HEIGHT,
    "layout": LAYOUT,
    "pass_bar": PASS_BAR,
}

# ---- 组件四态 ----
COMPONENT_STATES = ("hover", "active", "disabled", "loading")

STATE_STYLES: dict[str, dict[str, Any]] = {
    "hover": {
        "style": "Hover.TButton", "cursor": "hand2", "background": "#2C6FBE",
        "foreground": "#FFFFFF", "relief": "flat", "state": "normal",
        "_interactive": True,
    },
    "active": {
        "style": "Active.TButton", "cursor": "hand2", "background": COLORS["primary"],
        "foreground": "#FFFFFF", "relief": "sunken", "state": "normal",
        "_interactive": True,
    },
    "disabled": {
        "style": "Disabled.TButton", "cursor": "arrow", "background": COLORS["divider"],
        "foreground": COLORS["text_secondary"], "relief": "flat", "state": "disabled",
        "_interactive": False,
    },
    "loading": {
        "style": "Loading.TButton", "cursor": "watch", "background": COLORS["warning"],
        "foreground": "#FFFFFF", "relief": "flat", "state": "disabled",
        "text": "处理中…", "_interactive": False,
    },
}


def component_state(widget: Any, state: str) -> dict[str, Any]:
    """把组件切到 hover / active / disabled / loading 四态之一。

    逐项调用 ``widget.configure``；真实 ttk 组件不支持的样式键（例如给
    TButton 配 background）会被容错跳过，stub / fake widget 则全部生效。
    返回本次状态与已生效配置，便于界面与测试判断状态是否真的切换。
    """
    if state not in STATE_STYLES:
        raise ValueError(f"未知组件状态 {state!r}，允许：{', '.join(COMPONENT_STATES)}")
    cfg = dict(STATE_STYLES[state])
    applied: dict[str, Any] = {}
    for key, value in cfg.items():
        try:
            widget.configure(**{key: value})
            applied[key] = value
        except Exception:
            # ttk 对部分选项不接受（TclError）；此处只记录成功项，不中断切换
            continue
    try:
        widget.configure(_ae_qc_component_state=state)
        applied["_ae_qc_component_state"] = state
    except Exception:
        pass
    try:
        setattr(widget, "_ae_qc_component_state", state)
    except Exception:
        pass
    return {"widget": widget, "state": state, "config": cfg, "applied": applied}


def apply_theme(root: Any = None) -> dict[str, Any]:
    """把设计 token 应用到 ttk 样式；无显示 / fake root 时安全退化。

    有真实 Tk root 时配置 ttk.Style（颜色 / 字体 / 行高）；没有则仅返回 token，
    并把它挂到 root 上供界面读取，绝不创建 Tk() 顶层实例。
    """
    result: dict[str, Any] = {"tokens": TOKENS, "tk_applied": False, "style": None}
    if root is not None:
        try:
            import tkinter.ttk as ttk  # 惰性：顶层绝不 import tkinter / 绝不建 Tk()

            style = ttk.Style(root)
            family = FONTS["family"][0]
            c = COLORS
            style.configure("TFrame", background=c["base"])
            style.configure("Panel.TFrame", background=c["panel"])
            style.configure("TLabel", background=c["base"], foreground=c["text"],
                            font=(family, FONTS["table_body"]))
            style.configure("Header.TLabel", background=c["base"],
                            foreground=c["primary"], font=(family, FONTS["page_header"]))
            style.configure("PanelTitle.TLabel", background=c["panel"],
                            font=(family, FONTS["panel_title"]))
            style.configure("Caption.TLabel", background=c["panel"],
                            foreground=c["text_secondary"], font=(family, FONTS["caption"]))
            style.configure("TButton", font=(family, FONTS["table_body"]))
            style.configure("Treeview", rowheight=ROW_HEIGHT,
                            font=(family, FONTS["table_body"]))
            style.map("TButton",
                      background=[("active", c["primary"]), ("disabled", c["divider"])])
            result["tk_applied"] = True
            result["style"] = style
        except Exception as exc:
            # fake widget / 无 DISPLAY：ttk.Style 需要真实 Tk root，退化不报错
            result["tk_applied"] = False
            result["reason"] = type(exc).__name__
    try:
        if root is not None:
            setattr(root, "_ae_qc_theme", result)
    except Exception:
        pass
    return result


def tkinter_available() -> bool:
    """只探测模块可导入，不创建 Tk()（无 DISPLAY 下创建会抛 TclError）。"""
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False


# ---- 无显示环境用的 duck typing 替身 ----
class StubWidget:
    """最小 tkinter widget 替身：视图逻辑只依赖这些方法，绝不调用真实 Tk()。"""

    def __init__(self, kind: str = "widget", parent: Any = None, **options: Any):
        self.kind = kind
        self.parent = parent
        self.options: dict[str, Any] = dict(options)
        self.children: list[Any] = []
        self.bindings: dict[str, Any] = {}
        self.drawn: list[tuple[str, tuple, dict]] = []   # Canvas 绘制记录
        self.inserted: list[tuple[tuple, dict]] = []     # Treeview 插入记录
        self.calls: list[tuple[str, tuple, dict]] = []   # 其余方法调用记录
        self._ae_qc_component_state: str | None = None
        if parent is not None and hasattr(parent, "children"):
            parent.children.append(self)

    def configure(self, **kw: Any) -> None:
        self.options.update(kw)

    def config(self, **kw: Any) -> None:
        self.configure(**kw)

    def cget(self, key: str) -> Any:
        return self.options.get(key)

    def grid(self, **kw: Any) -> "StubWidget":
        self.options["_grid"] = kw
        return self

    def pack(self, **kw: Any) -> "StubWidget":
        self.options["_pack"] = kw
        return self

    def place(self, **kw: Any) -> "StubWidget":
        self.options["_place"] = kw
        return self

    def bind(self, event: str, callback: Any = None) -> Any:
        self.bindings[event] = callback
        return callback

    # 原生 Canvas 绘制 API（批次通过率细条）
    def create_rectangle(self, *args: Any, **kw: Any) -> int:
        self.drawn.append(("rectangle", args, kw))
        return len(self.drawn)

    def create_text(self, *args: Any, **kw: Any) -> int:
        self.drawn.append(("text", args, kw))
        return len(self.drawn)

    def create_line(self, *args: Any, **kw: Any) -> int:
        self.drawn.append(("line", args, kw))
        return len(self.drawn)

    def create_oval(self, *args: Any, **kw: Any) -> int:
        self.drawn.append(("oval", args, kw))
        return len(self.drawn)

    def delete(self, *args: Any) -> None:
        self.drawn.clear()
        self.inserted.clear()

    # Treeview / 表格
    def insert(self, *args: Any, **kw: Any) -> int:
        self.inserted.append((args, kw))
        return len(self.inserted)

    def get_children(self, *args: Any) -> list[Any]:
        return list(range(len(self.inserted)))

    def selection(self) -> tuple:
        return getattr(self, "_selection", ())

    def selection_set(self, iid: Any) -> None:
        self._selection = (iid,)

    # 按钮：invoke 触发 command（测试据此断言点击走接口）
    def invoke(self) -> Any:
        cmd = self.options.get("command")
        if callable(cmd):
            return cmd()
        return None

    def column(self, *args: Any, **kw: Any) -> Any:
        self.calls.append(("column", args, kw))
        return None

    def heading(self, *args: Any, **kw: Any) -> Any:
        self.calls.append(("heading", args, kw))
        return None

    def __getattr__(self, name: str) -> Any:
        # 其余 tkinter 方法（winfo_* / rowconfigure / columnconfigure / after 等）
        # 以记录型 no-op 兜底，保证 duck typing 不因缺方法而崩。
        def _noop(*args: Any, **kw: Any) -> Any:
            self.calls.append((name, args, kw))
            return None
        return _noop


class StubFactory:
    """无显示环境默认工厂：产出 StubWidget，供视图逻辑被真实驱动。"""

    def __init__(self) -> None:
        self.created: list[StubWidget] = []

    def _make(self, kind: str, parent: Any = None, **kw: Any) -> StubWidget:
        widget = StubWidget(kind, parent, **kw)
        self.created.append(widget)
        return widget

    def frame(self, parent: Any = None, **kw: Any) -> StubWidget:
        return self._make("frame", parent, **kw)

    def labelframe(self, parent: Any = None, **kw: Any) -> StubWidget:
        return self._make("labelframe", parent, **kw)

    def label(self, parent: Any = None, **kw: Any) -> StubWidget:
        return self._make("label", parent, **kw)

    def button(self, parent: Any = None, **kw: Any) -> StubWidget:
        return self._make("button", parent, **kw)

    def entry(self, parent: Any = None, **kw: Any) -> StubWidget:
        return self._make("entry", parent, **kw)

    def canvas(self, parent: Any = None, **kw: Any) -> StubWidget:
        return self._make("canvas", parent, **kw)

    def treeview(self, parent: Any = None, **kw: Any) -> StubWidget:
        return self._make("treeview", parent, **kw)


__all__ = [
    "TOKENS", "COLORS", "SPACING", "GRID", "FONTS", "ROW_HEIGHT", "LAYOUT", "PASS_BAR",
    "COMPONENT_STATES", "STATE_STYLES", "component_state", "apply_theme", "tkinter_available",
    "StubWidget", "StubFactory",
]
