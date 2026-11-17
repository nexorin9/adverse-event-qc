"""入站 D 平台客户端专项测试：请求规范、六类错误码、重试策略、回落与去重。

网络一律走 httpx.MockTransport / 本地替身，绝不连真实端点。
"""
from __future__ import annotations

import httpx
import pytest

from ae_qc.errors import ERROR_CODES, ConfigError
from ae_qc.ingest.ae_platform_client import (
    AEPlatformClient, ERROR_CODES_INGEST, fallback_to_files,
)

BASE_URL = "http://ae-platform.test"
TOKEN = "test-token"


def _row(n: int, dept: str = "D01") -> dict:
    """符合 spec 入站 B 字段规范的一行（11 个标准列）。"""
    return {
        "incident_id": f"<EXAMPLE_LOT>-{dept}-{n:04d}",
        "dept_code": dept,
        "event_type_text": "跌倒",
        "event_type_code": "AE-CAT-08",
        "severity_level": "A",
        "severity_class": "Ⅳ",
        "occur_time": "2026-01-02 10:00:00",
        "report_time": "2026-01-02 11:00:00",
        "reporter_role": "示例安全员",
        "harm_desc": "未见影响",
        "root_cause_text": "地面湿滑",
    }


def _client(handler) -> AEPlatformClient:
    return AEPlatformClient(BASE_URL, TOKEN, transport=httpx.MockTransport(handler))


