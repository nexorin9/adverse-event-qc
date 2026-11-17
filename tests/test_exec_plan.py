"""任务 13：执行计划（规则 × 可用列笛卡尔积 / 缺列不生成 / 剔除审计 / 回放一致）。

对齐源机制「指标 × 列」配置清单 + 逐条剔除 + 计划可导出事前复核。
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from ae_qc.engine import executor as ex
from ae_qc.models import STANDARD_COLUMNS


def test_plan_item_fields_and_version(ruleset_v1):
    plan = ex.build_plan(ruleset_v1, list(STANDARD_COLUMNS))
    assert plan["version"] == "v2026-01"
    assert plan["items"]
    for it in plan["items"]:
        for key in ("plan_id", "rule_id", "column", "params", "basis", "kind", "severity"):
            assert key in it, f"计划项缺少 {key}"
        assert it["basis"]
        assert isinstance(it["params"], dict)
    # plan_id 唯一且确定
    ids = [it["plan_id"] for it in plan["items"]]
    assert len(ids) == len(set(ids))


def test_default_plan_missing_column_not_generated(ruleset_v1):
    """默认模式：规则 × 其适用列；适用列不在本批可用列时不生成并记审计。"""
    cols = [c for c in STANDARD_COLUMNS if c != "dept_code"]
    plan = ex.build_plan(ruleset_v1, cols)
    rule_ids = {it["rule_id"] for it in plan["items"]}
    assert "AE-DEPT-CODE" not in rule_ids
    assert all(it["column"] in cols for it in plan["items"])
    assert any(
        a["action"] == "missing_column" and a["rule_id"] == "AE-DEPT-CODE"
        and a["column"] == "dept_code"
        for a in plan["audit"]
    )


def test_cartesian_product_count(ruleset_v1):
    """cross_product=True：计划条数 = 规则数 × 可用列数，每条规则配齐每个可用列。"""
    cols = ["severity_level", "incident_id", "occur_time"]
    plan = ex.build_plan(ruleset_v1, cols, cross_product=True)
    assert len(plan["items"]) == len(ruleset_v1.entries) * len(cols) == 9 * 3
    assert {it["column"] for it in plan["items"]} == set(cols)
    per_rule = Counter(it["rule_id"] for it in plan["items"])
    assert set(per_rule.values()) == {len(cols)}
    assert set(per_rule) == {e.id for e in ruleset_v1.entries}


def test_exclusion_by_rule_and_column_with_audit(ruleset_v1):
    cols = list(STANDARD_COLUMNS)
    base = ex.build_plan(ruleset_v1, cols)
    plan = ex.build_plan(ruleset_v1, cols, exclusions={
        "rules": ["AE-SEV-LEVEL", "AE-INCIDENT-ID"],
        "columns": ["event_type_text"],
    })
    ids = {it["rule_id"] for it in plan["items"]}
    assert "AE-SEV-LEVEL" not in ids          # 按规则 id 剔除
    assert "AE-INCIDENT-ID" not in ids
    assert "AE-ALIAS-RESOLUTION" not in ids   # 其适用列 event_type_text 被按列名剔除
    assert len(plan["items"]) < len(base["items"])

    excluded = [a for a in plan["audit"] if a["action"] == "excluded"]
    assert excluded
    assert any(a["rule_id"] == "AE-SEV-LEVEL" for a in excluded)
    assert any(a["column"] == "event_type_text" for a in excluded)
    assert all("剔除" in a["message"] and a["reason"] for a in excluded)


def test_exclusion_by_string_token(ruleset_v1):
    plan = ex.build_plan(ruleset_v1, list(STANDARD_COLUMNS), exclusions=["AE-TIME-ORDER"])
    assert "AE-TIME-ORDER" not in {it["rule_id"] for it in plan["items"]}
    assert any(a["action"] == "excluded" and a["rule_id"] == "AE-TIME-ORDER"
               for a in plan["audit"])


def test_replay_plan_matches_direct_execution(ruleset_v1, sample_rows, aliases, tmp_path):
    """同一份导出的计划回放，得到与直接执行完全相同的一批结果。"""
    plan = ex.build_plan(ruleset_v1, list(STANDARD_COLUMNS))
    ctx = {"aliases": aliases, "limit": 3}
    direct = ex.execute_plan(plan, sample_rows, ctx)

    path = tmp_path / "plan.json"
    written = ex.export_plan(plan, path)
    assert Path(written).exists()

    replay = ex.replay_plan(path, sample_rows, ctx)
    assert replay["results"] == direct["results"]
    assert replay["skipped"] == direct["skipped"]
    assert replay["summary"] == direct["summary"]

    # 导出的 JSON 可供质控员复核规则 id / 列 / 参数 / basis
    payload = json.loads(Path(written).read_text(encoding="utf-8"))
    assert payload["version"] == "v2026-01"
    assert all("basis" in it and "params" in it and "rule_id" in it
               for it in payload["items"])
