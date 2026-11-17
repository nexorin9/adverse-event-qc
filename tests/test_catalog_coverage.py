"""任务 12：反向规则族 catalog_coverage。

对齐源机制「反向覆盖断言」（期望集合中某些成员必须出现）：本期间按目录应出现
的类目缺失时产生一条 info/提示级发现；提示不判定违规，不混入 unexpected_count
的错误口径（success 仍为 True，unexpected_samples 为空）。
"""
from __future__ import annotations

from ae_qc.engine.rule_kinds import evaluate
from ae_qc.models import RuleEntry


def _entry(expected, **params) -> RuleEntry:
    p = {"expected_categories": list(expected)}
    p.update(params)
    return RuleEntry(id="CC", kind="catalog_coverage", column="event_type_code",
                     basis="测试口径·目录覆盖（提示级）", severity="info", params=p)


def _run(entry: RuleEntry, rows: list[dict]):
    return evaluate(entry, [dict(r) for r in rows], {})


def test_missing_categories_produce_one_info_finding():
    e = _entry(["AE-CAT-01", "AE-CAT-02", "AE-CAT-03"])
    rows = [{"_row_no": 1, "event_type_code": "AE-CAT-01", "_dept_code": "D01"}]
    r = _run(e, rows)
    assert r.unexpected_count == 0
    assert r.success is True
    assert len(r.info_findings) == 1
    f = r.info_findings[0]
    assert f["severity"] == "info"
    assert f["kind"] == "catalog_coverage"
    assert f["rule_id"] == "CC"
    assert set(f["missing_categories"]) == {"AE-CAT-02", "AE-CAT-03"}
    assert f["observed_categories"] == ["AE-CAT-01"]


def test_full_coverage_produces_no_finding(clean_rows):
    """用公共 clean_rows：把其出现的类目作为期望集合，全覆盖不产生提示。"""
    codes = sorted({str(row["event_type_code"]).strip() for row in clean_rows
                    if str(row["event_type_code"]).strip()})
    e = _entry(codes)
    r = _run(e, clean_rows)
    assert r.info_findings == []
    assert r.unexpected_count == 0
    assert r.success is True


def test_out_of_scope_dept_produces_no_finding():
    """applicable_depts 限定适用科室：不在范围内的科室不产生提示。"""
    e = _entry(["AE-CAT-01"], applicable_depts=["D05", "D10"])
    rows = [{"_row_no": 1, "event_type_code": "AE-CAT-99", "_dept_code": "D01"}]
    r = _run(e, rows)
    assert r.info_findings == []
    assert r.unexpected_count == 0
    assert r.observed_value.get("out_of_scope") is True


def test_info_level_not_counted_as_unexpected():
    """提示级发现不进 unexpected_count，也不进 unexpected_samples 的错误口径。"""
    e = _entry(["AE-CAT-01", "AE-CAT-02"])
    rows = [{"_row_no": 1, "event_type_code": "AE-CAT-01", "_dept_code": "D05"}]
    r = _run(e, rows)
    assert len(r.info_findings) == 1
    assert r.info_findings[0]["severity"] == "info"
    assert r.info_findings[0]["severity"] != "error"
    assert r.unexpected_count == 0
    assert r.total_count == 0
    assert r.unexpected_samples == []
    assert r.success is True   # 提示不判定违规
