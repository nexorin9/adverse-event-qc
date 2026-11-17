"""本地协议替身工厂 —— 仅测试层，不是最终对接方式。

【重要】本模块仅用于 tests/test_writeback_contract.py 起一个 http.server 本地
替身，按 spec「出站」段校验收到的 path / headers / body 字段名与类型，用来断言
writeback.qc_client.QCClient 发出的请求逐字段符合正式接口规范。

它不是产品对接代码：真实对接以 QCClient + 环境变量 QC_BASE_URL / QC_TOKEN
为准；业务层不得依赖本模块做真实外发，本模块不依赖任何第三方 HTTP 客户端库。
替身只用标准库 http.server，绝不连真实生产端点。

三类响应：
- success    201 已受理（回执 id）；
- reject     422 业务拒绝（4xx，不重试，落冲突记录）；
- rate_limit 429 限流（不当次重试，转待写队列稍后按幂等键重放）。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..reporting.headers import FINDING_BODY_FIELDS, FORBIDDEN_FIELD_TOKENS

FINDING_PATH = "/api/v1/quality-findings"

# body 字段类型规范（按 spec「出站」段 / models.Finding）
BODY_FIELD_TYPES: dict[str, Any] = {
    "finding_id": str,
    "batch_ref": str,
    "dept_code": str,
    "period": str,
    "rule_id": str,
    "rule_set_version": str,
    "observed_value": object,             # 允许任意类型（None / str / dict / list）
    "unexpected_count": int,              # bool 不算 int
    "unexpected_samples": list,
    "attribution_draft": (str, dict),
    "attribution_confidence": (int, float),
    "status": str,
    "confirmed_by": str,
    "confirmed_at": str,
}


def _type_ok(value: Any, expected: Any) -> bool:
    if expected is object:
        return True
    types = expected if isinstance(expected, tuple) else (expected,)
    if isinstance(value, bool) and int in types and bool not in types:
        return False
    return isinstance(value, types)


def validate_request(method: str, path: str, headers: dict, body: Any) -> list[str]:
    """按 spec 校验一次出站请求，返回违规项列表（空列表＝完全合规）。

    校验 path / Authorization / Idempotency-Key(sha256 hex) / body 字段名
    （逐字段等于 14 字段白名单，缺一多一都算违规）/ 类型 / 禁用字段缺席。
    """
    violations: list[str] = []
    if method != "POST":
        violations.append(f"method 应为 POST，实为 {method!r}")
    if path != FINDING_PATH:
        violations.append(f"path 应为 {FINDING_PATH}，实为 {path!r}")
    h = {str(k).lower(): str(v) for k, v in headers.items()}
    auth = h.get("authorization", "")
    if not auth.startswith("Bearer ") or not auth[len("Bearer "):].strip():
        violations.append("缺少或非法的 Authorization: Bearer 头")
    idem = h.get("idempotency-key", "")
    if len(idem) != 64 or any(c not in "0123456789abcdef" for c in idem.lower()):
        violations.append(f"Idempotency-Key 应为 sha256 hex（64 位），实为 {idem!r}")
    if not isinstance(body, dict):
        violations.append(f"body 应为 JSON 对象，实为 {type(body).__name__}")
        return violations
    keys = set(body)
    expected = set(FINDING_BODY_FIELDS)
    missing = sorted(expected - keys)
    extra = sorted(keys - expected)
    if missing:
        violations.append(f"body 缺 spec 字段 {missing}")
    if extra:
        violations.append(f"body 出现白名单外字段 {extra}")
    for key in keys:
        low = str(key).lower()
        for token in FORBIDDEN_FIELD_TOKENS:
            if token.lower() in low:
                violations.append(f"body 出现禁用字段 {key!r}（命中 token {token!r}）")
                break
    for field, expected_type in BODY_FIELD_TYPES.items():
        if field in body and not _type_ok(body[field], expected_type):
            violations.append(
                f"body.{field} 类型应为 {expected_type}，实为 {type(body[field]).__name__}"
            )
    return violations


def make_contract_handler(mode: str = "success", *, seen: list | None = None,
                          violations: list | None = None,
                          receipt_id: str | None = None):
    """构造一个 BaseHTTPRequestHandler 子类（仅测试层，不是最终对接方式）。

    seen / violations 由调用方持有引用，用于断言替身确实按 spec 校验过请求。
    """
    seen_list = seen if seen is not None else []
    violations_list = violations if violations is not None else []
    rid = receipt_id or "EX-RECEIPT-0001"

    class ContractHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server 约定
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                body = {}
            found = validate_request("POST", self.path, dict(self.headers), body)
            seen_list.append({
                "method": "POST", "path": self.path,
                "headers": dict(self.headers), "body": body,
                "violations": list(found),
            })
            violations_list.extend(found)
            if found:
                self._respond(400, {"error": "contract_violation", "violations": found})
                return
            if mode == "success":
                self._respond(201, {"receipt_id": rid, "status": "accepted"})
            elif mode == "reject":
                self._respond(422, {"error": "business_rejected",
                                    "detail": "发现单业务校验未通过"})
            elif mode == "rate_limit":
                self._respond(429, {"error": "rate_limited", "retry_after": 1},
                              extra_headers={"Retry-After": "1"})
            else:
                self._respond(400, {"error": "unknown_contract_mode", "mode": mode})

        def _respond(self, status: int, payload: dict,
                     extra_headers: dict | None = None) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:  # 静默，避免污染 pytest 输出
            pass

    return ContractHandler


class ContractServer:
    """本地协议替身服务器（仅测试层，不是最终对接方式），支持 with 起停。"""

    def __init__(self, mode: str = "success", *, receipt_id: str | None = None):
        self.seen: list[dict] = []
        self.violations: list[str] = []
        handler = make_contract_handler(mode, seen=self.seen,
                                        violations=self.violations,
                                        receipt_id=receipt_id)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> "ContractServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


__all__ = [
    "FINDING_PATH", "BODY_FIELD_TYPES", "validate_request",
    "make_contract_handler", "ContractServer",
]
