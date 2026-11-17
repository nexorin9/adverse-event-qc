"""审计证据包：回答「这批数是用哪套口径查的、谁确认的、模型调了哪些次、写回回执是什么」。

内容来自任务 27 的审计流水（AuditLog.export），不另造一套：规则套件版本与
条目 basis、期望结果摘要、行级证据计数、模型调用元数据、确认人与时间、
写回回执，外加生成时间／生成人／台账版本。支持按 batch_ref 单批与按 period
批量导出。产出为结构化 JSON（便于飞检／内审核对），生成前经统一脱敏检查器，
不落患者可识别明细，也不含考核类字段（272 号文）。

人工确认是唯一放行口（见 writeback.guard）；年度底稿不自动外发
（见 reporting.annual_draft）。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..ledger.audit import MODEL_META_FIELDS, scrub
from ..ledger.store import SCHEMA_VERSION
from .headers import assert_no_forbidden, sanitize


def _select(events: list[dict], batch_ref: Any, period: Any) -> list[dict]:
    """按 batch_ref 单批 / 按 period 批量过滤；两者都给时取交集。"""
    out: list[dict] = []
    ref_filter = str(batch_ref or "")
    period_filter = str(period or "")
    for event in events:
        ref = str(event.get("batch_ref", "") or "")
        if ref_filter and ref != ref_filter:
            continue
        if period_filter:
            payload = event.get("payload") or {}
            hint = str(payload.get("period", "") or "") if isinstance(payload, dict) else ""
            if period_filter not in ref and hint != period_filter:
                continue
        out.append(event)
    return out


def export_evidence_pack(batch_ref: Any, audit_events: list[dict], path, *,
                         period: str | None = None, generator: str = "质控科") -> dict:
    """导出审计证据包；返回含 path / pack / 计数与元信息的 dict。

    注入禁用字段／患者可识别字段到审计流水时抛 ValueError（272 号文不变量）。
    """
    events = [dict(e) for e in (audit_events or [])]
    assert_no_forbidden(events)
    selected = _select(events, batch_ref, period)
    # 双重脱敏：audit.scrub（裁患者文本与长原文）+ headers.sanitize（裁禁用字段）
    clean_events = [sanitize(scrub(e)) for e in selected]

    rule_versions: list[str] = []
    basis_by_rule: dict[str, list[str]] = {}
    result_summaries: list[dict] = []
    row_evidence_count = 0
    model_meta: list[dict] = []
    confirmations: list[dict] = []
    receipts: list[dict] = []

    for event in clean_events:
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        etype = str(event.get("event_type", ""))
        ver = payload.get("rule_set_version") or payload.get("ruleset_version")
        if ver and str(ver) not in rule_versions:
            rule_versions.append(str(ver))

        # 规则版本与每条 basis（按 rule_id / 事件类型归集，不造新口径）
        basis = payload.get("basis")
        if basis:
            rid = str(payload.get("rule_id") or etype)
            bucket = basis_by_rule.setdefault(rid, [])
            if str(basis) not in bucket:
                bucket.append(str(basis))

        # 期望结果摘要
        if "unexpected_count" in payload or "expected_result_summary" in payload:
            summary = payload.get("expected_result_summary") or {}
            result_summaries.append({
                "event_id": event.get("id"), "ts": event.get("ts"),
                "rule_id": payload.get("rule_id", ""),
                "success": summary.get("success", payload.get("success")),
                "unexpected_count": summary.get("unexpected_count",
                                                payload.get("unexpected_count", 0)),
                "rule_set_version": str(ver or ""),
            })

        # 行级证据计数
        for key in ("row_evidence_count", "unexpected_rows"):
            value = payload.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                row_evidence_count += int(value)

        # 模型调用元数据（只取白名单字段，绝不落完整患者文本）
        if etype == "model_call" or any(k in payload for k in MODEL_META_FIELDS):
            meta = {k: payload[k] for k in MODEL_META_FIELDS if k in payload}
            if meta:
                model_meta.append({"event_id": event.get("id"),
                                   "ts": event.get("ts"), **meta})

        # 确认人与时间
        if etype == "confirmation" or "confirmed_at" in payload:
            confirmations.append({
                "event_id": event.get("id"),
                "finding_id": payload.get("finding_id", ""),
                "actor": event.get("actor") or payload.get("actor")
                         or payload.get("confirmed_by", ""),
                "confirmed_at": payload.get("confirmed_at") or event.get("ts", ""),
            })

        # 写回回执
        if etype == "writeback_receipt" or "receipt_id" in payload:
            receipts.append({
                "event_id": event.get("id"),
                "receipt_id": payload.get("receipt_id", ""),
                "status_code": payload.get("status_code") or payload.get("status"),
                "idempotency_key": payload.get("idempotency_key", ""),
                "ts": event.get("ts", ""),
            })

    pack: dict[str, Any] = {
        "index": ["生成信息", "规则套件版本", "条目 basis", "期望结果摘要",
                  "行级证据计数", "模型调用元数据", "确认人与时间", "写回回执", "审计流水"],
        "format": "ae_qc.evidence_pack/v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "generator": generator,
        "ledger_schema_version": SCHEMA_VERSION,
        "batch_ref": str(batch_ref or ""),
        "period": str(period or ""),
        "rule_set_versions": rule_versions,
        "basis_by_rule": basis_by_rule,
        "expected_result_summaries": result_summaries,
        "row_evidence_count": row_evidence_count,
        "model_call_metadata": model_meta,
        "confirmations": confirmations,
        "writeback_receipts": receipts,
        "audit_event_count": len(clean_events),
        "audit_events": clean_events,
    }
    assert_no_forbidden(pack)

    out = Path(path)
    if not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pack, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    return {"path": str(out), "pack": pack,
            "batch_ref": str(batch_ref or ""), "period": str(period or ""),
            "audit_event_count": len(clean_events),
            "rule_set_versions": rule_versions,
            "generated_at": pack["generated_at"], "generator": generator,
            "ledger_schema_version": SCHEMA_VERSION}


__all__ = ["export_evidence_pack"]
