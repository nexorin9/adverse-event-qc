"""幂等键台账：sha256(batch_ref|rule_id|row_key)。

同键重复入库不增行；同键重发只发一次并返回首次结果（与写回待写队列联动）。
"""
from __future__ import annotations

import hashlib
from typing import Any

from .store import Store, dumps, loads, now_iso


def idempotency_key(batch_ref: str, rule_id: str, row_key: str) -> str:
    raw = f"{batch_ref}|{rule_id}|{row_key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class IdempotencyLedger:
    def __init__(self, store: Store):
        self.store = store
        self.store.initialize()

    def record(self, key: str, payload: dict[str, Any] | None = None, *,
               result: dict[str, Any] | None = None) -> dict[str, Any]:
        """首次入库返回 is_new=True；重复键返回首次结果且不增行。"""
        existing = self.get(key)
        if existing is not None:
            return {"key": key, "is_new": False, "result": existing["first_result"],
                    "count": self.count()}
        body = dict(payload or {})
        first = dict(result or {"accepted": True, "key": key})
        self.store.execute(
            "INSERT INTO idempotency_keys(key,batch_ref,rule_id,row_key,payload_json,first_result_json,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (key, body.get("batch_ref", ""), body.get("rule_id", ""), body.get("row_key", ""),
             dumps(body), dumps(first), now_iso()),
        )
        return {"key": key, "is_new": True, "result": first, "count": self.count()}

    def get(self, key: str) -> dict[str, Any] | None:
        rows = self.store.query("SELECT * FROM idempotency_keys WHERE key = ?", (key,))
        if not rows:
            return None
        row = rows[0]
        return {"key": key, "payload": loads(row["payload_json"], {}),
                "first_result": loads(row["first_result_json"], {}),
                "created_at": row["created_at"]}

    def count(self) -> int:
        return int(self.store.query("SELECT COUNT(*) AS n FROM idempotency_keys")[0]["n"])

    def replay_once(self, key: str, payload: dict | None = None) -> dict[str, Any]:
        """同键重发：只返回首次结果，绝不产生第二条记录。"""
        return self.record(key, payload)
