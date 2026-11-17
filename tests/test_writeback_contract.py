"""任务 40：对接规范测试（本地 http.server 替身按 spec 校验 body 字段名与类型）。

替身来自 writeback.contract（仅测试层，不是最终对接方式），绝不连真实生产端点。
覆盖成功 / 业务拒绝 / 限流三分支，并断言写回回执落库可查。
"""
from __future__ import annotations

import httpx

from ae_qc.ledger.audit import AuditLog
from ae_qc.ledger.idempotency import IdempotencyLedger, idempotency_key
from ae_qc.ledger.store import Store
from ae_qc.reporting.headers import FINDING_BODY_FIELDS
from ae_qc.writeback.contract import (
    ContractServer, FINDING_PATH, validate_request,
)
from ae_qc.writeback.qc_client import QCClient
from ae_qc.writeback.queue import WritebackQueue

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


def _client(server, store):
    return QCClient(server.base_url, TOKEN, store=store)


def _headers_lower(record):
    return {str(k).lower(): str(v) for k, v in record["headers"].items()}


def test_contract_validator_checks_field_names_and_types():
    """替身确实按 spec 校验：path/头/body 字段名与类型/禁用字段都逐项检查。"""
    headers = {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": _key("K")}
    body = _finding("K")
    body.pop("row_key")  # 出站 body 严格 14 字段
    assert set(body) == set(FINDING_BODY_FIELDS)
    assert validate_request("POST", FINDING_PATH, headers, body) == []

    missing = dict(body)
    missing.pop("confirmed_at")
    assert any("缺" in v for v in validate_request("POST", FINDING_PATH, headers, missing))

    extra = dict(body, score=1)
    violations = validate_request("POST", FINDING_PATH, headers, extra)
    assert any("白名单外" in v for v in violations)
    assert any("score" in v for v in violations)

    bad_type = dict(body, unexpected_count="1")
    assert any("unexpected_count" in v and "类型" in v
               for v in validate_request("POST", FINDING_PATH, headers, bad_type))

    assert any("Authorization" in v
               for v in validate_request("POST", FINDING_PATH, {}, body))
    assert any("Idempotency-Key" in v for v in validate_request(
        "POST", FINDING_PATH, {"Authorization": f"Bearer {TOKEN}"}, body))
    assert any("path" in v for v in validate_request("POST", "/wrong/path", headers, body))
    assert any("method" in v for v in validate_request("GET", FINDING_PATH, headers, body))


def test_success_branch_and_receipt_persisted(tmp_db):
    """成功分支：替身校验通过，回执落台账与审计流水可查。"""
    store = Store(tmp_db)
    with ContractServer("success", receipt_id="EX-R-OK") as server:
        client = _client(server, store)
        result = client.post_finding(_finding("K-SUCCESS"), row_key="K-SUCCESS")
    assert result["ok"] is True
    assert result["status"] == 201
    assert result["receipt_id"] == "EX-R-OK"
    assert server.violations == []          # 替身按 spec 校验 body 无违规
    assert len(server.seen) == 1
    seen = server.seen[0]
    assert seen["path"] == FINDING_PATH
    assert _headers_lower(seen)["authorization"] == f"Bearer {TOKEN}"
    assert _headers_lower(seen)["idempotency-key"] == _key("K-SUCCESS")
    assert set(seen["body"]) == set(FINDING_BODY_FIELDS)

    events = AuditLog(store).export("D01-2026-01")
    receipts = [e for e in events if e["event_type"] == "writeback_receipt"]
    assert len(receipts) == 1
    assert receipts[0]["payload"]["receipt_id"] == "EX-R-OK"
    assert receipts[0]["payload"]["status_code"] == 201
    assert receipts[0]["payload"]["idempotency_key"] == _key("K-SUCCESS")
    assert IdempotencyLedger(store).get(_key("K-SUCCESS")) is not None


def test_reject_branch_no_retry_and_conflict_recorded(tmp_db):
    """业务拒绝（422）：不重试，落冲突记录，不记成功幂等。"""
    store = Store(tmp_db)
    with ContractServer("reject") as server:
        client = _client(server, store)
        result = client.post_finding(_finding("K-REJECT"), row_key="K-REJECT")
    assert result["ok"] is False
    assert result["code"] == "WRITEBACK_4XX"
    assert result["status"] == 422
    assert client.request_count == 1
    assert server.violations == []
    conflicts = [e for e in AuditLog(store).export("D01-2026-01")
                 if e["event_type"] == "writeback_conflict"]
    assert len(conflicts) == 1
    assert conflicts[0]["payload"]["code"] == "WRITEBACK_4XX"
    assert IdempotencyLedger(store).get(_key("K-REJECT")) is None


def test_rate_limit_branch_goes_to_pending_queue(tmp_db):
    """限流（429）：不当次重试，转待写队列，不丢单。"""
    store = Store(tmp_db)
    with ContractServer("rate_limit") as server:
        client = _client(server, store)
        result = client.post_finding(_finding("K-RATE"), row_key="K-RATE")
    assert result["ok"] is False
    assert result["status"] == 429
    assert result["rate_limited"] is True
    assert result["code"] == "WRITEBACK_PENDING"
    assert client.request_count == 1
    assert result["enqueued"] is True
    assert server.violations == []
    queue = WritebackQueue(store)
    assert queue.count() == 1
    assert queue.list()[0]["key"] == _key("K-RATE")


def test_rate_limited_finding_replays_later(tmp_db):
    """限流入队的发现单恢复后按幂等键重放成功，只产生一条发现单。"""
    store = Store(tmp_db)
    with ContractServer("rate_limit") as server:
        c1 = _client(server, store)
        c1.post_finding(_finding("K-LATER"), row_key="K-LATER")
    queue = WritebackQueue(store)
    assert queue.count() == 1

    calls = []

    def ok_handler(request):
        calls.append(request)
        return httpx.Response(201, json={"receipt_id": "EX-R-LATER"})

    c2 = QCClient("http://qc-platform.test", TOKEN,
                  transport=httpx.MockTransport(ok_handler), store=store)
    replay = queue.replay(c2)
    assert replay["sent"] == 1
    assert replay["pending"] == 0
    assert len(calls) == 1
    assert IdempotencyLedger(store).get(_key("K-LATER")) is not None
