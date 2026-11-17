"""任务 27：审计流水含规则版本/basis/模型元数据/确认人时间/写回回执，且不落患者文本。"""
from __future__ import annotations

from ae_qc.ledger.audit import MODEL_META_FIELDS, AuditLog, scrub
from ae_qc.ledger.idempotency import idempotency_key
from ae_qc.ledger.store import Store


def test_confirmation_event_has_actor_and_time(tmp_db):
    log = AuditLog(Store(tmp_db))
    event = log.confirmation("D01-2026-01", "EX-F-1", "示例质控员")
    assert event["payload"]["actor"] == "示例质控员"
    assert event["payload"]["confirmed_at"]
    exported = log.export("D01-2026-01")
    assert len(exported) == 1 and exported[0]["event_type"] == "confirmation"


def test_audit_contains_basis_and_ruleset_version(tmp_db):
    log = AuditLog(Store(tmp_db))
    log.write("batch_run", "D01-2026-01",
              rule_set_version="v2026-01",
              basis="《医疗质量安全不良事件分级分类标准》附件2（已核原文）",
              expected_result_summary={"success": False, "unexpected_count": 3},
              row_evidence_count=3)
    events = log.export("D01-2026-01")
    payload = events[-1]["payload"]
    assert payload["rule_set_version"] == "v2026-01"
    assert "附件2" in payload["basis"]
    assert payload["row_evidence_count"] == 3


def test_model_metadata_fields_complete_and_no_patient_text(tmp_db):
    log = AuditLog(Store(tmp_db))
    long_text = "患者" * 300
    meta = {"request_id": "req-1", "model": "院内模型", "elapsed_ms": 123.4,
            "tokens": 512, "input_count": 20, "retries": 1, "code": "",
            "input_texts": long_text, "patient_name": "不应落库"}
    event = log.model_call("D01-2026-01", meta)
    payload = event["payload"]
    for field in ("request_id", "model", "elapsed_ms", "tokens", "input_count", "retries"):
        assert field in payload
    assert "input_texts" not in payload or payload.get("input_texts") is None
    assert "patient_name" not in payload
    assert log.assert_no_phi("D01-2026-01")
    assert set(MODEL_META_FIELDS) >= {"request_id", "model", "tokens", "retries"}


def test_receipt_and_scrub(tmp_db):
    log = AuditLog(Store(tmp_db))
    key = idempotency_key("D01-2026-01", "AE-SEV-LEVEL", "ROW1")
    log.receipt("D01-2026-01", "EX-RECEIPT-1", 201, key)
    events = log.export("D01-2026-01")
    assert events[-1]["payload"]["receipt_id"] == "EX-RECEIPT-1"
    assert events[-1]["payload"]["status_code"] == 201
    cleaned = scrub({"harm_desc": "很长" * 300, "root_cause_text": "原文",
                     "diagnosis_detail": "不应出现", "ok": 1})
    assert "harm_desc" not in cleaned and "diagnosis_detail" not in cleaned
    assert cleaned["ok"] == 1
