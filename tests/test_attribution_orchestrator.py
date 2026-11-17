"""任务 34：批量编排（分组 / 限流 / 逐行回填 / 部分失败 / 断点续跑）。"""
from __future__ import annotations

import json
import threading
import time

from ae_qc.attribution.orchestrator import BATCH_SCHEMA, AttributionOrchestrator
from ae_qc.ledger.queries import LedgerQueries
from ae_qc.ledger.store import Store


class _ScriptedGateway:
    """线程安全的本地协议替身：按批量请求里的 row_key 返回结果，可注入失败与延迟。"""

    def __init__(self, *, fail_keys=(), shuffle=True, delay=0.0):
        self.fail_keys = set(fail_keys)
        self.shuffle = shuffle
        self.delay = delay
        self.call_count = 0
        self.max_concurrent = 0
        self.batch_keys = []
        self._cur = 0
        self._lock = threading.Lock()

    def structured_call(self, messages, schema, **kw):
        with self._lock:
            self.call_count += 1
            self._cur += 1
            self.max_concurrent = max(self.max_concurrent, self._cur)
        try:
            assert schema == BATCH_SCHEMA
            rows = json.loads(messages[1]["content"])["rows"]
            keys = [r["row_key"] for r in rows]
            with self._lock:
                self.batch_keys.append(list(keys))
            if self.delay:
                time.sleep(self.delay)
            if any(k in self.fail_keys for k in keys):
                return {"ok": False, "data": None, "unavailable": True,
                        "code": "CONN_FAILED", "meta": {}}
            results = [{
                "row_key": k, "draft_text": f"draft:{k}", "cause_type": "spelling_drift",
                "suggested_code": "AE-CAT-08", "confidence": 0.9, "missing_info": [],
            } for k in keys]
            if self.shuffle:
                results.reverse()  # 故意逆序，验证逐行按 row_key 回填不错位
            return {"ok": True, "data": {"results": results},
                    "unavailable": False, "code": "", "meta": {}}
        finally:
            with self._lock:
                self._cur -= 1


def _rows(count=6):
    return [{"row_key": f"k{i}", "batch_ref": "D01-2026-01",
             "rule_family": "set_membership", "text": "x" * 10} for i in range(count)]


def test_grouping_by_rule_family_and_text_length(tmp_db):
    """按规则族 × 文本长度分桶分组，每组一次批量请求。"""
    store = Store(tmp_db)
    rows = [
        {"row_key": "a-short", "batch_ref": "B1", "rule_family": "set_membership", "text": "x" * 10},
        {"row_key": "a-long", "batch_ref": "B1", "rule_family": "set_membership", "text": "x" * 60},
        {"row_key": "b-short", "batch_ref": "B1", "rule_family": "alias_resolution", "text": "x" * 10},
        {"row_key": "b-long", "batch_ref": "B1", "rule_family": "alias_resolution", "text": "x" * 60},
    ]
    gw = _ScriptedGateway()
    out = AttributionOrchestrator(gw, store=store, batch_size=50, max_concurrency=1).run(rows)
    summary = out["summary"]

    assert summary["groups"] == 4
    assert summary["batches"] == 4
    assert ["set_membership", 0] in summary["group_keys"]
    assert ["set_membership", 1] in summary["group_keys"]
    assert ["alias_resolution", 0] in summary["group_keys"]
    assert ["alias_resolution", 1] in summary["group_keys"]
    assert sorted(k for keys in gw.batch_keys for k in keys) == sorted(r["row_key"] for r in rows)
    assert all(len(keys) == 1 for keys in gw.batch_keys)
    assert summary["succeeded_rows"] == 4
    store.close()


