"""任务 42：年度汇总表底稿（院内人工上传，本产品不自动外发）。"""
from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

import ae_qc.reporting.annual_draft as annual_module
from ae_qc.ledger.queries import LedgerQueries
from ae_qc.ledger.store import Store
from ae_qc.reporting.annual_draft import MANUAL_NOTICE, export_annual_draft
from ae_qc.reporting.headers import ANNUAL_HEADERS, assert_no_forbidden


def _stats_from_ledger(tmp_db):
    """数字取自真实台账聚合（LedgerQueries.batch_stats），不另造一套统计。"""
    store = Store(tmp_db)
    q = LedgerQueries(store)
    batch_ref = "D01-2026-01"
    q.save_batch(batch_ref, dept_code="D01", period="2026-01", rule_set_version="v2026-01")
    q.save_result(batch_ref, {"rule_id": "AE-SEV-LEVEL", "column": "severity_level",
                              "status": "executed", "success": False,
                              "unexpected_count": 3, "total_count": 3})
    for n in range(1, 4):
        q.save_evidence(batch_ref, "AE-SEV-LEVEL",
                        {"row_key": f"R{n}", "row_no": n, "dept_code": "D01"})
    return q.batch_stats(batch_ref), batch_ref


def _rows(stats, batch_ref):
    return [{"period": "2026-01", "dept_code": "D01", "category": "safety",
             "level": "A", "count": stats["unexpected_rows"],
             "batch_ref": batch_ref, "rule_set_version": "v2026-01"}]


def test_fields_complete(tmp_db, tmp_path):
    """字段齐备：期间/科室/类别/等级/条数。"""
    stats, batch_ref = _stats_from_ledger(tmp_db)
    out = export_annual_draft(_rows(stats, batch_ref), tmp_path / "annual.xlsx")
    assert ANNUAL_HEADERS == ("期间", "科室", "类别", "等级", "条数")
    assert out["headers"] == list(ANNUAL_HEADERS)
    ws = load_workbook(out["path"])["年度汇总底稿"]
    assert [c.value for c in ws[1]] == list(ANNUAL_HEADERS)
    assert_no_forbidden(ANNUAL_HEADERS)


def test_numbers_match_ledger_stats(tmp_db, tmp_path):
    """底稿数字与台账统计一致，且每个数字可回溯到批次与规则版本。"""
    stats, batch_ref = _stats_from_ledger(tmp_db)
    out = export_annual_draft(_rows(stats, batch_ref), tmp_path / "annual.xlsx")
    assert out["total_count"] == stats["unexpected_rows"] == 3
    wb = load_workbook(out["path"])
    ws = wb["年度汇总底稿"]
    count_idx = list(ANNUAL_HEADERS).index("条数") + 1
    assert ws.cell(row=2, column=count_idx).value == stats["unexpected_rows"]
    src = wb["口径来源"]
    assert [c.value for c in src[1]] == ["行号", "批次号", "规则版本", "条数", "聚合来源"]
    assert src.cell(row=2, column=2).value == batch_ref
    assert src.cell(row=2, column=3).value == "v2026-01"
    assert src.cell(row=2, column=4).value == stats["unexpected_rows"]
    assert out["provenance"][0]["batch_ref"] == batch_ref
    assert out["provenance"][0]["rule_set_version"] == "v2026-01"


def test_manual_upload_notice_present(tmp_db, tmp_path):
    """底稿内标注「需院内人工上传国家平台，本产品不自动外发」。"""
    stats, batch_ref = _stats_from_ledger(tmp_db)
    out = export_annual_draft(_rows(stats, batch_ref), tmp_path / "annual.xlsx")
    assert "需院内人工上传国家平台" in out["notice"]
    assert "本产品不自动外发" in out["notice"]
    assert MANUAL_NOTICE == "需院内人工上传国家平台，本产品不自动外发"
    wb = load_workbook(out["path"])
    blob = " ".join(str(c.value) for ws in wb for row in ws.iter_rows()
                    for c in row if c.value is not None)
    assert MANUAL_NOTICE in blob


def test_source_has_no_automatic_outbound_call():
    """grep 断言：代码中不存在任何自动外发/上传调用路径。"""
    source = Path(annual_module.__file__).read_text(encoding="utf-8")
    forbidden = ("import httpx", "import requests", "urllib", "socket",
                 "http.client", ".post(", "upload(", "urlopen", "smtplib",
                 "requests.post", "httpx.post", ".send(")
    hits = [token for token in forbidden if token in source]
    assert hits == []


@pytest.mark.parametrize("bad_field", ["score", "绩效分", "患者姓名", "住院号"])
def test_injected_forbidden_field_rejected(tmp_db, tmp_path, bad_field):
    """注入禁用字段／患者可识别字段即被拒。"""
    stats, batch_ref = _stats_from_ledger(tmp_db)
    rows = _rows(stats, batch_ref)
    rows[0][bad_field] = "x"
    with pytest.raises(ValueError):
        export_annual_draft(rows, tmp_path / "bad.xlsx")
