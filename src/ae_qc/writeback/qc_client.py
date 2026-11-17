"""出站写回唯一真源：POST /api/v1/quality-findings（Bearer + 幂等键 + 14 字段 body）。

spec「出站」段：
    POST {QC_BASE_URL}/api/v1/quality-findings
    Authorization: Bearer $QC_TOKEN
    Idempotency-Key: sha256(batch_ref|rule_id|row_key)
    body = finding_id / batch_ref / dept_code / period / rule_id / rule_set_version /
           observed_value / unexpected_count / unexpected_samples / attribution_draft /
           attribution_confidence / status / confirmed_by / confirmed_at

【唯一真源】本模块是产品内**唯一** httpx 出站 POST 实现；业务层不得另写一份
httpx 出站逻辑。真实端点由环境变量 QC_BASE_URL / QC_TOKEN 提供，缺项在构造期
抛 ConfigError 并逐个点名变量名，不静默降级。测试用 httpx.MockTransport 或
tests 层 http.server 本地替身，绝不连真实生产端点。

响应处理：
- 2xx 已受理 → 取回执 id，写幂等台账 + 审计回执（回执落库可查）；
- 4xx（除 429）业务拒绝 → 不重试，落冲突记录；
- 429 限流 → 不当次重试（避免打爆），转 pending_writeback 队列稍后按幂等键重放；
- 5xx / 网络错误 → 递增退避重试 ≤3 次，耗尽转 pending_writeback 队列，
  保留完整请求体，绝不丢单。

人工确认是唯一放行口：发请求前经 writeback.guard.check_writeback 双重判定
（状态机许可 + 禁用字段），未确认／降级一律直接返回，出站请求数 = 0。
模型产出永不推进状态；年度底稿不自动外发（见 reporting.annual_draft）。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from ..config import REQUIRED_ENV
from ..errors import ConfigError
from ..ledger.audit import AuditLog
from ..ledger.idempotency import IdempotencyLedger, idempotency_key
from ..ledger.state_machine import DEGRADED_STATE
from ..ledger.store import Store, dumps, now_iso
from ..logging_util import log_event
from .guard import FINDING_BODY_FIELDS, build_body, check_writeback

FINDING_PATH = "/api/v1/quality-findings"
MAX_RETRIES = 3          # 退避 ≤3 次（共最多 MAX_RETRIES+1 次请求）
CREDENTIAL_ENV = ("QC_BASE_URL", "QC_TOKEN")
RATE_LIMIT_STATUS = 429


class QCClient:
    """质控平台出站客户端；transport 可注入 httpx.MockTransport 供测试。

    store 可选：显式传入时与 WritebackQueue / AuditLog / IdempotencyLedger 共享
    同一台账（回执落库、幂等、待写队列都可查）；未传时用进程内内存 SQLite，
    功能完整但不落用户磁盘。
    """

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 timeout: float = 10, transport: Any = None, store: Store | None = None):
        env = dict(os.environ)
        self.base_url = str(base_url if base_url is not None else env.get("QC_BASE_URL", "")).rstrip("/")
        self.token = str(token if token is not None else env.get("QC_TOKEN", ""))
        missing = [n for n, value in (("QC_BASE_URL", self.base_url), ("QC_TOKEN", self.token))
                   if not value]
        if missing:
            lines = "\n".join(f"  - {n}：{REQUIRED_ENV.get(n, '出站写回对接凭据')}" for n in missing)
            raise ConfigError(
                f"缺少必需环境变量，出站写回客户端启动中止（不静默降级）：\n{lines}"
            )
        self.timeout = float(timeout)
        self.max_retries = MAX_RETRIES
        self.request_count = 0  # 实际发出的 POST 次数，供「4xx 不重试」「未确认出站=0」断言
        self._store = store if store is not None else Store(":memory:")
        self._store.initialize()
        # trust_env=False：本地 http.server 替身不受环境 HTTP(S)_PROXY 干扰
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout,
                                    transport=transport, trust_env=False)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self) -> "QCClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @staticmethod
    def _backoff(attempt: int) -> None:
        # 递增退避；测试注入 MockTransport 时保持极短，不影响业务可观测性
        time.sleep(min(0.001 * (2 ** attempt), 0.02))

    @staticmethod
    def _receipt_id(response: httpx.Response, status: int, key: str) -> str:
        """2xx 视为已受理并取回执 id；无显式 id 时生成确定性占位，绝不静默丢单。"""
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            for name in ("receipt_id", "id", "finding_id", "quality_finding_id"):
                if payload.get(name):
                    return str(payload[name])
            data = payload.get("data")
            if isinstance(data, dict):
                for name in ("receipt_id", "id"):
                    if data.get(name):
                        return str(data[name])
        return f"HTTP-{status}-{key[:16]}"

    def _outcome(self, *, ok, code, status, receipt_id, body, key, retries=0,
                 deduplicated=False, enqueued=False, rate_limited=False,
                 cause=None, reasons=None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": ok, "code": code, "status": status, "receipt_id": receipt_id,
            "body": body, "key": key, "retries": retries,
            "deduplicated": deduplicated, "enqueued": enqueued,
            "rate_limited": rate_limited, "cause": cause,
        }
        if reasons is not None:
            out["reasons"] = reasons
        return out

    def _record_receipt(self, finding, key, status, receipt_id, outcome) -> None:
        if self._store is None:
            return
        batch_ref = str(finding.get("batch_ref", ""))
        AuditLog(self._store).receipt(batch_ref, receipt_id, status, key)
        IdempotencyLedger(self._store).record(key, finding, result=dict(outcome))
        log_event("INFO", code="WRITEBACK_OK", message="写回已受理，回执落库",
                  batch_ref=batch_ref, rule_id=str(finding.get("rule_id", "")),
                  receipt_id=receipt_id, status=status, key=key,
                  body_fields=len(FINDING_BODY_FIELDS))

    def _record_conflict(self, finding, key, status, code) -> None:
        if self._store is None:
            return
        AuditLog(self._store).write(
            "writeback_conflict", batch_ref=str(finding.get("batch_ref", "")),
            finding_id=str(finding.get("finding_id", "")),
            status_code=status, code=code, idempotency_key=key,
        )

    def _enqueue_pending(self, finding, key) -> bool:
        """5xx/网络/限流耗尽后转 pending_writeback 队列，保留完整请求体。"""
        if self._store is None:
            return False
        self._store.execute(
            "INSERT OR IGNORE INTO pending_writeback(key,finding_id,body_json,attempts,enqueued_at)"
            " VALUES (?,?,?,?,?)",
            (key, str(finding.get("finding_id", "")), dumps(finding), 0, now_iso()),
        )
        return True

    def post_finding(self, finding: dict, *, row_key: str) -> dict[str, Any]:
        """POST 一条已确认发现单；返回 {ok, code, status, receipt_id, body, key, ...}。"""
        finding = dict(finding)
        batch_ref = str(finding.get("batch_ref", ""))
        rule_id = str(finding.get("rule_id", ""))
        state = str(finding.get("status", ""))
        degraded = bool(finding.get("degraded") or finding.get("_degraded")
                        or finding.get("attribution_state") == DEGRADED_STATE)
        key = idempotency_key(batch_ref, rule_id, row_key)

        # ① 人工确认是唯一放行口：前置校验不过 → 直接返回，不发任何 HTTP 请求
        verdict = check_writeback(finding, state, degraded)
        if not verdict["allowed"]:
            code = ("WRITEBACK_DEGRADED" if degraded else
                    "WRITEBACK_NOT_CONFIRMED" if state != "confirmed" else
                    "WRITEBACK_FORBIDDEN_FIELD")
            log_event("WARN", code=code,
                      message="写回前置校验拒绝出站：" + "；".join(verdict["reasons"]),
                      batch_ref=batch_ref, rule_id=rule_id)
            return self._outcome(ok=False, code=code, status=None, receipt_id="",
                                 body={}, key=key, reasons=verdict["reasons"])

        # ② 白名单构造 body（禁用字段/白名单外字段在此抛 ValueError）
        body = build_body(finding)

        # ③ 幂等短路：同键已成功写回过 → 直接返回首次结果，不产生第二条发现单
        prior = IdempotencyLedger(self._store).get(key)
        if prior and prior["first_result"].get("ok"):
            result = dict(prior["first_result"])
            result.update({"deduplicated": True, "retries": 0,
                           "enqueued": False, "rate_limited": False})
            return result

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Idempotency-Key": key,
        }
        retries = 0
        outcome = self._outcome(ok=False, code="WRITEBACK_PENDING", status=None,
                                receipt_id="", body=body, key=key, cause="CONN_FAILED")

        for attempt in range(self.max_retries + 1):
            self.request_count += 1
            try:
                response = self._client.post(FINDING_PATH, json=body, headers=headers)
            except httpx.TimeoutException:
                outcome = self._outcome(ok=False, code="WRITEBACK_PENDING", status=None,
                                        receipt_id="", body=body, key=key,
                                        retries=retries, cause="TIMEOUT")
                if attempt < self.max_retries:
                    retries += 1
                    self._backoff(attempt)
                    continue
                break
            except Exception:
                # ConnectError / 协议错误等网络级问题
                outcome = self._outcome(ok=False, code="WRITEBACK_PENDING", status=None,
                                        receipt_id="", body=body, key=key,
                                        retries=retries, cause="CONN_FAILED")
                if attempt < self.max_retries:
                    retries += 1
                    self._backoff(attempt)
                    continue
                break

            status = response.status_code
            if 200 <= status < 300:
                receipt_id = self._receipt_id(response, status, key)
                outcome = self._outcome(ok=True, code=None, status=status,
                                        receipt_id=receipt_id, body=body, key=key,
                                        retries=retries)
                self._record_receipt(finding, key, status, receipt_id, outcome)
                break
            if status == RATE_LIMIT_STATUS:
                # 限流：不当次重试，转待写队列稍后按幂等键重放
                outcome = self._outcome(ok=False, code="WRITEBACK_PENDING", status=status,
                                        receipt_id="", body=body, key=key, retries=retries,
                                        rate_limited=True, cause="RATE_LIMITED")
                self._record_conflict(finding, key, status, "WRITEBACK_PENDING")
                break
            if 400 <= status < 500:
                # 4xx 业务拒绝：不重试，落冲突记录
                outcome = self._outcome(ok=False, code="WRITEBACK_4XX", status=status,
                                        receipt_id="", body=body, key=key, retries=retries)
                self._record_conflict(finding, key, status, "WRITEBACK_4XX")
                break
            # 5xx：可重试
            outcome = self._outcome(ok=False, code="WRITEBACK_PENDING", status=status,
                                    receipt_id="", body=body, key=key, retries=retries,
                                    cause="WRITEBACK_5XX")
            if attempt < self.max_retries:
                retries += 1
                self._backoff(attempt)
                continue
            break

        # 收尾：pending 一律入队（限流/5xx/网络），保留完整请求体，绝不丢单
        if not outcome["ok"] and outcome["code"] == "WRITEBACK_PENDING" and not outcome["enqueued"]:
            outcome["enqueued"] = self._enqueue_pending(finding, key)
        if not outcome["ok"]:
            log_event("WARN", code=outcome["code"] or "WRITEBACK_5XX",
                      message=f"写回未受理 cause={outcome['cause']} retries={outcome['retries']}",
                      batch_ref=batch_ref, rule_id=rule_id, key=key,
                      status=outcome["status"], retries=outcome["retries"],
                      enqueued=outcome["enqueued"])
        return outcome


__all__ = ["QCClient", "FINDING_PATH", "MAX_RETRIES", "CREDENTIAL_ENV", "RATE_LIMIT_STATUS"]
