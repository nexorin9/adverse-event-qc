"""任务 43：审计证据包（规则版本/basis/结果/模型元数据/确认人/回执）。"""
from __future__ import annotations

import json

import pytest

from ae_qc.ledger.audit import AuditLog
from ae_qc.ledger.store import SCHEMA_VERSION, Store
from ae_qc.reporting.evidence_pack import export_evidence_pack

BASIS = "《医疗质量安全不良事件分级分类标准》附件2（已核原文）"


def _audit_events(tmp_db):
    store = Store(tmp_db)
    log = AuditLog(store)
    log.write("batch_run", "D01-2026-01", rule_set_version="v2026-01",
              rule_id="AE-SEV-LEVEL", basis=BASIS,
              unexpected_count=3, row_evidence_count=3,
              expected_result_summary={"success": False, "unexpected_count": 3})
    log.model_call("D01-2026-01", {"request_id": "req-1", "model": "院内模型",
                                   "elapsed_ms": 12.3, "tokens": 512,
                                   "input_count": 20, "retries": 0, "code": ""})
    log.confirmation("D01-2026-01", "EX-F-1", "示例质控员")
    log.receipt("D01-2026-01", "EX-R-1", 201, "k" * 64)
    return store, log.export("D01-2026-01")


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_contains_rule_version_and_each_basis(tmp_db, tmp_path):
    """含规则套件版本与每条 basis。"""
    _, events = _audit_events(tmp_db)
    out = export_evidence_pack("D01-2026-01", events, tmp_path / "pack.json")
    pack = _read(tmp_path / "pack.json")
    assert "v2026-01" in pack["rule_set_versions"]
    assert "AE-SEV-LEVEL" in pack["basis_by_rule"]
    assert BASIS in pack["basis_by_rule"]["AE-SEV-LEVEL"]
    assert out["rule_set_versions"] == ["v2026-01"]


def test_contains_confirmation_actor_and_time(tmp_db, tmp_path):
    """含确认人与时间。"""
    _, events = _audit_events(tmp_db)
    export_evidence_pack("D01-2026-01", events, tmp_path / "pack.json")
    pack = _read(tmp_path / "pack.json")
    assert len(pack["confirmations"]) == 1
    c = pack["confirmations"][0]
    assert c["actor"] == "示例质控员"
    assert c["confirmed_at"]
    assert c["finding_id"] == "EX-F-1"


def test_contains_model_metadata_fields(tmp_db, tmp_path):
    """含模型调用元数据字段（request_id/model/耗时/tokens/输入条数/重试次数）。"""
    _, events = _audit_events(tmp_db)
    export_evidence_pack("D01-2026-01", events, tmp_path / "pack.json")
    pack = _read(tmp_path / "pack.json")
    assert len(pack["model_call_metadata"]) == 1
    meta = pack["model_call_metadata"][0]
    for field in ("request_id", "model", "elapsed_ms", "tokens", "input_count", "retries"):
        assert field in meta
    assert meta["request_id"] == "req-1"


def test_contains_receipts_and_generation_metadata(tmp_db, tmp_path):
    """含写回回执、期望结果摘要、行级证据计数、生成时间/生成人/台账版本。"""
    _, events = _audit_events(tmp_db)
    out = export_evidence_pack("D01-2026-01", events, tmp_path / "pack.json",
                               generator="质控科")
    pack = _read(tmp_path / "pack.json")
    assert pack["writeback_receipts"][0]["receipt_id"] == "EX-R-1"
    assert pack["writeback_receipts"][0]["status_code"] == 201
    assert pack["expected_result_summaries"][0]["unexpected_count"] == 3
    assert pack["row_evidence_count"] == 3
    assert pack["generated_at"]
    assert pack["generator"] == "质控科"
    assert pack["ledger_schema_version"] == SCHEMA_VERSION
    assert out["ledger_schema_version"] == SCHEMA_VERSION
    assert "生成信息" in pack["index"] and "条目 basis" in pack["index"]


def test_batch_ref_export_matches_audit_stream(tmp_db, tmp_path):
    """按 batch_ref 单批导出，内容与审计流水一致，其它批被排除。"""
    store, _ = _audit_events(tmp_db)
    AuditLog(store).write("batch_run", "D02-2026-02", rule_set_version="v2026-02",
                          basis="另一版口径")
    all_events = AuditLog(store).export()
    out = export_evidence_pack("D01-2026-01", all_events, tmp_path / "pack.json")
    pack = _read(tmp_path / "pack.json")
    expected_ids = [e["id"] for e in all_events if e["batch_ref"] == "D01-2026-01"]
    assert sorted(e["id"] for e in pack["audit_events"]) == sorted(expected_ids)
    assert pack["audit_event_count"] == len(expected_ids) == 4
    assert "v2026-02" not in pack["rule_set_versions"]
    assert out["audit_event_count"] == len(expected_ids)


def test_period_export_supported(tmp_db, tmp_path):
    """按 period 批量导出可用。"""
    store, _ = _audit_events(tmp_db)
    AuditLog(store).write("batch_run", "D02-2026-02", rule_set_version="v2026-02",
                          basis="另一版口径")
    all_events = AuditLog(store).export()
    out = export_evidence_pack(None, all_events, tmp_path / "pack_p.json", period="2026-01")
    pack = _read(tmp_path / "pack_p.json")
    assert pack["period"] == "2026-01"
    assert {e["batch_ref"] for e in pack["audit_events"]} == {"D01-2026-01"}
    assert pack["audit_event_count"] == 4
    assert out["period"] == "2026-01"


@pytest.mark.parametrize("bad_field", ["score", "患者姓名", "住院号"])
def test_injected_forbidden_field_rejected(tmp_db, tmp_path, bad_field):
    """注入禁用字段／患者可识别字段即被拒。"""
    _, events = _audit_events(tmp_db)
    events[0][bad_field] = "x"
    with pytest.raises(ValueError):
        export_evidence_pack("D01-2026-01", events, tmp_path / "bad.json")
