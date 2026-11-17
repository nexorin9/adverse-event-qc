"""托盘常驻（任务 49）：空闲隐藏、收到新批次弹角标；无 pystray → window_only 降级。

pystray 只在探测函数内惰性 import；缺失时降级为「仅窗口」模式，并在启动日志
（结构化 log_event）与 ``fallback_note`` / ``notice`` 说明文案中写明，主窗口保持可用。
顶层不 import tkinter / pystray、不创建任何顶层实例。
"""
from __future__ import annotations

from typing import Any, Callable

from ..logging_util import log_event

WINDOW_ONLY_NOTE = (
    "未检测到 pystray，桌面形态降级为「仅窗口」(window_only) 模式："
    "托盘常驻与角标不可用，主窗口保持可用；安装 pystray 并重启后可恢复托盘。"
)
TRAY_ENABLED_NOTE = "托盘常驻已启用（pystray 可用）：空闲隐藏，收到新批次弹角标。"


def _probe_pystray() -> Any:
    """探测 pystray；函数内惰性 import，缺失返回 None（绝不抛栈）。"""
    try:
        import pystray  # noqa: F401
        return pystray
    except Exception:
        return None


class TrayController:
    """托盘 / 窗口形态控制器。

    ``pystray=False`` 可强制降级（用于测试）；``probe`` 可注入探测函数，便于
    在已安装 pystray 的机器上稳定复现 window_only 路径。
    """

    def __init__(self, *, pystray: Any = None, icon: Any = None,
                 probe: Callable[[], Any] | None = None, auto_start: bool = True):
        probe_fn = probe or _probe_pystray
        if pystray is False:
            module = None
        elif pystray is None:
            module = probe_fn()
        else:
            module = pystray

        self.pystray = module
        self._icon = icon
        self.mode = "tray" if module is not None else "window_only"
        self.fallback_note = WINDOW_ONLY_NOTE if self.mode == "window_only" else ""
        self.notice = TRAY_ENABLED_NOTE if self.mode == "tray" else WINDOW_ONLY_NOTE
        self.visible = True
        self.badge = False
        self.started = False
        self.notifications: list[dict[str, Any]] = []

        # 启动日志：降级原因 / 模式写进结构化日志与说明文案
        self.startup_log = log_event(
            level="INFO", code="TRAY_MODE", message=self.notice,
            mode=self.mode, component="desktop.tray", pystray_available=module is not None)
        if auto_start:
            self.start()

    def start(self) -> dict[str, Any]:
        """标记常驻已启动；真实托盘线程由宿主在外部运行，这里不阻塞、不建 Tk()。"""
        self.started = True
        return self.status()

    def set_idle(self) -> bool:
        """空闲隐藏主窗口（托盘仍在后台常驻）。"""
        self.visible = False
        return self.visible

    def show(self) -> bool:
        self.visible = True
        return self.visible

    def hide(self) -> bool:
        return self.set_idle()

    def notify_new_batch(self, batch_ref: str) -> dict[str, Any]:
        """收到新批次：弹角标并显示窗口；window_only 模式仍返回降级说明。"""
        entry: dict[str, Any] = {"batch_ref": str(batch_ref), "mode": self.mode,
                                 "badge": False}
        if self.mode == "tray" and self._icon is not None:
            try:
                self._icon.notify(f"收到新批次 {batch_ref}", "不良事件质检")
                entry["badge"] = True
            except Exception as exc:
                entry["error"] = type(exc).__name__
        else:
            entry["note"] = self.fallback_note or self.notice
        self.badge = entry["badge"]
        self.notifications.append(entry)
        self.visible = True
        return entry

    def status(self) -> dict[str, Any]:
        return {"mode": self.mode, "notice": self.notice,
                "fallback_note": self.fallback_note, "visible": self.visible,
                "started": self.started, "badge": self.badge,
                "notifications": len(self.notifications),
                "pystray_available": self.pystray is not None}


__all__ = ["TrayController", "WINDOW_ONLY_NOTE", "TRAY_ENABLED_NOTE"]
