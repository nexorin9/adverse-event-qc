"""任务 30：错误分级 + 退避重试 ≤2 + 不可达降级信号。

六类错误码各一断言；4xx 不重试（调用计数 = 1）；5xx 最多重试 2 次；
降级标记可被业务层（degradation）识别。
"""
from __future__ import annotations

import json

import httpx
import pytest

from ae_qc.attribution.degradation import degrade_row, is_degraded
from ae_qc.attribution.model_gateway import (
    ERROR_CODES_MODEL, RETRYABLE_CODES, ModelGateway,
)
from ae_qc.errors import describe

BASE_URL = "http://model.local.test/v1"
SCHEMA = {"type": "object", "required": ["ok_flag"],
          "properties": {"ok_flag": {"type": "boolean"}}}


def _ok_response() -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps({"ok_flag": True})}}],
        "usage": {"total_tokens": 5},
    })


# 每类错误一个工厂，避免重试时复用同一个 Response 对象导致流被消费
FAILURES = {
    "CONN_FAILED": lambda: httpx.ConnectError("refused"),
    "TIMEOUT": lambda: httpx.ReadTimeout("timeout"),
    "HTTP_4XX": lambda: httpx.Response(400, json={"error": "bad request"}),
    "HTTP_5XX": lambda: httpx.Response(503, json={"error": "busy"}),
    "NOT_JSON": lambda: httpx.Response(200, text="not-json"),
    "MODEL_SCHEMA_INVALID": lambda: httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps({"wrong": 1})}}],
    }),
}


def _build(factories, max_retries: int = 2):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        item = factories[min(calls["n"] - 1, len(factories) - 1)]()
        if isinstance(item, Exception):
            raise item
        return item

    gw = ModelGateway(base_url=BASE_URL, api_key="unit-key", model="unit-model",
                      transport=httpx.MockTransport(handler), max_retries=max_retries)
    return gw, calls


@pytest.mark.parametrize("code", list(ERROR_CODES_MODEL))
def test_six_error_codes(code):
    """六类错误码各一断言：结构化返回码 + 降级标记 + 处置建议 + meta。"""
    gw, _ = _build([FAILURES[code]])
    result = gw.structured_call([{"role": "user", "content": "u"}], SCHEMA)
    gw.close()
    assert result["code"] == code
    assert result["ok"] is False
    assert result["unavailable"] is True
    assert result["data"] is None
    assert result["action"] == describe(code)["action"]
    assert result["desc"] == describe(code)["desc"]
    assert result["layer"] == describe(code)["layer"]
    assert result["meta"]["code"] == code
    assert result["meta"]["retries"] == (2 if code in RETRYABLE_CODES else 0)


def test_4xx_no_retry_call_count_is_one():
    gw, calls = _build([FAILURES["HTTP_4XX"]])
    result = gw.structured_call([{"role": "user", "content": "u"}], SCHEMA)
    gw.close()
    assert calls["n"] == 1
    assert gw.request_count == 1
    assert result["meta"]["retries"] == 0
    assert result["meta"]["backoff_ms"] == []


@pytest.mark.parametrize("code", ["HTTP_4XX", "NOT_JSON", "MODEL_SCHEMA_INVALID"])
def test_non_retryable_codes_single_call(code):
    gw, calls = _build([FAILURES[code]])
    gw.structured_call([{"role": "user", "content": "u"}], SCHEMA)
    gw.close()
    assert calls["n"] == 1


def test_5xx_retries_at_most_two():
    """持续 5xx：最多重试 2 次（共 3 次调用），退避间隔递增。"""
    gw, calls = _build([FAILURES["HTTP_5XX"]])
    result = gw.structured_call([{"role": "user", "content": "u"}], SCHEMA)
    gw.close()
    assert calls["n"] == 3
    assert gw.request_count == 3
    assert result["code"] == "HTTP_5XX"
    assert result["meta"]["retries"] == 2
    backoffs = result["meta"]["backoff_ms"]
    assert len(backoffs) == 2
    assert backoffs[1] > backoffs[0]

    # 第 3 次成功：仍不超过 2 次重试，且能正常返回 data
    gw2, calls2 = _build([FAILURES["HTTP_5XX"], FAILURES["HTTP_5XX"], _ok_response])
    ok = gw2.structured_call([{"role": "user", "content": "u"}], SCHEMA)
    gw2.close()
    assert calls2["n"] == 3
    assert ok["ok"] is True
    assert ok["data"] == {"ok_flag": True}
    assert ok["meta"]["retries"] == 2


def test_retryable_codes_are_only_network_and_5xx():
    assert RETRYABLE_CODES == frozenset({"CONN_FAILED", "TIMEOUT", "HTTP_5XX"})
    for code in ("HTTP_4XX", "NOT_JSON", "MODEL_SCHEMA_INVALID"):
        assert code not in RETRYABLE_CODES


def test_degraded_signal_recognizable_by_business_layer():
    """降级标记可被业务层识别：网关不抛栈，degradation 据此把该行归因转人工。"""
    gw, _ = _build([FAILURES["CONN_FAILED"]])
    result = gw.structured_call([{"role": "user", "content": "u"}], SCHEMA)
    gw.close()
    assert result["unavailable"] is True
    assert result["ok"] is False

    draft = degrade_row({"_row_key": "D01|2026-01|fp|7"}, result["code"])
    assert draft.state == "pending_manual_classification"
    assert is_degraded(draft) is True
    assert draft.degraded_reason == "CONN_FAILED"
    assert "模型不可用" in draft.draft_text


def test_error_codes_reuse_errors_table_actions():
    for code in ERROR_CODES_MODEL:
        assert ERROR_CODES_MODEL[code]["action"] == describe(code)["action"]
        assert ERROR_CODES_MODEL[code]["action"].strip()
