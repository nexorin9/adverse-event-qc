"""任务 14：单规则失败隔离 + skipped 汇总与原因点名。

对齐源机制「对单项计算开启捕获异常」：抛错的规则进已中止清单而不拖垮整批，
skipped 明确显示「未执行」、不带入通过率分母，结果摘要显式列出被跳过规则数
与原因分类计数。
"""
from __future__ import annotations

from ae_qc.engine import executor as ex
from ae_qc.models import STANDARD_COLUMNS


def _ctx(aliases=None, **extra) -> dict:
    return {"aliases": aliases or {}, "limit": 5, **extra}


def _plan(ruleset_v1) -> dict:
    return ex.build_plan(ruleset_v1, list(STANDARD_COLUMNS))


def test_baseline_all_executed(ruleset_v1, sample_rows, aliases):
    plan = _plan(ruleset_v1)
    out = ex.execute_plan(plan, sample_rows, _ctx(aliases))
    assert out["skipped"] == []
    assert len(out["results"]) == len(plan["items"])
    assert out["summary"]["denominator"] == len(out["results"])
    assert out["summary"]["skipped_count"] == 0


def test_injected_failure_isolated_others_unchanged(ruleset_v1, sample_rows, aliases):
    plan = _plan(ruleset_v1)
    target = "AE-INCIDENT-ID"   # 默认模式下该规则恰好 1 个计划项
    baseline = ex.execute_plan(plan, sample_rows, _ctx(aliases))
    injected = ex.execute_plan(plan, sample_rows, _ctx(aliases, _force_fail_rule=target))

    # 注入失败只让该条进 skipped，其余规则结果条数与基线一致（逐条完全相等）。
    assert len(injected["results"]) == len(baseline["results"]) - 1
    others = [r for r in baseline["results"] if r["rule_id"] != target]
    assert injected["results"] == others

    assert len(injected["skipped"]) == 1
    s = injected["skipped"][0]
    assert s["rule_id"] == target
    assert s["status"] == "skipped"
    assert s["exception_type"] == "RuntimeError"
    assert s["column"] == "incident_id"
    assert "隔离" in s["skip_reason"] and s["skip_reason"]
    assert s["sample_row"] and "row_no" in s["sample_row"]


def test_skipped_excluded_from_pass_rate_denominator(ruleset_v1, sample_rows, aliases):
    plan = _plan(ruleset_v1)
    target = "AE-SEV-LEVEL"
    baseline = ex.execute_plan(plan, sample_rows, _ctx(aliases))
    injected = ex.execute_plan(plan, sample_rows, _ctx(aliases, _force_fail_rule=target))
    summ = injected["summary"]

    # 通过率分母 = executed 条数，skipped 显式排除、不静默计入。
    assert summ["denominator"] == len(injected["results"])
    assert summ["denominator"] == baseline["summary"]["denominator"] - 1
    assert summ["executed"] == len(injected["results"])
    assert summ["skipped_count"] == 1
    assert summ["excluded_skipped"] == 1
    assert summ["skip_reasons"].get("RuntimeError") == 1
    assert target in summ["skip_rule_ids"]
    assert summ["pass_rate"] == round(summ["pass_count"] / summ["denominator"], 6)


def test_call_hook_can_inject_isolated_failure(ruleset_v1, sample_rows, aliases):
    """call_hook 抛异常同样被隔离：其余规则照跑，异常类型与规则 id 被点名。"""
    plan = _plan(ruleset_v1)

    def hook(entry, rows, ctx):
        if entry.id == "AE-TIME-ORDER":
            raise ValueError("hook injected failure")
        return None   # 其余规则仍走 evaluate

    out = ex.execute_plan(plan, sample_rows, _ctx(aliases), call_hook=hook)
    # AE-TIME-ORDER 在默认模式下有 2 个计划项（occur_time / report_time），两条都抛。
    assert len(out["skipped"]) == 2
    assert all(s["rule_id"] == "AE-TIME-ORDER" for s in out["skipped"])
    assert all(s["exception_type"] == "ValueError" for s in out["skipped"])
    assert all(r["rule_id"] != "AE-TIME-ORDER" for r in out["results"])
    assert len(out["results"]) == len(plan["items"]) - 2
