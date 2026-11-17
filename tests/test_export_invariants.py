"""任务 44：导出脱敏 + 「不产出考核字段」不变量（272 号文），跨 4 个产物参数化。

覆盖 整改清单 / 年度底稿 / 证据包 / 写回 body；注入一个禁用字段或患者可识别
字段即被拒。人工确认是唯一放行口；年度底稿不自动外发。
"""
from __future__ import annotations

import json

import pytest
from openpyxl import load_workbook

from ae_qc.ledger.audit import AuditLog
from ae_qc.ledger.store import Store
from ae_qc.models import FORBIDDEN_TOKENS
from ae_qc.reporting.annual_draft import MANUAL_NOTICE, export_annual_draft
from ae_qc.reporting.evidence_pack import export_evidence_pack
from ae_qc.reporting.headers import (
    ANNUAL_HEADERS, EXPORT_HEADERS, FINDING_BODY_FIELDS,
    FORBIDDEN_FIELD_TOKENS, assert_no_forbidden, sanitize,
)
from ae_qc.reporting.rectify_list import export_rectify_list
from ae_qc.writeback.guard import build_body

BASIS = "《医疗质量安全不良事件分级分类标准》附件2（已核原文）"


def _finding():
    return {
        "finding_id": "EX-F-1", "batch_ref": "D01-2026-01", "dept_code": "D01",
        "period": "2026-01", "rule_id": "AE-SEV-LEVEL",
        "rule_set_version": "v2026-01", "observed_value": "Z",
        "unexpected_count": 1, "unexpected_samples": [{"row_no": 2, "value": "Z"}],
        "attribution_draft": "拼写漂移", "attribution_confidence": 0.9,
        "status": "confirmed", "confirmed_by": "示例质控员",
        "confirmed_at": "2026-01-10T10:00:00", "row_key": "ROW-1",
    }


def _rows():
    return [dict(dept_code="D01", row_no=1, column="severity_level",
                 present_value="Z", expected_value="A–I", basis=BASIS,
                 rule_id="AE-SEV-LEVEL")]


def _stats():
    return [{"period": "2026-01", "dept_code": "D01", "category": "safety",
             "level": "A", "count": 2, "batch_ref": "D01-2026-01",
             "rule_set_version": "v2026-01"}]


def _audit_events(tmp_path):
    store = Store(str(tmp_path / "inv.sqlite"))
    log = AuditLog(store)
    log.write("batch_run", "D01-2026-01", rule_set_version="v2026-01",
              rule_id="AE-SEV-LEVEL", basis=BASIS, row_evidence_count=2)
    log.model_call("D01-2026-01", {"request_id": "req-1", "model": "院内模型",
                                   "tokens": 10, "retries": 0})
    log.confirmation("D01-2026-01", "EX-F-1", "示例质控员")
    return log.export("D01-2026-01")


def _produce_rectify(tmp_path, inject):
    rows = _rows()
    if inject:
        rows[0][inject[0]] = inject[1]
    out = export_rectify_list(rows, tmp_path / "rectify.xlsx")
    ws = load_workbook(out["path"]).active
    return {"headers": [c.value for c in ws[1]],
            "row_count": out["row_count"],
            "basis_present": bool(ws.cell(row=2, column=6).value)}


def _produce_annual(tmp_path, inject):
    rows = _stats()
    if inject:
        rows[0][inject[0]] = inject[1]
    out = export_annual_draft(rows, tmp_path / "annual.xlsx")
    ws = load_workbook(out["path"])["年度汇总底稿"]
    return {"headers": [c.value for c in ws[1]],
            "total_count": out["total_count"],
            "notice_present": MANUAL_NOTICE in out["notice"]}


def _produce_evidence(tmp_path, inject):
    events = _audit_events(tmp_path)
    if inject:
        events[0][inject[0]] = inject[1]
    export_evidence_pack("D01-2026-01", events, tmp_path / "pack.json")
    return json.loads((tmp_path / "pack.json").read_text(encoding="utf-8"))


