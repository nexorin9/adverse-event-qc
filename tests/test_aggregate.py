"""任务 20：批次通过率与规则族 / 科室维度聚合（界面 / 导出 / 摘要同源）。

覆盖：通过率口径；skipped 不入分母；三处数字一致；按族与按科室聚合条数正确；
展示样本被截断时，全量 unexpected_detail 仍保证聚合口径不变。
"""
from __future__ import annotations

from collections import Counter

from ae_qc.engine import aggregate as ag
from ae_qc.engine import executor as ex
from ae_qc.engine.result import skipped_result
from ae_qc.models import STANDARD_COLUMNS


def _run(ruleset, rows, aliases, limit=2):
    plan = ex.build_plan(ruleset, list(STANDARD_COLUMNS))
    return ex.execute_plan(plan, rows, {"aliases": aliases, "limit": limit})["results"]


def test_pass_rate_and_denominator_exclude_skipped(ruleset_v1, sample_rows, aliases):
    results = _run(ruleset_v1, sample_rows, aliases)
    skipped = skipped_result("AE-BROKEN", "severity_level", "注入失败").to_dict()
    agg = ag.aggregate(results + [skipped], sample_rows)

    assert agg["denominator"] == len(results)  # skipped 排除在分母外
    assert agg["excluded_skipped"] == 1
    assert "AE-BROKEN" in agg["excluded_skip_rule_ids"]
    assert agg["total_rules"] == len(results) + 1
    assert agg["executed_rules"] == len(results)
    expected_pass = sum(1 for r in results if r["success"])
    assert agg["pass_count"] == expected_pass
    assert agg["fail_count"] == len(results) - expected_pass
    assert agg["pass_rate"] == round(expected_pass / len(results), 6)
    assert agg["pass_count"] + agg["fail_count"] == agg["denominator"]


def test_all_skipped_gives_zero_denominator():
    skipped = [skipped_result("R1", "c", "崩了").to_dict(),
               skipped_result("R2", "c", "崩了").to_dict()]
    agg = ag.aggregate(skipped, [])
    assert agg["denominator"] == 0
    assert agg["pass_rate"] == 0.0
    assert agg["excluded_skipped"] == 2
    assert agg["total_unexpected_rows"] == 0


def test_display_export_summary_numbers_identical(ruleset_v1, sample_rows, aliases):
    results = _run(ruleset_v1, sample_rows, aliases)
    agg = ag.aggregate(results, sample_rows)

    ui = ag.numbers_for_display(agg)
    export = ag.numbers_for_export(agg)
    summary = ag.numbers_for_summary(agg)
    assert ui == export == summary  # 三处共用同一份聚合结果

    for key in ("pass_rate", "denominator", "pass_count", "fail_count",
                "total_unexpected_rows", "excluded_skipped"):
        assert ui[key] == agg[key]
    assert ui["rule_count"] == agg["executed_rules"]
    # 数字与全量明细一致，不受展示截断影响
    assert ui["total_unexpected_rows"] == sum(len(r["unexpected_detail"]) for r in results)


def test_by_kind_and_by_dept_counts(ruleset_v1, sample_rows, aliases):
    results = _run(ruleset_v1, sample_rows, aliases)
    agg = ag.aggregate(results, sample_rows)

    assert sum(v["rule_count"] for v in agg["by_kind"].values()) == agg["denominator"]
    assert sum(v["unexpected_rows"] for v in agg["by_kind"].values()) == agg["total_unexpected_rows"]
    assert sum(v["unexpected_rows"] for v in agg["by_dept"].values()) == agg["total_unexpected_rows"]

    expected_kinds = dict(Counter(r["kind"] for r in results))
    assert {k: v["rule_count"] for k, v in agg["by_kind"].items()} == expected_kinds
    for kind, counts in agg["by_kind"].items():
        assert counts["pass_count"] + counts["fail_count"] == counts["rule_count"]
    for dept, counts in agg["by_dept"].items():
        assert dept and counts["unexpected_rows"] > 0 and counts["rule_ids"]


def test_truncated_display_samples_do_not_change_aggregate(ruleset_v1, sample_rows, aliases):
    full = _run(ruleset_v1, sample_rows, aliases, limit=1000)
    small = _run(ruleset_v1, sample_rows, aliases, limit=1)
    # 确实发生了展示层截断
    assert any(len(r["unexpected_samples"]) < len(r["unexpected_detail"]) for r in small)

    agg_full = ag.aggregate(full, sample_rows)
    agg_small = ag.aggregate(small, sample_rows)
    assert agg_full["total_unexpected_rows"] == agg_small["total_unexpected_rows"]
    assert agg_full["pass_rate"] == agg_small["pass_rate"]
    assert agg_full["by_kind"] == agg_small["by_kind"]
    assert agg_full["by_dept"] == agg_small["by_dept"]


def test_info_findings_not_counted_as_unexpected_rows():
    # 提示级反向覆盖发现：unexpected_count=0，但 info_findings 非空
    info_result = {
        "rule_id": "AE-CATALOG-COVERAGE", "column": "event_type_code",
        "success": True, "unexpected_count": 0, "unexpected_samples": [],
        "status": "executed", "severity": "info", "kind": "catalog_coverage",
        "info_findings": [{"severity": "info", "missing_categories": ["AE-CAT-02"]}],
    }
    agg = ag.aggregate([info_result], [])
    assert agg["total_unexpected_rows"] == 0
    assert agg["by_kind"]["catalog_coverage"]["unexpected_rows"] == 0
    assert agg["pass_rate"] == 1.0
