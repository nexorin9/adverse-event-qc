"""任务 37：出站写回客户端专项测试（请求规范 / body 字段 / 4xx 不重试）。

网络一律走 httpx.MockTransport，绝不连真实生产端点。
"""
from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from ae_qc.errors import ConfigError
from ae_qc.ledger.idempotency import idempotency_key
from ae_qc.ledger.store import Store
from ae_qc.reporting.headers import FINDING_BODY_FIELDS
from ae_qc.writeback.qc_client import FINDING_PATH, MAX_RETRIES, QCClient

BASE_URL = "http://qc-platform.test"
TOKEN = "test-token"

# spec「出站」段 14 字段，逐字段写死，用于独立断言（不依赖被测代码常量）
SPEC_FIELDS = (
    "finding_id", "batch_ref", "dept_code", "period", "rule_id",
    "rule_set_version", "observed_value", "unexpected_count",
    "unexpected_samples", "attribution_draft", "attribution_confidence",
    "status", "confirmed_by", "confirmed_at",
)


def _finding(**overrides):
    """一条已确认发现单（spec 14 字段 + 仅用于算幂等键的 row_key）。"""
    f = {
        "finding_id": "EX-F-1",
        "batch_ref": "D01-2026-01",
        "dept_code": "D01",
        "period": "2026-01",
        "rule_id": "AE-SEV-LEVEL",
        "rule_set_version": "v2026-01",
        "observed_value": "Z",
        "unexpected_count": 1,
        "unexpected_samples": [{"row_no": 2, "value": "Z", "expected": "A–I"}],
        "attribution_draft": "拼写漂移，建议改 A",
        "attribution_confidence": 0.9,
        "status": "confirmed",
        "confirmed_by": "示例质控员",
        "confirmed_at": "2026-01-10T10:00:00",
        "row_key": "ROW-1",
    }
    f.update(overrides)
    return f


def _client(handler, **kw):
    return QCClient(BASE_URL, TOKEN, transport=httpx.MockTransport(handler), **kw)


def test_authorization_and_idempotency_key_headers():
    """POST + Bearer 头 + Idempotency-Key = sha256(batch_ref|rule_id|row_key)。"""
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(201, json={"receipt_id": "EX-R-1"})

    client = _client(handler)
    result = client.post_finding(_finding(row_key="ROW-KEY-1"), row_key="ROW-KEY-1")
    assert result["ok"] is True
    req = seen[0]
    assert req.method == "POST"
    assert req.url.path == FINDING_PATH
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    assert "Idempotency-Key" in req.headers
    expected = hashlib.sha256(
        "D01-2026-01|AE-SEV-LEVEL|ROW-KEY-1".encode("utf-8")
    ).hexdigest()
    assert req.headers["Idempotency-Key"] == expected
    assert req.headers["Idempotency-Key"] == idempotency_key(
        "D01-2026-01", "AE-SEV-LEVEL", "ROW-KEY-1")


def test_body_fields_match_spec_exactly():
    """body 字段与 spec 逐字段一致（14 字段、顺序、值；row_key 不进 body）。"""
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(201, json={"receipt_id": "EX-R-1"})

    client = _client(handler)
    finding = _finding(row_key="ROW-BODY")
    result = client.post_finding(finding, row_key="ROW-BODY")
    body = json.loads(seen[0].content.decode("utf-8"))
    assert tuple(body) == SPEC_FIELDS
    assert set(body) == set(FINDING_BODY_FIELDS)
    assert len(body) == 14
    for field in SPEC_FIELDS:
        assert field in body
    assert body["finding_id"] == "EX-F-1"
    assert body["batch_ref"] == "D01-2026-01"
    assert body["status"] == "confirmed"
    assert body["unexpected_samples"] == finding["unexpected_samples"]
    assert "row_key" not in body
    assert result["body"] == body


def test_2xx_takes_receipt_id():
    """2xx 视为已受理并取回执 id。"""
    client = _client(lambda r: httpx.Response(201, json={"receipt_id": "EX-R-77"}))
    result = client.post_finding(_finding(row_key="K2"), row_key="K2")
    assert result["ok"] is True
    assert result["code"] is None
    assert result["status"] == 201
    assert result["receipt_id"] == "EX-R-77"
    assert result["key"] == idempotency_key("D01-2026-01", "AE-SEV-LEVEL", "K2")


def test_4xx_does_not_retry():
    """4xx 业务拒绝不重试：请求计数恰为 1。"""
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(422, json={"error": "business_rejected"})

    client = _client(handler)
    result = client.post_finding(_finding(row_key="K3"), row_key="K3")
    assert result["ok"] is False
    assert result["code"] == "WRITEBACK_4XX"
    assert result["status"] == 422
    assert client.request_count == 1
    assert len(calls) == 1


def test_5xx_retries_at_most_three_then_pending():
    """5xx 递增退避 ≤3 次，耗尽转 pending_writeback。"""
    client = _client(lambda r: httpx.Response(503, json={}))
    result = client.post_finding(_finding(row_key="K4"), row_key="K4")
    assert result["ok"] is False
    assert result["code"] == "WRITEBACK_PENDING"
    assert result["cause"] == "WRITEBACK_5XX"
    assert result["retries"] == MAX_RETRIES
    assert client.request_count == MAX_RETRIES + 1


def test_5xx_then_success_stops_retrying():
    """5xx 重试后成功即停止，不多发请求。"""
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503, json={})
        return httpx.Response(201, json={"receipt_id": "EX-R-5"})

    client = _client(handler)
    result = client.post_finding(_finding(row_key="K5"), row_key="K5")
    assert result["ok"] is True
    assert result["receipt_id"] == "EX-R-5"
    assert client.request_count == 3


def test_idempotent_resend_returns_first_result(tmp_db):
    """同键重发只产生一条发现单：第二次直接返回首次回执，不再发请求。"""
    from ae_qc.ledger.idempotency import IdempotencyLedger

    store = Store(tmp_db)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(201, json={"receipt_id": "EX-R-IDEM"})

    client = QCClient(BASE_URL, TOKEN, transport=httpx.MockTransport(handler), store=store)
    first = client.post_finding(_finding(row_key="K6"), row_key="K6")
    second = client.post_finding(_finding(row_key="K6"), row_key="K6")
    assert first["ok"] is True and second["ok"] is True
    assert second["deduplicated"] is True
    assert second["receipt_id"] == first["receipt_id"]
    assert client.request_count == 1
    assert len(calls) == 1
    assert IdempotencyLedger(store).count() == 1


def test_missing_credentials_raises_config_error(monkeypatch):
    """凭据缺失抛 ConfigError 并逐个点名变量名，不静默降级。"""
    monkeypatch.delenv("QC_BASE_URL", raising=False)
    monkeypatch.delenv("QC_TOKEN", raising=False)
    with pytest.raises(ConfigError) as exc:
        QCClient()
    message = str(exc.value)
    assert "QC_BASE_URL" in message
    assert "QC_TOKEN" in message


def test_partially_missing_credentials_names_only_missing_var(monkeypatch):
    """只缺 QC_TOKEN 时报错点名 QC_TOKEN。"""
    monkeypatch.setenv("QC_BASE_URL", "http://qc.test")
    monkeypatch.delenv("QC_TOKEN", raising=False)
    with pytest.raises(ConfigError) as exc:
        QCClient()
    assert "QC_TOKEN" in str(exc.value)
