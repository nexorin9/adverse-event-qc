"""任务 16：全量越界明细落盘与回查（截断只影响展示，不影响可查）。

对齐源机制 unexpected_count 是全量计数：界面 / 整改清单显示的「共 N 条」必须
等于全量明细条数。落盘按批次/规则组织，支持按批次/规则/科室/行号回查；
不落姓名 / 住院号 / 诊断明细等患者可识别字段，保留原值与归一后值两份。
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from ae_qc.engine import executor as ex
from ae_qc.engine.detail_store import DetailStore
from ae_qc.engine.row_locator import locate
from ae_qc.models import RuleEntry, RuleSet

BATCH = "MIX-2026-01"


def _ruleset() -> RuleSet:
    return RuleSet(version="t2", entries=[
        RuleEntry(id="SET-SEV", kind="set_membership", column="severity_level",
                  value_set=["A", "B"], basis="测试口径", params={}),
        RuleEntry(id="NN-ID", kind="not_null", column="incident_id",
                  basis="测试口径", params={}),
    ])


def _rows() -> list[dict]:
    rows = []
    for i in range(1, 13):
        dept = "D01" if i <= 6 else "D02"
        r = dict(incident_id=f"EX-{i:04d}", dept_code=dept, severity_level="A",
                 patient_name=f"张三{i}", inpatient_no=f"INP{i}", diagnosis_detail="内部明细",
                 _row_no=i, _row_key=f"k{i}", _dept_code=dept, _period="2026-01",
                 _batch_ref=BATCH, _file_name="example.xlsx", _sheet_name="明细")
        if i in (2, 5, 9):
            r["severity_level"] = "Z"   # SET-SEV 越界：3 行（D01:2 / D02:1）
        if i in (3, 7):
            r["incident_id"] = ""       # NN-ID 越界：2 行（D01:1 / D02:1）
        rows.append(r)
    return rows


def _build(tmp_path: Path):
    rows = _rows()
    plan = ex.build_plan(_ruleset(), ["severity_level", "incident_id"])
    # display_limit=2 制造展示截断，核查全量不受影响
    results = ex.execute_plan(plan, rows, {"limit": 2})["results"]
    store = DetailStore(root=tmp_path / "details")
    grouped: dict[str, list[dict]] = defaultdict(list)
    for res in results:
        grouped[res["rule_id"]].extend(locate(res, rows))
    for rid, evs in grouped.items():
        store.write(BATCH, rid, evs)
    return rows, results, store, tmp_path / "details"


def test_full_count_equals_sum_of_unexpected(tmp_path):
    _rows, results, store, _root = _build(tmp_path)
    total = sum(r["unexpected_count"] for r in results)
    assert total == 5                       # SET-SEV 3 + NN-ID 2
    assert store.count(BATCH) == total       # 全量条数 == unexpected_count 总和
    assert store.count(BATCH, "SET-SEV") == 3
    assert store.count(BATCH, "NN-ID") == 2


def test_query_by_rule_dept_and_row_no(tmp_path):
    _rows, _results, store, _root = _build(tmp_path)
    assert len(store.query(batch_ref=BATCH, rule_id="SET-SEV")) == 3
    assert len(store.query(batch_ref=BATCH, rule_id="NN-ID")) == 2
    assert len(store.query(batch_ref=BATCH, dept_code="D01")) == 3   # SET(2,5) + NN(3)
    assert len(store.query(batch_ref=BATCH, dept_code="D02")) == 2   # SET(9) + NN(7)
    assert len(store.query(batch_ref=BATCH, rule_id="SET-SEV", dept_code="D01")) == 2
    assert len(store.query(batch_ref=BATCH, row_no=2)) == 1
    assert len(store.query(batch_ref=BATCH, row_no=3)) == 1
    assert len(store.query(batch_ref=BATCH, row_no=1)) == 0


def test_truncation_does_not_affect_full_count(tmp_path):
    _rows, results, store, _root = _build(tmp_path)
    # 至少一条结果的展示样本被截断（少于全量计数）
    assert any(len(r["unexpected_samples"]) < r["unexpected_count"] for r in results)
    # 但落盘全量条数仍等于 unexpected_count 总和
    assert store.count(BATCH) == sum(r["unexpected_count"] for r in results)


def test_no_phi_or_forbidden_fields_on_disk(tmp_path):
    _rows, _results, store, root = _build(tmp_path)
    text = "".join(p.read_text(encoding="utf-8") for p in root.rglob("*.json"))
    for token in ("张三", "patient_name", "inpatient_no", "INP7",
                  "diagnosis_detail", "内部明细", "姓名", "住院号", "诊断明细",
                  "score", "rank", "penalty"):
        assert token not in text, f"落盘出现禁止字段/值：{token}"
    # 回查结果不含患者可识别明细，但仍保留原值与归一后值两份（证据可追溯）
    records = store.query(batch_ref=BATCH)
    assert records
    for rec in records:
        raw = rec.get("raw_values", {})
        assert "patient_name" not in raw
        assert "inpatient_no" not in raw
        assert "diagnosis_detail" not in raw
        assert "present_value" in rec and "normalized_value" in rec