def test_throttling_limit_effective(tmp_db):
    """超出的批次排队而非并发爆发：在途并发不超过 max_concurrency。"""
    store = Store(tmp_db)
    rows = _rows(8)
    gw = _ScriptedGateway(delay=0.02)
    out = AttributionOrchestrator(gw, store=store, batch_size=1, max_concurrency=2).run(rows)
    summary = out["summary"]

    assert summary["batches"] == 8
    assert summary["max_concurrency"] == 2
    assert summary["max_in_flight"] <= 2
    assert gw.max_concurrent <= 2
    assert summary["queued_batches"] == 6  # 8 批 - 2 个并发槽位，其余排队
    assert summary["queued_batches"] > 0
    assert gw.call_count == 8
    assert summary["succeeded_rows"] == 8
    store.close()


def test_row_key_backfill_no_misalignment(tmp_db):
    """模型结果逆序返回时，仍逐行按 row_key 回填，顺序与内容均不错位。"""
    store = Store(tmp_db)
    rows = _rows(6)
    gw = _ScriptedGateway(shuffle=True)
    out = AttributionOrchestrator(gw, store=store, batch_size=2, max_concurrency=3).run(rows)

    assert [d["row_key"] for d in out["drafts"]] == [r["row_key"] for r in rows]
    for draft, row in zip(out["drafts"], rows):
        assert draft["draft_text"] == f"draft:{row['row_key']}"
        assert draft["state"] == "attributed_pending"
        assert draft["source"] == "model"
    assert out["summary"]["failed_rows"] == 0
    store.close()


def test_partial_failure_marks_failed_and_persists_rest(tmp_db):
    """部分失败：失败行显式标记并进重试队列，其余行正常落库。"""
    store = Store(tmp_db)
    rows = _rows(6)
    gw = _ScriptedGateway(fail_keys={"k2", "k3"})
    out = AttributionOrchestrator(gw, store=store, batch_size=2, max_concurrency=2).run(rows)

    assert {f["row_key"] for f in out["failed"]} == {"k2", "k3"}
    assert all(f["error_code"] and f["reason"] for f in out["failed"])
    summary = out["summary"]
    assert summary["failed_rows"] == 2
    assert summary["succeeded_rows"] == 4
    assert summary["retry_queue_size"] == 2
    assert len(out["drafts"]) == 6
    assert [d["row_key"] for d in out["drafts"]] == [r["row_key"] for r in rows]

    pending = LedgerQueries(store).pending_attributions()
    assert len(pending) == 6
    states = {p["row_key"]: p["state"] for p in pending}
    for key in ("k0", "k1", "k4", "k5"):
        assert states[key] == "attributed_pending"
    for key in ("k2", "k3"):
        assert states[key] == "pending_manual_classification"
    store.close()


def test_resume_after_interruption(tmp_db):
    """中断后从断点续跑：成功行复用不重复调模型，失败行重试后补齐。"""
    store = Store(tmp_db)
    rows = _rows(6)
    failing = _ScriptedGateway(fail_keys={"k2", "k3"})
    orchestrator = AttributionOrchestrator(failing, store=store, batch_size=2, max_concurrency=2)
    first = orchestrator.run(rows)
    assert first["summary"]["failed_rows"] == 2
    assert len(orchestrator.retry_queue) == 2

    healthy = _ScriptedGateway()
    orchestrator.gateway = healthy  # 模拟中断恢复
    second = orchestrator.run(rows)

    assert second["summary"]["failed_rows"] == 0
    assert second["summary"]["succeeded_rows"] == 2
    assert second["summary"]["reused_rows"] == 4  # 已落库行不重复调用模型
    assert healthy.call_count == 1               # 只重跑失败批次
    assert [d["row_key"] for d in second["drafts"]] == [r["row_key"] for r in rows]
    assert all(d["state"] == "attributed_pending" for d in second["drafts"])

    pending = LedgerQueries(store).pending_attributions()
    assert len(pending) == 6
    assert all(p["state"] == "attributed_pending" for p in pending)

    # 重复执行不增行不翻倍
    third = orchestrator.run(rows)
    assert len(third["drafts"]) == 6
    assert len(LedgerQueries(store).pending_attributions()) == 6
    assert healthy.call_count == 1
    store.close()