def _produce_body(tmp_path, inject):
    finding = _finding()
    if inject:
        finding[inject[0]] = inject[1]
    return build_body(finding)


PRODUCERS = [
    ("整改清单", _produce_rectify, ("headers", "row_count", "basis_present")),
    ("年度底稿", _produce_annual, ("headers", "total_count", "notice_present")),
    ("证据包", _produce_evidence, ("audit_events", "rule_set_versions", "generated_at")),
    ("写回body", _produce_body, tuple(FINDING_BODY_FIELDS)),
]
INJECT_PRODUCERS = [(name, producer) for name, producer, _ in PRODUCERS]


@pytest.mark.parametrize("name,producer,expected_keys", PRODUCERS)
def test_legal_product_has_no_forbidden_fields(tmp_path, name, producer, expected_keys):
    """四个产物各一断言：合法产物字段齐备且不含任何禁用/PII 字段。"""
    product = producer(tmp_path, None)
    for key in expected_keys:
        assert key in product
    assert_no_forbidden(product)


@pytest.mark.parametrize("name,producer", INJECT_PRODUCERS)
@pytest.mark.parametrize("bad_field", [
    "score", "rank", "penalty", "绩效分", "排名", "扣分", "罚分", "performance",
])
def test_forbidden_assessment_field_rejected(tmp_path, name, producer, bad_field):
    """注入一个考核类禁用字段即被拒（跨 4 个产物）。"""
    with pytest.raises(ValueError):
        producer(tmp_path, (bad_field, 99))


@pytest.mark.parametrize("name,producer", INJECT_PRODUCERS)
@pytest.mark.parametrize("phi_field", [
    "patient_name", "姓名", "住院号", "inpatient_no", "诊断明细", "患者姓名",
])
def test_patient_identifiable_field_rejected(tmp_path, name, producer, phi_field):
    """注入一个患者可识别字段即被拒（跨 4 个产物）。"""
    with pytest.raises(ValueError):
        producer(tmp_path, (phi_field, "张三"))


def test_export_headers_and_body_whitelist_clean():
    """导出表头与出站 body 白名单一律不含禁用 token。"""
    assert_no_forbidden(EXPORT_HEADERS)
    assert_no_forbidden(ANNUAL_HEADERS)
    assert_no_forbidden({f: None for f in FINDING_BODY_FIELDS})
    for token in FORBIDDEN_FIELD_TOKENS:
        for header in EXPORT_HEADERS:
            assert token.lower() not in header.lower()
        for header in ANNUAL_HEADERS:
            assert token.lower() not in header.lower()
        for field in FINDING_BODY_FIELDS:
            assert token.lower() not in field.lower()


def test_forbidden_tokens_match_base_model():
    """统一检查器与底座 models.FORBIDDEN_TOKENS 同源（底座是其子集）。"""
    assert set(FORBIDDEN_TOKENS) <= set(FORBIDDEN_FIELD_TOKENS)
    for token in ("score", "rank", "penalty", "绩效", "排名", "扣分",
                  "姓名", "住院号", "诊断明细"):
        assert token in FORBIDDEN_FIELD_TOKENS


def test_sanitize_removes_forbidden_fields():
    cleaned = sanitize({"score": 1, "ok": 2, "nested": {"rank": 3, "keep": 4}})
    assert cleaned == {"ok": 2, "nested": {"keep": 4}}


@pytest.mark.parametrize("field", ["score", "患者姓名", "住院号", "诊断明细"])
def test_assert_no_forbidden_raises_on_injected_field(field):
    """assert_no_forbidden 命中禁用字段抛 ValueError 并点名。"""
    with pytest.raises(ValueError) as exc:
        assert_no_forbidden({"finding_id": "F1", field: "x"})
    assert "272" in str(exc.value)
    assert field in str(exc.value)
