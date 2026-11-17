"""任务 15：行级证据定位 + 越界样本截断。

对齐源机制「一次期望结果取回具体记录定位到行」：unexpected_count 展开为
RowEvidence 形状的行级证据（row_key / 行号 / 越界值 / 原始行 / 文件名+工作表），
展示样本可配上限，超限标注 truncated_count 与 total_count，但全量明细可查。
"""
from __future__ import annotations

from ae_qc.engine import executor as ex
from ae_qc.engine.rule_kinds import evaluate
from ae_qc.engine.row_locator import locate, truncate_samples
from ae_qc.models import RuleEntry, RuleSet


def _rule() -> RuleEntry:
    return RuleEntry(id="SET", kind="set_membership", column="severity_level",
                     value_set=["A"], basis="测试口径·行级定位", params={})


def _rows(n: int = 25) -> list[dict]:
    return [
        dict(incident_id=f"EX-{i:04d}", severity_level="Z", patient_name=f"张三{i}",
             _row_no=i, _row_key=f"k{i}", _dept_code="D01", _period="2026-01",
             _batch_ref="D01-2026-01", _file_name="example.xlsx", _sheet_name="明细")
        for i in range(1, n + 1)
    ]


def _full_result(rows: list[dict], display_limit: int = 5) -> dict:
    """用执行器产出带全量 unexpected_detail、展示样本被截断的结果。"""
    rs = RuleSet(version="t1", entries=[_rule()])
    plan = ex.build_plan(rs, ["severity_level"])
    return ex.execute_plan(plan, rows, {"limit": display_limit})["results"][0]


def test_locate_count_matches_unexpected_count():
    rows = _rows()
    res = _full_result(rows)
    evs = locate(res, rows)
    assert res["unexpected_count"] == 25
    assert len(evs) == res["unexpected_count"]
    assert len(res["unexpected_samples"]) < res["unexpected_count"]  # 展示被截断
    assert len(evs) != len(res["unexpected_samples"])                 # 展开的是全量而非展示样本


def test_truncate_counts_are_correct():
    rows = _rows()
    res = _full_result(rows)
    t = truncate_samples(res["unexpected_detail"], 5)
    assert t["total_count"] == 25
    assert t["truncated_count"] == 20
    assert len(t["items"]) == 5
    # 结果自身的 total_count / truncated_count 标注一致
    assert res["total_count"] == 25
    assert res["truncated_count"] == 20
    # 上限不小于总量时不截断
    t2 = truncate_samples(res["unexpected_detail"], 50)
    assert t2["total_count"] == 25
    assert t2["truncated_count"] == 0
    assert len(t2["items"]) == 25


def test_evidence_points_back_to_original_row():
    rows = _rows()
    res = _full_result(rows)
    evs = locate(res, rows)
    by_no = {r["_row_no"]: r for r in rows}
    assert {e["row_no"] for e in evs} == set(range(1, 26))
    for e in evs:
        row = by_no[e["row_no"]]
        # 可回指原始行：文件名 + 工作表 + 行号 + 批次 + row_key
        assert e["file_name"] == row["_file_name"] == "example.xlsx"
        assert e["sheet_name"] == row["_sheet_name"] == "明细"
        assert e["row_no"] == row["_row_no"]
        assert e["row_key"] == row["_row_key"]
        assert e["batch_ref"] == row["_batch_ref"]
        assert e["column"] == "severity_level"
        # 保留原值与归一后值两份
        assert e["present_value"] == "Z"
        assert e["normalized_value"] == "z"
        assert e["raw_values"]["incident_id"] == row["incident_id"]


def test_locate_with_sample_rows_fixture(sample_rows):
    """用公共 sample_rows + evaluate（未截断）：行级清单条数 == unexpected_count 且可回指。"""
    entry = RuleEntry(id="SET", kind="set_membership", column="severity_level",
                      value_set=["A", "B", "E", "I"], basis="测试口径", params={})
    res = evaluate(entry, [dict(r) for r in sample_rows], {}).to_dict()
    evs = locate(res, sample_rows)
    assert len(evs) == res["unexpected_count"]
    # sample_rows 中仅第 2 行 severity_level='Z' 落在允许集合外
    assert [e["row_no"] for e in evs] == [2]
    assert evs[0]["file_name"] == "example.xlsx"
    assert evs[0]["sheet_name"] == "明细"
    assert evs[0]["batch_ref"] == "D01-2026-01"
