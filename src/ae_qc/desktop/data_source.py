"""界面 → 台账查询层的唯一取数入口（任务 50）。

界面上所有数字（批次列表、越界行、待确认数、待写队列数）都来自
``ledger.queries``，不自己算、不直连存储。确认 / 驳回 / 撤回走
``ledger.state_machine`` 与 ``attribution.feedback_loop`` 接口。

工程约束：desktop/ 下不直连底层存储、不出现直接查询；引擎层零 GUI 依赖。
"""
from __future__ import annotations

from typing import Any

from ..attribution.feedback_loop import FeedbackLoop
from ..ledger import state_machine
from ..ledger.queries import LedgerQueries


class DesktopDataSource:
    """界面与台账之间的唯一接线层。

    ``queries`` 为空时惰性连默认台账（构造时不连库、不建文件），便于无显示
    环境下只做装配测试；测试统一传 ``tmp_db`` 的 ``LedgerQueries``。
    """

    def __init__(self, queries: LedgerQueries | None = None):
        self._queries = queries
        self._feedback_loop: FeedbackLoop | None = None

    @property
    def queries(self) -> LedgerQueries:
        """暴露底层查询对象（供确认面板取 store），界面本身仍不碰查询。"""
        if self._queries is None:
            from ..ledger.store import Store
            self._queries = LedgerQueries(Store())
        return self._queries

    # ---- 取数：一律转发台账查询层 ----
    def list_batches(self) -> list[dict[str, Any]]:
        return self.queries.list_batches()

    def unexpected_rows(self, batch_ref: str | None = None, rule_id: str | None = None,
                        dept_code: str | None = None,
                        row_no: int | None = None) -> list[dict[str, Any]]:
        return self.queries.unexpected_rows(batch_ref, rule_id, dept_code, row_no)

    def pending_attributions(self) -> list[dict[str, Any]]:
        return self.queries.pending_attributions()

    def pending_drafts(self) -> list[dict[str, Any]]:
        return self.pending_attributions()

    def pending_count(self) -> int:
        return self.queries.pending_count()

    def pending_writeback_count(self) -> int:
        return self.queries.pending_writeback_count()

    def batch_stats(self, batch_ref: str) -> dict[str, Any]:
        return self.queries.batch_stats(batch_ref)

    def status_numbers(self) -> dict[str, int]:
        """状态栏一次性取两个数，保证与主窗口其它处口径一致。"""
        return {"pending_count": self.pending_count(),
                "pending_writeback_count": self.pending_writeback_count()}

    # ---- 动作：走状态机与反馈回流接口，不直接改库 ----
    def confirm_finding(self, finding_id: str, from_state: str, actor: str,
                        batch_ref: str = "") -> dict[str, Any]:
        """确认：attributed_pending → confirmed（模型草稿永不自行推进）。"""
        return state_machine.transition(self.queries.store, finding_id, from_state,
                                        "confirmed", actor, batch_ref=batch_ref)

    def withdraw_finding(self, finding_id: str, actor: str, reason: str = "",
                         batch_ref: str = "") -> dict[str, Any]:
        """撤回：confirmed → attributed_pending（留痕）。"""
        return state_machine.withdraw(self.queries.store, finding_id, actor, reason,
                                      batch_ref=batch_ref)

    def _feedback(self) -> FeedbackLoop:
        if self._feedback_loop is None:
            self._feedback_loop = FeedbackLoop(store=self.queries.store)
        return self._feedback_loop

    def confirm_attribution(self, row: dict, code: str, reviewer: str,
                            batch_ref: str = "") -> dict[str, Any]:
        """人工确认建议编码 → 只写别名候选表，不直接生效。"""
        return self._feedback().confirm(row, code, reviewer, batch_ref)

    def reject_attribution(self, row: dict, code: str, reviewer: str,
                           batch_ref: str = "", reason: str = "") -> dict[str, Any]:
        """驳回建议编码 → 写驳回候选，供评测与后续人工指定。"""
        return self._feedback().reject(row, code, reviewer, batch_ref, reason)


__all__ = ["DesktopDataSource"]
