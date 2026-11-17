"""任务 28：台账查询层 / 归档只读 / 保留策略 / 读出层脱敏。"""
from __future__ import annotations

import pytest

from ae_qc.ledger.audit import AuditLog
from ae_qc.ledger.queries import LedgerQueries
from ae_qc.ledger.store import Store


def _seed(tmp_db) -> LedgerQueries:
    q = LedgerQueries(Store(tmp_db))
    q.save_batch("D01-2026-01", dept_code="D01", period="2026-01", source="file",
                 rule_set_version="v2026-01", pass_rate=0.8, valid_rows=5)
    q.save_result("D01-2026-01", {"rule_id": "AE-SEV-LEVEL", "column": "severity_level",
                                  "status": "executed", "success": False,
                                  "unexpected_count": 2, "total_count": 2,
                                  "truncated_count": 0, "rule_set_version": "v2026-01",
                                  "basis": "已核原文"})
    q.save_result("D01-2026-01", {"rule_id": "AE-BROKEN", "column": "x",
                                  "status": "skipped", "success": False,
                                  "unexpected_count": 0, "skip_reason": "TypeError"})
    for no in (2, 5):
        q.save_evidence("D01-2026-01", "AE-SEV-LEVEL",
                        {"row_key": f"ROW{no}", "row_no": no, "dept_code": "D01",
                         "value": "Z", "expected": "A-I", "harm_desc": "自由文本",
                         "patient_name": "不应出现在读出层"})
    q.save_draft("D01-2026-01", {"row_key": "ROW2", "state": "attributed_pending",
                                 "source": "model", "draft_text": "草稿"})
    return q


def test_three_query_results_match_written_data(tmp_db):
    q = _seed(tmp_db)
    batches = q.list_batches()
    assert len(batches) == 1 and batches[0]["batch_ref"] == "D01-2026-01"
    assert batches[0]["pass_rate"] == 0.8
    rows = q.unexpected_rows("D01-2026-01", rule_id="AE-SEV-LEVEL")
    assert len(rows) == 2 and {r["row_no"] for r in rows} == {2, 5}
    assert q.pending_count() == 1 and q.pending_attributions()[0]["row_key"] == "ROW2"
    stats = q.batch_stats("D01-2026-01")
    assert stats["skipped_rules"] == 1 and stats["executed_rules"] == 1
    assert stats["unexpected_rows"] == 2 and stats["pass_rate"] == 0.0


def test_read_layer_strips_patient_identifiable_fields(tmp_db):
    q = _seed(tmp_db)
    rows = q.unexpected_rows("D01-2026-01")
    assert all("patient_name" not in r for r in rows)
    assert all("harm_desc" not in r for r in rows)


def test_archive_is_read_only(tmp_db):
    q = _seed(tmp_db)
    q.archive("D01-2026-01")
    assert q.is_archived("D01-2026-01") is True
    with pytest.raises(ValueError) as exc:
        q.save_result("D01-2026-01", {"rule_id": "R", "column": "x"})
    assert "只读" in str(exc.value)
    assert q.list_batches()[0]["state"] == "archived"


def test_purge_keeps_audit_and_summary(tmp_db):
    q = _seed(tmp_db)
    AuditLog(q.store).write("batch_run", "D01-2026-01", rule_set_version="v2026-01")
    result = q.purge_details("2999-01-01")
    assert result["deleted_details"] == 2
    assert result["audit_events_kept"] >= 1
    assert q.unexpected_rows("D01-2026-01") == []
    assert len(AuditLog(q.store).export("D01-2026-01")) >= 1
    assert q.batch_stats("D01-2026-01")["unexpected_count"] == 2  # 汇总长期保留


def test_filter_by_dept_and_row_no(tmp_db):
    q = _seed(tmp_db)
    assert len(q.unexpected_rows(dept_code="D01", row_no=2)) == 1
    assert q.unexpected_rows(dept_code="D99") == []