def test_get_path_params_and_bearer_header():
    """GET /api/v1/incident-reports + period/dept_code 参数 + Bearer 头。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"rows": [_row(1), _row(2)]})

    client = _client(handler)
    result = client.fetch("2026-01", "D01")
    assert result["ok"] is True
    assert result["code"] is None
    assert result["status"] == 200
    assert len(result["rows"]) == 2
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/v1/incident-reports"
    assert dict(seen[0].url.params) == {"period": "2026-01", "dept_code": "D01"}
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"


def test_platform_rows_carry_source_metadata():
    """平台来源行补来源定位元数据（下划线前缀，不参与业务规则）。"""
    # 正式规范要求平台按 dept_code 返回该科室数据：响应行 dept_code 与请求一致
    client = _client(lambda req: httpx.Response(200, json={"rows": [_row(1, dept="D03")]}))
    row = client.fetch("2026-02", "D03")["rows"][0]
    assert row["_source"] == "platform"
    assert row["_file_name"] == "platform"
    assert row["_dept_code"] == "D03"
    assert row["_period"] == "2026-02"
    assert row["_batch_ref"] == "D03-2026-02"
    assert row["_row_no"] == 1


def _raise(exc: Exception):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc
    return handler


@pytest.mark.parametrize("handler,expected_code", [
    (_raise(httpx.ConnectError("connection refused")), "CONN_FAILED"),
    (_raise(httpx.ReadTimeout("timeout")), "TIMEOUT"),
    (lambda req: httpx.Response(403, json={}), "HTTP_4XX"),
    (lambda req: httpx.Response(503, json={}), "HTTP_5XX"),
    (lambda req: httpx.Response(200, text="not-json-at-all"), "NOT_JSON"),
    (lambda req: httpx.Response(200, json={"rows": [{"incident_id": "x", "extra": 1}]}),
     "SCHEMA_INVALID"),
])
def test_six_error_codes(handler, expected_code):
    """五类（含六码）错误各返回结构化错误码，永不抛栈。"""
    client = _client(handler)
    result = client.fetch("2026-01", "D01")
    assert result["ok"] is False
    assert result["rows"] == []
    assert result["code"] == expected_code


def test_4xx_does_not_retry():
    """4xx 业务拒绝不重试：调用计数恰为 1。"""
    client = _client(lambda req: httpx.Response(401, json={"error": "unauthorized"}))
    result = client.fetch("2026-01", "D01")
    assert result["code"] == "HTTP_4XX"
    assert result["status"] == 401
    assert client.request_count == 1


@pytest.mark.parametrize("handler,expected_code", [
    (_raise(httpx.ReadTimeout("timeout")), "TIMEOUT"),
    (lambda req: httpx.Response(500, json={}), "HTTP_5XX"),
    (_raise(httpx.ConnectError("down")), "CONN_FAILED"),
])
def test_retryable_errors_retry_at_most_twice(handler, expected_code):
    """超时 / 5xx / 连接失败重试 ≤2 次（共 3 次调用），调用计数可断言。"""
    client = _client(handler)
    result = client.fetch("2026-01", "D01")
    assert result["code"] == expected_code
    assert client.request_count == 3


def test_5xx_then_success_stops_retrying():
    """5xx 重试后成功即停止，不多发请求。"""
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"rows": [_row(1)]})

    client = _client(handler)
    result = client.fetch("2026-01", "D01")
    assert result["ok"] is True
    assert client.request_count == 3
    assert len(result["rows"]) == 1


@pytest.mark.parametrize("payload", [
    {"rows": [{"incident_id": "x", "dept_code": "D01"}]},  # 缺 occur_time / report_time
    {"rows": [dict(_row(1), unexpected_field="未登记")]},    # 多出标准列外字段
    {"rows": [dict(_row(1), harm_desc={"nested": "object"})]},  # 类型不符
    {"data": []},                                          # 缺 rows 字段
])
def test_schema_invalid_on_missing_extra_or_bad_type(payload):
    """响应缺列 / 多列 / 类型不符 / 缺 rows → SCHEMA_INVALID，不静默补空。"""
    client = _client(lambda req: httpx.Response(200, json=payload))
    result = client.fetch("2026-01", "D01")
    assert result["ok"] is False
    assert result["code"] == "SCHEMA_INVALID"
    assert result["rows"] == []


def test_fallback_to_files_marks_files_only_note():
    """平台不可达 → 回落文件来源，摘要标注「本批仅含文件来源」。"""
    frames = [{
        "dept_code": "D01", "period": "2026-01",
        "rows": [dict(_row(2), _source="file"), dict(_row(3), _source="file")],
    }]
    client = _client(_raise(httpx.ConnectError("down")))
    result = fallback_to_files(client, frames, "2026-01", "D01")
    assert result["source"] == "files"
    assert result["note"] == "本批仅含文件来源"
    assert result["summary"]["note"] == "本批仅含文件来源"
    assert result["summary"]["platform_ok"] is False
    assert result["platform_row_count"] == 0
    assert len(result["rows"]) == 2


def test_fallback_dedup_by_dept_period_incident_id():
    """平台+文件混合时按 (dept_code, period, incident_id) 去重，平台优先。"""
    frames = [{
        "dept_code": "D01", "period": "2026-01",
        "rows": [dict(_row(2), _source="file"), dict(_row(3), _source="file")],
    }]

    def handler(request):
        return httpx.Response(200, json={"rows": [_row(1), _row(2)]})

    result = fallback_to_files(_client(handler), frames, "2026-01", "D01")
    assert result["source"] == "mixed"
    assert result["note"] == ""
    assert result["duplicate_count"] == 1
    assert len(result["rows"]) == 3  # EX-1(平台), EX-2(平台), EX-3(文件)
    assert result["summary"]["source_mix"] == {"platform": 2, "file": 1}


def test_fallback_ignores_other_dept_period_frames():
    """回落只合并本 (dept_code, period) 的文件来源行。"""
    frames = [
        {"dept_code": "D02", "period": "2026-01", "rows": [dict(_row(9), _source="file")]},
        {"dept_code": "D01", "period": "2026-02", "rows": [dict(_row(9), _source="file")]},
        {"dept_code": "D01", "period": "2026-01", "rows": [dict(_row(1), _source="file")]},
    ]
    client = _client(_raise(httpx.ConnectError("down")))
    result = fallback_to_files(client, frames, "2026-01", "D01")
    assert len(result["rows"]) == 1
    assert result["summary"]["file_rows"] == 1


def test_error_codes_ingest_constant():
    """ERROR_CODES_INGEST 覆盖六码且与统一错误码表同源。"""
    assert set(ERROR_CODES_INGEST) == {
        "CONN_FAILED", "TIMEOUT", "HTTP_4XX", "HTTP_5XX", "NOT_JSON", "SCHEMA_INVALID",
    }
    for code in ERROR_CODES_INGEST:
        assert code in ERROR_CODES
        assert ERROR_CODES_INGEST[code]["desc"]


def test_missing_credentials_raises_config_error(monkeypatch):
    """凭据缺失抛 ConfigError 并逐个点名变量名，不静默降级。"""
    monkeypatch.delenv("AE_BASE_URL", raising=False)
    monkeypatch.delenv("AE_API_TOKEN", raising=False)
    with pytest.raises(ConfigError) as exc_info:
        AEPlatformClient()
    message = str(exc_info.value)
    assert "AE_BASE_URL" in message
    assert "AE_API_TOKEN" in message
