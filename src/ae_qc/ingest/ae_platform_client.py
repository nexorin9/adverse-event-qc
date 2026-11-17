"""入站 D：院内不良事件报告平台只读客户端（GET · 错误分级 · 不可达回落文件来源）。

正式对接规范（spec 入站 B）：
    GET {AE_BASE_URL}/api/v1/incident-reports?period={yyyy-mm}&dept_code={dept_code}
    Authorization: Bearer {AE_API_TOKEN}
响应体字段与标准列一致；本产品只读，不写入上报平台。

业务口径：
- 六类错误码 CONN_FAILED / TIMEOUT / HTTP_4XX / HTTP_5XX / NOT_JSON / SCHEMA_INVALID。
- 超时与 5xx 重试 ≤2 次（共最多 3 次调用，调用计数可断言），4xx 不重试。
- 响应缺列 / 多列 / 类型不符返回 SCHEMA_INVALID，不静默补空。
- 平台不可达时回落 xlsx 文件来源，并在摘要标注「本批仅含文件来源」，
  两种来源的行按 (dept_code, period, incident_id) 去重（HTTP 为主，平台优先）。

272 号文：本层只读入站数据，不产出排名/扣分，不落患者可识别明细。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from ..config import REQUIRED_ENV
from ..errors import ConfigError, describe
from ..logging_util import log_event
from ..models import REQUIRED_COLUMNS, STANDARD_COLUMNS

# 本客户端可返回的入站错误码（与统一错误码表同源，便于 doctor 与摘要复用）
ERROR_CODES_INGEST: dict[str, dict[str, str]] = {
    code: describe(code)
    for code in ("CONN_FAILED", "TIMEOUT", "HTTP_4XX", "HTTP_5XX", "NOT_JSON", "SCHEMA_INVALID")
}

_REPORT_PATH = "/api/v1/incident-reports"
_ALLOWED_COLUMNS = set(STANDARD_COLUMNS)


def _validate_payload(payload: Any) -> dict:
    """按正式对接规范校验响应：缺列 / 多列 / 类型不符一律 SCHEMA_INVALID。"""
    if isinstance(payload, dict):
        rows = payload.get("rows", None)
    elif isinstance(payload, list):
        rows = payload
    else:
        return {"ok": False, "error": "响应顶层必须是对象（含 rows）或数组"}
    if rows is None:
        return {"ok": False, "error": "响应缺少 rows 字段"}
    if not isinstance(rows, list):
        return {"ok": False, "error": "rows 必须是数组"}
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            return {"ok": False, "error": f"rows[{idx}] 不是对象"}
        keys = set(row.keys())
        extra = keys - _ALLOWED_COLUMNS
        missing = set(REQUIRED_COLUMNS) - keys
        if missing:
            return {"ok": False, "error": f"rows[{idx}] 缺必需字段 {sorted(missing)}"}
        if extra:
            return {"ok": False, "error": f"rows[{idx}] 出现标准列外字段 {sorted(extra)}"}
        for column, value in row.items():
            if isinstance(value, (dict, list, bool)):
                return {"ok": False, "error": f"rows[{idx}].{column} 类型不符"}
    return {"ok": True, "rows": rows}


def _attach_meta(rows: list[dict], period: str, dept_code: str) -> list[dict]:
    """给平台来源行补来源定位元数据（下划线前缀，不参与业务规则）。"""
    out: list[dict] = []
    for idx, row in enumerate(rows, start=1):
        record = dict(row)
        record["_row_no"] = idx
        record["_row_key"] = f"{dept_code}|{period}|platform|r{idx}"
        record["_file_name"] = "platform"
        record["_sheet_name"] = "platform"
        record["_dept_code"] = str(row.get("dept_code") or dept_code)
        record["_period"] = period
        record["_batch_ref"] = f"{dept_code}-{period}"
        record["_source"] = "platform"
        out.append(record)
    return out


def _frame_view(frame: Any) -> dict:
    """BatchFrame 或 dict 统一视图（回落合并所需字段）。"""
    if isinstance(frame, dict):
        return {
            "dept_code": str(frame.get("dept_code", "")),
            "period": str(frame.get("period", "")),
            "rows": list(frame.get("rows", []) or []),
        }
    return {
        "dept_code": str(getattr(frame, "dept_code", "")),
        "period": str(getattr(frame, "period", "")),
        "rows": list(getattr(frame, "rows", []) or []),
    }


def _dedup(rows: list[dict], period: str, dept_code: str) -> tuple[list[dict], int]:
    """按 (dept_code, period, incident_id) 去重；平台来源在前，优先保留。"""
    seen: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    duplicates = 0
    for row in rows:
        dept = str(row.get("_dept_code") or row.get("dept_code") or dept_code)
        per = str(row.get("_period") or period)
        incident = str(row.get("incident_id") or row.get("_row_key") or "")
        key = (dept, per, incident)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        out.append(row)
    return out, duplicates


class AEPlatformClient:
    """上报平台只读客户端；transport 可注入 httpx.MockTransport 供测试，绝不连真实端点。"""

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 timeout: float = 10, transport: Any = None, max_retries: int = 2):
        env = dict(os.environ)
        self.base_url = str(base_url if base_url is not None else env.get("AE_BASE_URL", "")).rstrip("/")
        self.token = str(token if token is not None else env.get("AE_API_TOKEN", ""))
        missing = [n for n, value in (("AE_BASE_URL", self.base_url), ("AE_API_TOKEN", self.token)) if not value]
        if missing:
            lines = "\n".join(f"  - {n}：{REQUIRED_ENV.get(n, '对接凭据')}" for n in missing)
            raise ConfigError(f"缺少必需环境变量，入站客户端启动中止（不静默降级）：\n{lines}")
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.request_count = 0  # 实际发出的 GET 次数，供重试策略断言
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout, transport=transport)

    def _backoff(self, attempt: int) -> None:
        # 递增退避；测试注入 MockTransport 时保持极短，不影响业务可观测性
        time.sleep(min(0.01 * (attempt + 1), 0.05))

    def fetch(self, period: str, dept_code: str) -> dict:
        """GET 一批平台数据；返回 {ok, rows, code, status}，永不抛栈。"""
        params = {"period": str(period), "dept_code": str(dept_code)}
        headers = {"Authorization": f"Bearer {self.token}"}
        outcome: dict[str, Any] = {"ok": False, "rows": [], "code": "CONN_FAILED", "status": None}

        for attempt in range(self.max_retries + 1):
            self.request_count += 1
            try:
                response = self._client.get(_REPORT_PATH, params=params, headers=headers)
            except httpx.TimeoutException:
                outcome = {"ok": False, "rows": [], "code": "TIMEOUT", "status": None}
                if attempt < self.max_retries:
                    self._backoff(attempt)
                    continue
                break
            except Exception:
                # 连接失败 / DNS / 协议错误等网络级问题（含 httpx.TransportError）统一 CONN_FAILED
                outcome = {"ok": False, "rows": [], "code": "CONN_FAILED", "status": None}
                if attempt < self.max_retries:
                    self._backoff(attempt)
                    continue
                break

            status = response.status_code
            if 400 <= status < 500:
                # 4xx 业务拒绝：不重试
                outcome = {"ok": False, "rows": [], "code": "HTTP_4XX", "status": status}
                break
            if status >= 500:
                outcome = {"ok": False, "rows": [], "code": "HTTP_5XX", "status": status}
                if attempt < self.max_retries:
                    self._backoff(attempt)
                    continue
                break

            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError):
                outcome = {"ok": False, "rows": [], "code": "NOT_JSON", "status": status}
                break
            schema = _validate_payload(payload)
            if not schema["ok"]:
                outcome = {"ok": False, "rows": [], "code": "SCHEMA_INVALID", "status": status}
                log_event("WARN", code="SCHEMA_INVALID", message=schema["error"],
                          batch_ref=f"{dept_code}-{period}")
                break
            outcome = {
                "ok": True,
                "rows": _attach_meta(schema["rows"], str(period), str(dept_code)),
                "code": None,
                "status": status,
            }
            break

        if not outcome["ok"]:
            log_event("WARN", code=outcome["code"] or "CONN_FAILED",
                      message=f"上报平台读取失败 period={period} dept_code={dept_code}",
                      batch_ref=f"{dept_code}-{period}")
        return outcome

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


def fallback_to_files(client: AEPlatformClient, frames: list[Any], period: str,
                      dept_code: str) -> dict:
    """平台不可达时回落文件来源；成功时合并两来源并按 (dept,period,incident_id) 去重。

    回落时 note 与 summary.note 均标注「本批仅含文件来源」，避免用户误以为拿到全量。
    """
    attempt = client.fetch(period, dept_code)
    platform_rows = attempt["rows"] if attempt["ok"] else []

    file_rows: list[dict] = []
    for frame in frames or []:
        f = _frame_view(frame)
        if f["dept_code"] != str(dept_code) or f["period"] != str(period):
            continue
        for row in f["rows"]:
            record = dict(row)
            record.setdefault("_source", "file")
            file_rows.append(record)

    combined, duplicate_count = _dedup(platform_rows + file_rows, str(period), str(dept_code))
    if attempt["ok"]:
        source = "mixed" if file_rows else "platform"
        note = ""
    else:
        source = "files"
        note = "本批仅含文件来源"

    source_mix: dict[str, int] = {}
    for row in combined:
        key = str(row.get("_source", "file"))
        source_mix[key] = source_mix.get(key, 0) + 1

    summary = {
        "source_mix": source_mix,
        "note": note,
        "platform_ok": attempt["ok"],
        "platform_code": attempt["code"] if not attempt["ok"] else None,
        "platform_rows": len(platform_rows),
        "file_rows": len(file_rows),
        "valid_rows": len(combined),
        "duplicate_count": duplicate_count,
        "period": period,
        "dept_code": dept_code,
    }
    if note:
        log_event("WARN", code=attempt["code"] or "CONN_FAILED", message=note,
                  batch_ref=f"{dept_code}-{period}")

    return {
        "ok": bool(combined),
        "rows": combined,
        "source": source,
        "note": note,
        "summary": summary,
        "code": attempt["code"] if not attempt["ok"] else None,
        "platform_code": attempt["code"] if not attempt["ok"] else None,
        "duplicate_count": duplicate_count,
        "platform_row_count": len(platform_rows),
        "file_row_count": len(file_rows),
    }


__all__ = ["AEPlatformClient", "fallback_to_files", "ERROR_CODES_INGEST"]
