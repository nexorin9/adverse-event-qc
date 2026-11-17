"""任务 19：套件版本生效 + 历史批次可复现。

覆盖：结果绑定版本号与 basis 快照；旧版本重跑与原始逐条一致；升版后旧批次结论
不变；basis 快照可回看（不依赖当前套件文件）。
"""
from __future__ import annotations

from ae_qc.engine import executor as ex
from ae_qc.engine import ruleset as rs_mod
from ae_qc.engine import ruleset_version as rv
from ae_qc.models import STANDARD_COLUMNS


def _ctx(aliases):
    return {
        "aliases": aliases,
        "limit": 20,
        "available_columns": list(STANDARD_COLUMNS),
    }


def _baseline(ruleset, rows, aliases):
    """一次跑完的基线：常规 build_plan + execute_plan。"""
    plan = ex.build_plan(ruleset, list(STANDARD_COLUMNS))
    return ex.execute_plan(plan, rows, {"aliases": aliases, "limit": 20})["results"]


def test_bind_version_writes_version_and_basis_snapshot(ruleset_v1):
    raw = {"rule_id": "AE-SEV-LEVEL", "column": "severity_level",
           "success": False, "unexpected_count": 2}
    bound = rv.bind_version(raw, ruleset_v1)

    assert bound["rule_set_version"] == "v2026-01"
    assert set(bound["basis_snapshot"]) == {e.id for e in ruleset_v1.entries}
    assert bound["basis_snapshot"]["AE-SEV-LEVEL"] == ruleset_v1.entry_map()["AE-SEV-LEVEL"].basis
    assert "《医疗质量安全不良事件分级分类标准》" in bound["basis"]  # 缺 basis 时从快照补齐
    assert "rule_set_version" not in raw  # 不改动入参


def test_replay_with_version_matches_original(ruleset_v1, sample_rows, aliases):
    baseline = _baseline(ruleset_v1, sample_rows, aliases)
    replay = rv.replay_with_version(sample_rows, "v2026-01", _ctx(aliases))

    # 计划项数 = 规则 × 其适用列（time_order / cross_column_derived 各占两列）
    plan_items = ex.build_plan(ruleset_v1, list(STANDARD_COLUMNS))["items"]
    assert len(replay) == len(baseline) == len(plan_items)
    assert len(plan_items) > len(ruleset_v1.entries)
    assert replay == baseline  # 逐条一致（结构 / 计数 / 样本 / 版本）
    assert all(r["rule_set_version"] == "v2026-01" for r in replay)


def test_upgrade_does_not_change_old_batch_conclusion(ruleset_v1, sample_rows, aliases):
    # 当前最新生效版本已是 v2026-02（新增了规则条目）
    current, current_version = rs_mod.load_ruleset_for_period("2026-03")
    assert current_version == "v2026-02"
    assert "AE-SEV-CLASS-SET" in {e.id for e in current.entries}

    baseline = _baseline(ruleset_v1, sample_rows, aliases)
    replay_v1 = rv.replay_with_version(sample_rows, "v2026-01", _ctx(aliases))
    assert replay_v1 == baseline  # 升版后旧批次结论不变

    v2 = rv.replay_with_version(sample_rows, "v2026-02", _ctx(aliases))
    v1_ids = {r["rule_id"] for r in replay_v1}
    v2_ids = {r["rule_id"] for r in v2}
    assert "AE-SEV-CLASS-SET" in v2_ids and "AE-SEV-CLASS-SET" not in v1_ids
    assert len(v2) > len(replay_v1)  # 版本确实生效，但不动旧批次结论


def test_basis_snapshot_readable_after_upgrade(ruleset_v1, sample_rows, aliases):
    bound = [rv.bind_version(r, ruleset_v1)
             for r in _baseline(ruleset_v1, sample_rows, aliases)]
    current = rs_mod.load_ruleset("v2026-02")

    assert all(b["rule_set_version"] == "v2026-01" for b in bound)
    for b in bound:
        assert b["basis"]  # 每条结果都带依据
        assert b["basis_snapshot"].get(b["rule_id"])  # 快照可回看当时的 basis
    # 版本确实变了，但快照是绑定时的那份，不依赖当前套件文件
    assert "AE-SEV-CLASS-SET" in rs_mod.basis_snapshot(current)
    assert "AE-SEV-CLASS-SET" not in rs_mod.basis_snapshot(ruleset_v1)


def test_diff_confirms_the_upgrade():
    diff = rs_mod.diff_rulesets("v2026-01", "v2026-02")
    assert "AE-SEV-CLASS-SET" in diff["added"]
    assert "AE-SEV-LEVEL" in diff["modified"]
