"""右栏归因确认面板（任务 47）：人工确认是唯一放行口。

面板只展示草稿并发出动作，所有落库都走底座接口：
* 确认 → ``ledger.state_machine.transition``（attributed_pending → confirmed）；
* 驳回 → ``attribution.feedback_loop.reject``（写驳回候选，不直接改库）；
* 撤回 → 二次确认对话框通过后 ``ledger.state_machine.withdraw``；
* 降级行 → 显示降级原因 + 重试入口，确认按钮置 disabled（不写回）。

面板内不直连底层存储、不含查询逻辑；顶层不导入 GUI 工具包（默认对话框惰性导入）。
"""
from __future__ import annotations

from typing import Any

from ...attribution import degradation
from ...attribution.feedback_loop import FeedbackLoop
from ...ledger import state_machine
from ..theme import LAYOUT, StubFactory, component_state

FIELDS = ("草稿", "置信度", "建议编码", "依据", "状态", "降级原因")


def _default_dialog(title: str, message: str) -> bool:
    """破坏性操作的二次确认；无显示环境惰性导入失败时默认 False（不撤回）。"""
    try:
        from tkinter import messagebox  # 函数内惰性：顶层绝不 import tkinter
        return bool(messagebox.askyesno(title, message))
    except Exception:
        return False


