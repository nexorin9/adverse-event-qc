"""审计流水：规则版本 + 条目 basis + 模型元数据 + 确认人时间 + 写回回执。

模型调用元数据只落 request_id / 模型名 / 耗时 / token 数 / 输入条数 / 重试次数 /
错误码，不落完整患者相关文本（读出即裁剪长文本与敏感键）。证据包直接复用本流水。
"""
from __future__ import annotations

from typing import Any

from ..models import FORBIDDEN_TOKENS
from .store import Store, dumps, loads, now_iso

MODEL_META_FIELDS = (
    "request_id", "model", "model_name", "elapsed_ms", "tokens", "token_count",
    "input_count", "retries", "code",
)
NO_PHI_KEYS = (
    "patient_name", "姓名", "住院号", "inpatient_no", "diagnosis_detail", "诊断明细",
    "raw_values", "raw", "harm_desc", "root_cause_text", "original_text", "full_text",
)
MAX_TEXT_CHARS = 200


def scrub(payload: dict[str, Any]) -> dict[str, Any]:
    """裁掉患者可识别字段与超长原文，只保留可审计的摘要。"""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if any(token in str(key).lower() for token in ("patient_name", "inpatient_no", "diagnosis_detail")):
            continue
        if key in NO_PHI_KEYS:
            continue
        if isinstance(value, str) and len(value) > MAX_TEXT_CHARS:
            out[key] = value[:MAX_TEXT_CHARS] + f"…（已截断 {len(value) - MAX_TEXT_CHARS} 字符）"
        elif isinstance(value, dict):
            out[key] = scrub(value)
        else:
            out[key] = value
    return out


class AuditLog:
    def __init__(self, store: Store):
        self.store = store
        self.store.initialize()

    def write(self, event_type: str, batch_ref: str | None = None, **payload: Any) -> dict[str, Any]:
        clean = scrub(dict(payload))
        ts = now_iso()
        row_id = self.store.execute(
            "INSERT INTO audit_events(ts,event_type,batch_ref,actor,payload_json) VALUES (?,?,?,?,?)",
            (ts, event_type, batch_ref or clean.get("batch_ref", ""),
             str(clean.get("actor", clean.get("confirmed_by", ""))), dumps(clean)),
        )
        return {"id": row_id, "ts": ts, "event_type": event_type,
                "batch_ref": batch_ref or "", "payload": clean}

    def model_call(self, batch_ref: str, meta: dict[str, Any]) -> dict[str, Any]:
        """只落模型调用元数据白名单字段，绝不落完整请求文本。"""
        clean = {k: meta.get(k) for k in MODEL_META_FIELDS if k in meta}
        clean["input_texts"] = None
        return self.write("model_call", batch_ref, **clean)

    def confirmation(self, batch_ref: str, finding_id: str, actor: str,
                     confirmed_at: str | None = None) -> dict[str, Any]:
        return self.write("confirmation", batch_ref, finding_id=finding_id,
                          actor=actor, confirmed_at=confirmed_at or now_iso())

    def receipt(self, batch_ref: str, receipt_id: str, status: int,
                idempotency_key: str) -> dict[str, Any]:
        return self.write("writeback_receipt", batch_ref, receipt_id=receipt_id,
                          status_code=status, idempotency_key=idempotency_key)

    def export(self, batch_ref: str | None = None, *, period: str | None = None) -> list[dict[str, Any]]:
        """按批次导出可复现历史批次的审计流水（证据包复用）。"""
        if batch_ref:
            rows = self.store.query(
                "SELECT * FROM audit_events WHERE batch_ref=? ORDER BY id", (batch_ref,))
        elif period:
            rows = self.store.query(
                "SELECT a.* FROM audit_events a JOIN batches b ON a.batch_ref=b.batch_ref"
                " WHERE b.period=? ORDER BY a.id", (period,))
        else:
            rows = self.store.query("SELECT * FROM audit_events ORDER BY id")
        return [{"id": r["id"], "ts": r["ts"], "event_type": r["event_type"],
                 "batch_ref": r["batch_ref"], "actor": r["actor"],
                 "payload": loads(r["payload_json"], {})} for r in rows]

    def assert_no_phi(self, batch_ref: str) -> bool:
        """供不变量测试：审计流水中不得出现患者可识别字段或长文本。"""
        blob = dumps(self.export(batch_ref))
        return not any(token in blob for token in ("patient_name", "inpatient_no",
                                                   "diagnosis_detail", "住院号", "诊断明细"))
