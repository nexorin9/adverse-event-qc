"""待写队列：5xx/网络/限流耗尽后的发现单落此，恢复后按幂等键重放不产生重复。

人工确认是唯一放行口：未确认／降级发现单不进队列（enqueue 前置校验）。
与 ledger.idempotency 联动：同键只保留一条待写记录；replay 前查幂等台账，
已成功写回过的键直接丢弃不再重发，保证「同键只产生一条发现单」。
年度底稿不自动外发（见 reporting.annual_draft）。
"""
from __future__ import annotations

from typing import Any

from ..ledger.idempotency import IdempotencyLedger
from ..ledger.store import Store, dumps, loads, now_iso
from ..logging_util import log_event
from .guard import check_writeback


class WritebackQueue:
    """pending_writeback 表的读写封装；store 应与 QCClient 显式共享同一实例。"""

    def __init__(self, store: Store | None = None):
        # store=None 用内存 SQLite：功能完整且不落用户磁盘；生产/测试要与其他组件
        # 共享台账时，应显式传入同一个 Store 实例。
        self.store = store if store is not None else Store(":memory:")
        self.store.initialize()

    def enqueue(self, finding: dict, key: str) -> bool:
        """入队一条已确认发现单；未确认／降级一律不进队列（返回 False）。

        同 key 去重（INSERT OR IGNORE），保留完整请求体以便按幂等键重放。
        """
        finding = dict(finding)
        state = str(finding.get("status", ""))
        degraded = bool(finding.get("degraded") or finding.get("_degraded"))
        verdict = check_writeback(finding, state, degraded)
        if not verdict["allowed"]:
            log_event("WARN", code="WRITEBACK_NOT_CONFIRMED",
                      message="未确认／降级发现单不得进入待写队列",
                      batch_ref=str(finding.get("batch_ref", "")))
            return False
        self.store.execute(
            "INSERT OR IGNORE INTO pending_writeback(key,finding_id,body_json,attempts,enqueued_at)"
            " VALUES (?,?,?,?,?)",
            (key, str(finding.get("finding_id", "")), dumps(finding), 0, now_iso()),
        )
        return True

    def count(self) -> int:
        return int(self.store.query("SELECT COUNT(*) AS n FROM pending_writeback")[0]["n"])

    def list(self) -> list[dict[str, Any]]:
        rows = self.store.query("SELECT * FROM pending_writeback ORDER BY enqueued_at, key")
        return [{"key": r["key"], "finding_id": r["finding_id"],
                 "finding": loads(r["body_json"], {}),
                 "attempts": int(r["attempts"] or 0),
                 "enqueued_at": r["enqueued_at"]} for r in rows]

    def replay(self, client: Any) -> dict[str, Any]:
        """按幂等键重发；同键只产生一条发现单。

        - 幂等台账里该键已成功 → 直接出队，不再发请求（skipped）；
        - 成功重发 → 记幂等台账 + 出队（sent）；
        - 仍失败 → attempts+1，保留队列（pending），下次继续。
        返回 {sent, skipped, pending, results}。
        """
        ledger = IdempotencyLedger(self.store)
        sent = skipped = 0
        results: list[dict] = []
        for item in self.list():
            key = item["key"]
            prior = ledger.get(key)
            if prior and prior["first_result"].get("ok"):
                self._remove(key)
                skipped += 1
                continue
            finding = dict(item["finding"])
            row_key = str(finding.get("row_key") or finding.get("_row_key") or "")
            outcome = client.post_finding(finding, row_key=row_key)
            results.append(outcome)
            if outcome.get("ok"):
                ledger.record(key, finding, result=dict(outcome))
                self._remove(key)
                sent += 1
            else:
                self._bump(key)
        return {"sent": sent, "skipped": skipped, "pending": self.count(),
                "results": results}

    def _remove(self, key: str) -> None:
        self.store.execute("DELETE FROM pending_writeback WHERE key=?", (key,))

    def _bump(self, key: str) -> None:
        self.store.execute("UPDATE pending_writeback SET attempts=attempts+1 WHERE key=?", (key,))


__all__ = ["WritebackQueue"]