class ConfirmPanel:
    """右栏 360px：草稿 / 置信度 / 建议编码 / 依据 / 状态 / 降级原因 + 四个按钮。"""

    def __init__(self, parent: Any = None, *, factory: Any = None,
                 store: Any = None, feedback_loop: FeedbackLoop | None = None,
                 data_source: Any = None, dialog: Any = None,
                 actor: str = "质控员", retry_callback: Any = None,
                 catalogs_dir: Any = None):
        self.factory = factory or StubFactory()
        self.width = LAYOUT["right"]
        self._store = store
        self._data_source = data_source
        self._feedback_loop = feedback_loop
        self._catalogs_dir = catalogs_dir
        self._dialog = dialog
        self.actor = actor
        self.retry_callback = retry_callback
        self.on_action: Any = None          # 可选观测回调，便于无显示环境断言

        self.current: dict[str, Any] = {}   # 当前草稿
        self.row: dict[str, Any] = {}       # 关联的越界行（行表选中时联动）
        self.finding_id = ""
        self.from_state = "attributed_pending"
        self.batch_ref = ""
        self.basis = ""
        self._is_degraded = False
        self.degraded_reason_text = ""
        self.action_log: list[dict[str, Any]] = []
        self.last_action: dict[str, Any] = {}

        self.container = self.factory.frame(parent, width=self.width)
        self.title = self.factory.label(self.container, text="归因确认")
        self.labels = {f: self.factory.label(self.container, text=f"{f}：-") for f in FIELDS}
        self.confirm_button = self.factory.button(self.container, text="确认",
                                                   command=self.on_confirm)
        self.reject_button = self.factory.button(self.container, text="驳回",
                                                  command=self.on_reject)
        self.withdraw_button = self.factory.button(self.container, text="撤回已确认",
                                                    command=self.on_withdraw)
        self.retry_button = self.factory.button(self.container, text="重试归因",
                                                 command=self.on_retry)
        self.buttons = {
            "confirm": self.confirm_button, "reject": self.reject_button,
            "withdraw": self.withdraw_button, "retry": self.retry_button,
        }
        component_state(self.withdraw_button, "disabled")
        component_state(self.retry_button, "disabled")

    # ---- 依赖惰性解析（构造时零副作用、不连库） ----
    def _store_or_default(self) -> Any:
        if self._store is not None:
            return self._store
        if self._data_source is not None:
            return self._data_source.queries.store
        from ...ledger.store import Store
        self._store = Store()
        return self._store

    def _feedback(self) -> FeedbackLoop:
        if self._feedback_loop is None:
            self._feedback_loop = FeedbackLoop(catalogs_dir=self._catalogs_dir,
                                               store=self._store_or_default())
        return self._feedback_loop

    # ---- 展示 ----
    def set_row(self, row: dict) -> dict:
        self.row = dict(row or {})
        return self.row

    def show(self, draft: Any, *, finding_id: str | None = None,
             from_state: str | None = None, batch_ref: str | None = None,
             basis: str | None = None) -> dict[str, Any]:
        """渲染一行归因草稿；降级行显示原因并启用重试入口。"""
        if hasattr(draft, "to_dict"):
            draft = draft.to_dict()
        self.current = dict(draft or {})
        self.finding_id = str(finding_id or self.current.get("finding_id")
                              or self.current.get("row_key") or "")
        self.from_state = str(from_state or self.current.get("state")
                              or "attributed_pending")
        self.batch_ref = str(batch_ref or self.current.get("batch_ref") or "")
        self.basis = str(basis or self.current.get("basis") or "")

        self._is_degraded = degradation.is_degraded(self.current)
        self.degraded_reason_text = (degradation.degraded_reason(self.current)["display_text"]
                                     if self._is_degraded else "")
        values = {
            "草稿": str(self.current.get("draft_text") or "（无草稿文本）"),
            "置信度": f"{float(self.current.get('confidence') or 0.0):.2f}",
            "建议编码": str(self.current.get("suggested_code") or "（未建议编码）"),
            "依据": self.basis or str(self.current.get("cause_type") or "—"),
            "状态": self.from_state,
            "降级原因": self.degraded_reason_text or "（无）",
        }
        for name, text in values.items():
            self.labels[name].configure(text=f"{name}：{text}")

        if self._is_degraded:
            # 降级行：确认禁用、显示重试入口
            component_state(self.confirm_button, "disabled")
            component_state(self.retry_button, "active")
            component_state(self.withdraw_button, "disabled")
        else:
            component_state(self.confirm_button, "active")
            component_state(self.retry_button, "disabled")
            component_state(self.withdraw_button,
                            "active" if self.from_state == "confirmed" else "disabled")
        return {"draft": dict(self.current), "degraded": self._is_degraded,
                "degraded_reason": self.degraded_reason_text, "fields": values}

    # ---- 动作：一律走底座接口 ----
    def _record(self, action: str, result: dict[str, Any]) -> dict[str, Any]:
        entry = {"action": action, "finding_id": self.finding_id, "result": result}
        self.action_log.append(entry)
        self.last_action = entry
        if callable(self.on_action):
            self.on_action(action, result)
        return result

    def on_confirm(self) -> dict[str, Any]:
        """确认按钮 → 状态机 transition（模型草稿永不自行推进，降级行不许确认）。"""
        if self._is_degraded:
            return self._record("confirm", {"ok": False, "code": "WRITEBACK_DEGRADED",
                                            "reason": "降级行不得确认或写回"})
        result = state_machine.transition(
            self._store_or_default(), self.finding_id, self.from_state, "confirmed",
            self.actor, batch_ref=self.batch_ref)
        self.from_state = "confirmed"
        component_state(self.withdraw_button, "active")
        return self._record("confirm", {"ok": True, "transition": result,
                                        "to_state": "confirmed"})

    def on_reject(self) -> dict[str, Any]:
        """驳回按钮 → feedback_loop.reject（只写别名驳回候选，不直接改库）。"""
        row = self.row or self.current
        code = str(self.current.get("suggested_code") or "")
        result = self._feedback().reject(row, code, self.actor, self.batch_ref,
                                         reason="质控员在确认面板驳回建议编码")
        return self._record("reject", {"ok": True, "feedback": result})

    def on_withdraw(self) -> dict[str, Any]:
        """撤回已确认结论：先二次确认，通过后才走 state_machine.withdraw。"""
        approved = bool(self._ask("二次确认",
                                  "撤回已确认结论将退回 attributed_pending 并留痕，确认撤回？"))
        if not approved:
            return self._record("withdraw", {"ok": False, "withdrawn": False,
                                            "reason": "用户取消二次确认，未执行撤回"})
        result = state_machine.withdraw(self._store_or_default(), self.finding_id,
                                        self.actor, "质控员撤回已确认结论",
                                        batch_ref=self.batch_ref)
        self.from_state = "attributed_pending"
        component_state(self.withdraw_button, "disabled")
        return self._record("withdraw", {"ok": True, "withdrawn": True,
                                        "transition": result})

    def on_retry(self) -> dict[str, Any]:
        """降级行重试入口：触发外部注入的归因重跑回调。"""
        if callable(self.retry_callback):
            outcome = self.retry_callback(self.current, self.row)
            return self._record("retry", {"ok": True, "result": outcome})
        return self._record("retry", {"ok": False,
                                     "reason": "未配置归因重跑回调，请稍后重试"})

    def _ask(self, title: str, message: str) -> bool:
        if self._dialog is not None:
            return bool(self._dialog(title, message))
        return _default_dialog(title, message)


__all__ = ["ConfirmPanel", "FIELDS"]
