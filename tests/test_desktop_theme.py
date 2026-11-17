"""任务 45：设计 token 齐全（色板 / 栅格 / 字体层级 / 行高）；四态样式函数存在且可切换。

无显示环境：用 duck typing 的 fake widget 驱动，不创建真实 Tk()。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ae_qc.desktop import theme


class FakeWidget:
    """duck typing 替身：只实现界面逻辑会调用的几个方法。"""

    def __init__(self):
        self.configs: list[dict] = []
        self._ae_qc_component_state = None

    def configure(self, **kw):
        self.configs.append(dict(kw))

    def grid(self, **kw):
        return self

    def bind(self, event, callback=None):
        return callback

    def create_rectangle(self, *args, **kw):
        return 1


def test_color_tokens_complete():
    expected = {
        "base": "#F5F7FA", "panel": "#FFFFFF", "primary": "#1F5FA9",
        "success": "#2E7D4F", "warning": "#B26A00", "error": "#C0392B",
        "text": "#1F2933", "text_secondary": "#5B6770", "divider": "#E3E8EF",
    }
    assert theme.TOKENS["colors"] == expected
    assert theme.COLORS == expected
    assert len(expected) == 9


def test_spacing_grid_tokens():
    assert theme.GRID == (8, 16, 24, 32)
    assert theme.TOKENS["spacing"] == {"xs": 8, "sm": 16, "md": 24, "lg": 32}
    assert set(theme.TOKENS["spacing"].values()) == set(theme.GRID)


def test_font_hierarchy_row_height_and_layout_tokens():
    fonts = theme.TOKENS["fonts"]
    assert fonts["page_header"] == 15
    assert fonts["panel_title"] == 13
    assert fonts["table_body"] == 12
    assert fonts["caption"] == 11
    assert len(fonts["family"]) == 3
    assert theme.ROW_HEIGHT == 32 and theme.TOKENS["row_height"] == 32
    assert theme.LAYOUT == {"left": 240, "center": "flex", "right": 360}
    assert theme.TOKENS["pass_bar"]["height"] == 8


def test_component_state_four_states_exist_and_switch():
    widget = FakeWidget()
    seen = []
    for state in theme.COMPONENT_STATES:
        result = theme.component_state(widget, state)
        assert result["state"] == state
        assert widget._ae_qc_component_state == state       # 状态真的落到 widget
        assert widget.configs                               # configure 被真实调用
        seen.append(result["config"])
    assert len(theme.COMPONENT_STATES) == 4
    assert len({cfg["style"] for cfg in seen}) == 4         # 四态样式互不相同
    assert theme.component_state(widget, "disabled")["config"]["state"] == "disabled"
    assert theme.component_state(widget, "loading")["config"]["text"] == "处理中…"
    assert widget._ae_qc_component_state == "loading"        # 连续切换生效
    with pytest.raises(ValueError):
        theme.component_state(widget, "bogus")


def test_apply_theme_without_display_returns_tokens():
    fake_root = FakeWidget()
    result = theme.apply_theme(fake_root)
    assert result["tokens"] is theme.TOKENS
    assert result["tk_applied"] is False                    # 无真实 root 时安全退化
    assert fake_root._ae_qc_theme is result                 # token 挂到 root 供界面读取
    assert theme.apply_theme(None)["tk_applied"] is False


def test_module_top_level_has_no_gui_import_or_root_creation():
    src = Path(theme.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:  # 仅顶层语句，函数内的惰性导入不算
        if isinstance(node, ast.Import):
            assert not any(alias.name.startswith("tkinter") for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("tkinter")
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "Tk" for node in tree.body for n in ast.walk(node))
