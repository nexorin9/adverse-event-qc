"""任务 39：退避重试 ≤3 + pending_writeback 待写队列 + 队列重放不产生重复。

network 一律走 httpx.MockTransport，绝不连真实生产端点。
"""
from __future__ import annotations

import httpx

from ae_qc.ledger.idempotency import IdempotencyLedger, idempotency_key
from ae_qc.ledger.store import Store
from ae_qc.writeback.qc_client import MAX_RETRIES, QCClient
from ae_qc.writeback.queue import WritebackQueue

BASE_URL = "http://qc-platform.test"
TOKEN = "test-token"


def _finding(row_key="RK", **overrides):
    f = {
        "finding_id": "EX-F-1", "batch_ref": "D01-2026-01", "dept_code": "D01",
        "period": "2026-01", "rule_id": "AE-SEV-LEVEL",
        "rule_set_version": "v2026-01", "observed_value": "Z",
        "unexpected_count": 1, "unexpected_samples": [{"row_no": 2, "value": "Z"}],
        "attribution_draft": "拼写漂移", "attribution_confidence": 0.9,
        "status": "confirmed", "confirmed_by": "示例质控员",
        "confirmed_at": "2026-01-10T10:00:00", "row_key": row_key,
    }
    f.update(overrides)
    return f


def _key(row_key):
    return idempotency_key("D01-2026-01", "AE-SEV-LEVEL", row_key)


def test_5xx_exhausts_retries_then_pending_queue(tmp_db):
    """5xx 退避 ≤3 次后转待写队列，保留完整请求体。"""
    store = Store(tmp_db)
    client = QCClient(BASE_URL, TOKEN,
                      transport=httpx.MockTransport(lambda r: httpx.Response(503, json={})),
                      store=store)
    queue = WritebackQueue(store)
    assert queue.count() == 0
    result = client.post_finding(_finding("RK1"), row_key="RK1")
    assert result["ok"] is False
    assert result["code"] == "WRITEBACK_PENDING"
    assert result["cause"] == "WRITEBACK_5XX"
    assert result["retries"] == MAX_RETRIES
    assert client.request_count == MAX_RETRIES + 1
    assert result["enqueued"] is True
    assert queue.count() == 1
    items = queue.list()
    assert items[0]["key"] == _key("RK1")
    assert items[0]["finding"]["finding_id"] == "EX-F-1"
    assert items[0]["finding"]["status"] == "confirmed"
    assert items[0]["finding"]["row_key"] == "RK1"


def test_queue_enqueue_dedup_by_key(tmp_db):
    """同键重复入队只保留一条待写记录。"""
    store = Store(tmp_db)
    queue = WritebackQueue(store)
    key = _key("RK2")
    assert queue.enqueue(_finding("RK2"), key) is True
    assert queue.enqueue(_finding("RK2"), key) is True
    assert queue.count() == 1


def test_replay_does_not_create_duplicate(tmp_db):
    """队列重放不产生重复：同键只发一次，幂等台账只记一条。"""
    store = Store(tmp_db)
    queue = WritebackQueue(store)
    key = _key("RK3")
    queue.enqueue(_finding("RK3"), key)
    queue.enqueue(_finding("RK3"), key)  # 同键两次
    assert queue.count() == 1

    calls = []

    def ok_handler(request):
        calls.append(request)
        return httpx.Response(201, json={"receipt_id": "EX-R-REPLAY"})

    client = QCClient(BASE_URL, TOKEN, transport=httpx.MockTransport(ok_handler), store=store)
    replay = queue.replay(client)
    assert replay["sent"] == 1
    assert replay["pending"] == 0
    assert client.request_count == 1
    assert len(calls) == 1
    assert IdempotencyLedger(store).count() == 1
    # 再重放：队列已空，不再发请求
    replay2 = queue.replay(client)
    assert replay2["sent"] == 0
    assert client.request_count == 1


def test_replay_skips_already_succeeded_key(tmp_db):
    """幂等台账里已成功的键，重放直接出队，不再发请求。"""
    store = Store(tmp_db)
    queue = WritebackQueue(store)
    key = _key("RK4")
    IdempotencyLedger(store).record(key, _finding("RK4"),
                                    result={"ok": True, "receipt_id": "EX-R-DONE"})
    queue.enqueue(_finding("RK4"), key)
    assert queue.count() == 1

    calls = []

    def ok_handler(request):
        calls.append(request)
        return httpx.Response(201, json={"receipt_id": "X"})

    client = QCClient(BASE_URL, TOKEN, transport=httpx.MockTransport(ok_handler), store=store)
    replay = queue.replay(client)
    assert replay["skipped"] == 1
    assert replay["sent"] == 0
    assert client.request_count == 0
    assert len(calls) == 0
    assert queue.count() == 0


def test_unconfirmed_finding_does_not_enter_queue(tmp_db):
    """未确认／降级发现单不进队列。"""
    store = Store(tmp_db)
    queue = WritebackQueue(store)
    key = _key("RK5")
    assert queue.enqueue(_finding("RK5", status="attributed_pending"), key) is False
    assert queue.enqueue(_finding("RK5", status="collected"), key) is False
    assert queue.enqueue(_finding("RK5", degraded=True), key) is False
    assert queue.count() == 0


def test_replay_keeps_pending_on_continued_failure(tmp_db):
    """重放仍失败 → attempts+1，保留队列，不丢单。"""
    store = Store(tmp_db)
    queue = WritebackQueue(store)
    queue.enqueue(_finding("RK6"), _key("RK6"))
    client = QCClient(BASE_URL, TOKEN,
                      transport=httpx.MockTransport(lambda r: httpx.Response(503, json={})))
    replay = queue.replay(client)
    assert replay["sent"] == 0
    assert replay["pending"] == 1
    assert queue.list()[0]["attempts"] == 1
