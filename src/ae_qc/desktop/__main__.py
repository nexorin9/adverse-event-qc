"""桌面端启动入口：`python -m ae_qc.desktop`。

日常业务入口是桌面三栏窗口；CLI 只做运维与无人值守跑批。本模块是唯一会
创建真实 ``Tk()`` 并进入 ``mainloop`` 的地方，装配逻辑仍复用 ``app.build_app``，
因此无显示环境测试完全不依赖这里。托盘常驻线程由宿主在外部运行，缺失
pystray 时由 ``TrayController`` 降级为「仅窗口」模式并写结构化日志。
"""
from __future__ import annotations

import sys
from typing import Any


def main() -> int:
    try:
        import tkinter as tk
        root: Any = tk.Tk()
    except Exception as exc:  # 无 DISPLAY / 缺 tkinter：明确报错，不静默假成功
        print(f"[桌面不可用] 当前环境无法创建图形窗口：{exc}", file=sys.stderr)
        return 4

    from .app import APP_TITLE, WINDOW, build_app
    from .data_source import DesktopDataSource
    from .tray import TrayController

    root.title(APP_TITLE)
    root.geometry(f"{WINDOW['width']}x{WINDOW['height']}")
    root.minsize(WINDOW["min_width"], WINDOW["min_height"])
    app = build_app(root=root, data_source=DesktopDataSource())
    tray = TrayController(auto_start=True)
    print(f"[桌面] 常驻模式：{tray.mode} · {tray.notice}")
    app["refresh"]()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
