"""单一模型网关：兼容 chat/completions 的结构化调用 + 分级错误 + 降级信号。

模型产出的一切都只是草稿：模型产出永不推进状态、人工确认是唯一放行口。
本层只做连接层判定（可达性 / 协议 / 响应 schema），不做任何业务判断；不可用时
返回结构化 unavailable 标记，绝不抛栈中断整批。请求体携带调用方给定的固定 JSON
schema，响应必须过同一 schema 校验，不合格即拒绝，绝不静默兜底。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

import httpx

from ..config import model_settings
from ..errors import describe
from ..logging_util import elapsed_ms_since, log_event, timed

# 六类错误码；每码的 layer/desc/action 处置建议统一复用 errors.ERROR_CODES
ERROR_CODES_MODEL: dict[str, dict[str, str]] = {
    code: describe(code)
    for code in (
        "CONN_FAILED", "TIMEOUT", "HTTP_4XX", "HTTP_5XX", "NOT_JSON", "MODEL_SCHEMA_INVALID",
    )
}

# 只有网络级错误与 5xx 可重试；4xx / 非 JSON / schema 不符不重试（重试没有意义）
RETRYABLE_CODES = frozenset({"CONN_FAILED", "TIMEOUT", "HTTP_5XX"})
_CHAT_PATH = "/chat/completions"


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def validate_schema(data: Any, schema: dict | None) -> bool:
    """轻量 JSON Schema 校验：不合格一律判 False，由调用方拒绝并降级，绝不静默兜底。

    覆盖 type / enum / const / required / properties / additionalProperties /
    items / minItems / maxItems / minLength / pattern / minimum / maximum 常用子集。
    """
    if not isinstance(schema, dict) or not schema:
        return True
    declared = schema.get("type")
    if declared:
        types = [declared] if isinstance(declared, str) else list(declared)
        if not any(_type_ok(data, t) for t in types):
            return False
    if "const" in schema and data != schema["const"]:
        return False
    if "enum" in schema and data not in schema["enum"]:
        return False
    if isinstance(data, dict):
        for key in schema.get("required", []) or []:
            if key not in data:
                return False
        props = schema.get("properties", {}) or {}
        if schema.get("additionalProperties") is False and set(data) - set(props):
            return False
        for key, subschema in props.items():
            if key in data and not validate_schema(data[key], subschema):
                return False
        extra_schema = schema.get("additionalProperties")
        if isinstance(extra_schema, dict):
            for key, value in data.items():
                if key not in props and not validate_schema(value, extra_schema):
                    return False
        if "minProperties" in schema and len(data) < schema["minProperties"]:
            return False
    if isinstance(data, list):
        if "minItems" in schema and len(data) < schema["minItems"]:
            return False
        if "maxItems" in schema and len(data) > schema["maxItems"]:
            return False
        items = schema.get("items")
        if isinstance(items, dict):
            for item in data:
                if not validate_schema(item, items):
                    return False
    if isinstance(data, str):
        if "minLength" in schema and len(data) < schema["minLength"]:
            return False
        if "pattern" in schema and not re.search(str(schema["pattern"]), data):
            return False
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        if "minimum" in schema and data < schema["minimum"]:
            return False
        if "maximum" in schema and data > schema["maximum"]:
            return False
    return True


class ModelGateway:
    """结构化模型调用唯一入口；transport 可注入 httpx.MockTransport 供测试。

    base_url / api_key / model / timeout 全部来自 model_settings()（环境变量优先），
    也可显式传参覆盖。业务模块只依赖本类，不耦合推理服务实现细节。
    """

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout: float | None = None,
                 transport: Any = None, max_retries: int = 2):
        settings = model_settings()
        self.base_url = str(base_url if base_url is not None else settings["base_url"]).rstrip("/")
        self.api_key = str(api_key if api_key is not None else settings["api_key"])
        self.model = str(model if model is not None else settings["model"]) or "ae-qc-model"
        try:
            self.timeout = float(timeout if timeout is not None else settings["timeout"]) or 30.0
        except (TypeError, ValueError):
            self.timeout = 30.0
        self.max_retries = max(0, int(max_retries))
        self.request_count = 0  # 实际发出的出站请求次数，供重试策略断言
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout, transport=transport)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self) -> "ModelGateway":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _backoff_ms(self, attempt: int) -> float:
        """递增退避（毫秒级）；返回实际退避毫秒数，计入元数据便于审计。"""
        delay = min(0.001 * (2 ** attempt), 0.02)
        time.sleep(delay)
        return round(delay * 1000, 3)

    @staticmethod
    def _meta(request_id: str, model: str, elapsed_ms: float, tokens: int,
              input_count: int, retries: int, code: str,
              backoffs: list[float]) -> dict[str, Any]:
        return {
            "request_id": request_id,
            "model": model,
            "elapsed_ms": elapsed_ms,
            "tokens": tokens,
            "input_count": input_count,
            "retries": retries,
            "code": code,
            "backoff_ms": list(backoffs),
        }

    @staticmethod
    def _finish(code: str, meta: dict, *, data: dict | None = None,
                message: str = "") -> dict[str, Any]:
        meta = dict(meta)
        meta["code"] = code
        if code:
            info = describe(code)
            log_event("WARN", code=code, message=message or info["desc"],
                      request_id=meta["request_id"], model=meta["model"],
                      retries=meta["retries"], tokens=meta["tokens"],
                      input_count=meta["input_count"], elapsed_ms=meta["elapsed_ms"])
            # 降级信号：结构化 unavailable 标记，业务层据此把该行归因转人工
            return {
                "ok": False, "data": None, "unavailable": True, "code": code,
                "meta": meta, "action": info["action"], "desc": info["desc"],
                "layer": info["layer"],
            }
        log_event("INFO", code="MODEL_CALL_OK", message="结构化模型调用成功",
                  request_id=meta["request_id"], model=meta["model"],
                  retries=meta["retries"], tokens=meta["tokens"],
                  input_count=meta["input_count"], elapsed_ms=meta["elapsed_ms"])
        return {
            "ok": True, "data": data, "unavailable": False, "code": "",
            "meta": meta, "action": "", "desc": "", "layer": "",
        }

    def structured_call(self, messages: list, schema: dict, *, request_id: str | None = None) -> dict:
        """一次结构化调用；成功返回 data，任何失败返回 unavailable 标记而非抛栈。"""
        request_id = str(request_id or uuid.uuid4().hex[:16])
        message_list = list(messages or [])
        input_count = len(message_list)
        start = timed()
        # 请求体固定：调用方给定的 JSON schema 随响应格式下发给推理端点，便于协议替身断言
        payload = {
            "model": self.model,
            "messages": message_list,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "ae_qc_structured_output",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        backoffs: list[float] = []
        retries = 0
        tokens = 0
        code = "CONN_FAILED"

        for attempt in range(self.max_retries + 1):
            self.request_count += 1
            try:
                response = self._client.post(_CHAT_PATH, json=payload, headers=headers)
            except httpx.TimeoutException:
                code = "TIMEOUT"
                if attempt < self.max_retries and code in RETRYABLE_CODES:
                    retries += 1
                    backoffs.append(self._backoff_ms(attempt))
                    continue
                break
            except Exception:
                # 连接失败 / DNS / 无效 URL / 协议错误等网络级问题统一 CONN_FAILED
                code = "CONN_FAILED"
                if attempt < self.max_retries and code in RETRYABLE_CODES:
                    retries += 1
                    backoffs.append(self._backoff_ms(attempt))
                    continue
                break

            status = response.status_code
            if 400 <= status < 500:
                code = "HTTP_4XX"  # 业务拒绝：不重试
                break
            if status >= 500:
                code = "HTTP_5XX"
                if attempt < self.max_retries and code in RETRYABLE_CODES:
                    retries += 1
                    backoffs.append(self._backoff_ms(attempt))
                    continue
                break

            try:
                envelope = response.json()
            except ValueError:
                code = "NOT_JSON"
                break
            content = None
            if isinstance(envelope, dict):
                usage = envelope.get("usage") or {}
                if isinstance(usage, dict):
                    try:
                        tokens = int(usage.get("total_tokens") or 0)
                    except (TypeError, ValueError):
                        tokens = 0
                choices = envelope.get("choices")
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    message = choices[0].get("message") or {}
                    if isinstance(message, dict):
                        content = message.get("content")
                if content is None and "data" in envelope and "choices" not in envelope:
                    content = envelope.get("data")  # 兼容简化替身直接给出 data 对象

            if isinstance(content, str):
                try:
                    data = json.loads(content)
                except ValueError:
                    code = "NOT_JSON"
                    break
            elif isinstance(content, dict):
                data = content
            else:
                code = "NOT_JSON"
                break

            if not validate_schema(data, schema):
                code = "MODEL_SCHEMA_INVALID"  # 不静默兜底，按 schema 不合格处理
                break

            meta = self._meta(request_id, self.model, elapsed_ms_since(start), tokens,
                              input_count, retries, "", backoffs)
            return self._finish("", meta, data=data)

        meta = self._meta(request_id, self.model, elapsed_ms_since(start), tokens,
                          input_count, retries, code, backoffs)
        return self._finish(code, meta,
                            message=f"结构化模型调用失败 code={code} retries={retries}")


__all__ = ["ModelGateway", "validate_schema", "ERROR_CODES_MODEL", "RETRYABLE_CODES"]
