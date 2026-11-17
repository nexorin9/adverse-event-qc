"""任务 29：单一模型网关的本地协议替身验证。

验证结构化请求体与 schema 校验；超时 / 4xx / 5xx / 非 JSON / schema 不符
五条错误路径各一断言；grep 断言 attribution/ 下无厂商 SDK import。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from ae_qc.attribution import model_gateway as mg
from ae_qc.attribution.model_gateway import (
    ERROR_CODES_MODEL, ModelGateway, validate_schema,
)

BASE_URL = "http://model.local.test/v1"
SCHEMA = {
    "type": "object",
    "required": ["code", "confidence"],
    "properties": {
        "code": {"type": "string"},
        "confidence": {"type": "number"},
    },
}


def _content_response(data: dict, tokens: int = 17) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps(data, ensure_ascii=False)}}],
        "usage": {"total_tokens": tokens},
    })


def _make_gateway(handler, **kw) -> ModelGateway:
    return ModelGateway(base_url=BASE_URL, api_key="unit-key", model="unit-model",
                        transport=httpx.MockTransport(handler), **kw)


def test_structured_request_body_and_schema_validation():
    """请求体固定携带给定 JSON schema；合法响应过 schema 并返回齐备 meta。"""
    captured: list[httpx.Request] = []

    def handler(request):
        captured.append(request)
        return _content_response({"code": "AE-CAT-08", "confidence": 0.91}, tokens=23)

    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    gw = _make_gateway(handler)
    result = gw.structured_call(messages, SCHEMA, request_id="req-1")
    gw.close()

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/v1/chat/completions"
    assert req.headers["Authorization"] == "Bearer unit-key"
    body = json.loads(req.content)
    assert body["model"] == "unit-model"
    assert body["messages"] == messages
    assert body["stream"] is False
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA

    assert result["ok"] is True
    assert result["data"] == {"code": "AE-CAT-08", "confidence": 0.91}
    assert result["unavailable"] is False
    assert result["code"] == ""
    meta = result["meta"]
    for field in ("request_id", "model", "elapsed_ms", "tokens", "input_count", "retries", "code"):
        assert field in meta
    assert meta["request_id"] == "req-1"
    assert meta["model"] == "unit-model"
    assert meta["tokens"] == 23
    assert meta["input_count"] == 2
    assert meta["retries"] == 0
    assert meta["elapsed_ms"] >= 0


def test_validate_schema_rejects_bad_shapes():
    """schema 校验本身：缺列 / 类型不符 / 枚举外 / 额外属性 / 数组元素 / 数值范围。"""
    strict = {
        "type": "object", "required": ["a", "b"], "additionalProperties": False,
        "properties": {"a": {"type": "integer"}, "b": {"enum": ["x", "y"]}},
    }
    assert validate_schema({"a": 1, "b": "x"}, strict) is True
    assert validate_schema({"a": 1}, strict) is False
    assert validate_schema({"a": "1", "b": "x"}, strict) is False
    assert validate_schema({"a": 1, "b": "z"}, strict) is False
    assert validate_schema({"a": 1, "b": "x", "c": 0}, strict) is False
    assert validate_schema(
        {"items": [1, 2]},
        {"type": "object", "properties": {"items": {"type": "array",
                                                    "items": {"type": "integer"}, "minItems": 3}}},
    ) is False
    assert validate_schema(1.5, {"type": "number", "minimum": 0, "maximum": 1}) is False


@pytest.mark.parametrize("make_item, expected_code", [
    (lambda: httpx.ReadTimeout("timeout"), "TIMEOUT"),
    (lambda: httpx.Response(401, json={"error": "unauthorized"}), "HTTP_4XX"),
    (lambda: httpx.Response(503, json={"error": "busy"}), "HTTP_5XX"),
    (lambda: httpx.Response(200, text="not-a-json-body"), "NOT_JSON"),
])
def test_four_http_error_paths(make_item, expected_code):
    """超时 / 4xx / 5xx / 非 JSON 四条错误路径各一断言。"""
    def handler(request):
        item = make_item()
        if isinstance(item, Exception):
            raise item
        return item

    gw = _make_gateway(handler)
    result = gw.structured_call([{"role": "user", "content": "u"}], SCHEMA)
    gw.close()
    assert result["code"] == expected_code
    assert result["ok"] is False
    assert result["unavailable"] is True
    assert result["data"] is None
    assert result["action"]  # 处置建议非空（复用 errors.ERROR_CODES）


def test_schema_mismatch_path():
    """第五条错误路径：200 + 合法 JSON 但不符合固定 schema → MODEL_SCHEMA_INVALID，不静默兜底。"""
    def handler(request):
        return _content_response({"code": 123})  # 缺 confidence 且 code 类型不符

    gw = _make_gateway(handler)
    result = gw.structured_call([{"role": "user", "content": "u"}], SCHEMA)
    gw.close()
    assert result["code"] == "MODEL_SCHEMA_INVALID"
    assert result["ok"] is False
    assert result["unavailable"] is True
    assert result["data"] is None
    assert gw.request_count == 1  # schema 不符不重试


def test_no_vendor_sdk_import_in_attribution():
    """grep 断言：attribution/ 产品代码不得 import 任何厂商 SDK，只能走 httpx。"""
    root = Path(mg.__file__).parent
    files = sorted(root.glob("*.py"))
    assert len(files) >= 8, "应有 7 个实现文件 + __init__.py"
    vendor = re.compile(
        r"^\s*(?:from|import)\s+(?:openai|anthropic|google\.generativeai|cohere|"
        r"mistralai|dashscope|zhipuai|qwen|baidu)\b", re.M,
    )
    offenders = [f"{p.name}: {m.group(0).strip()}" for p in files
                 for m in vendor.finditer(p.read_text(encoding="utf-8"))]
    assert offenders == []
    assert "import httpx" in (root / "model_gateway.py").read_text(encoding="utf-8")


def test_error_code_table_exposes_actions():
    assert set(ERROR_CODES_MODEL) == {
        "CONN_FAILED", "TIMEOUT", "HTTP_4XX", "HTTP_5XX", "NOT_JSON", "MODEL_SCHEMA_INVALID",
    }
    for code, info in ERROR_CODES_MODEL.items():
        assert info["layer"] and info["desc"] and info["action"]
