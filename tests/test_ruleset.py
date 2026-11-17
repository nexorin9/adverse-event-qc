"""任务 2：期望套件 schema 校验、加载、版本号管理与条目级 diff。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ae_qc.engine.ruleset import (
    RULESET_DIR, basis_snapshot, diff_rulesets, latest_version, list_versions,
    load_ruleset, load_ruleset_for_period,
)
from ae_qc.errors import RuleError
from ae_qc.models import RuleEntry, RuleSet


def _write_suite(tmp_path: Path, name: str, entries: list[dict]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"version": path.stem, "entries": entries}, ensure_ascii=False),
                    encoding="utf-8")
    return path


def test_valid_suite_loads():
    rs = load_ruleset("v2026-01")
    assert isinstance(rs, RuleSet)
    assert rs.version == "v2026-01"
    assert len(rs.entries) >= 9
    kinds = {e.kind for e in rs.entries}
    assert "set_membership" in kinds and "catalog_coverage" in kinds
    assert all(e.basis for e in rs.entries)


def test_empty_value_set_rejected_with_line_number(tmp_path: Path):
    path = tmp_path / "empty_set.yaml"
    path.write_text(
        "version: vtest\nentries:\n  - id: AE-BAD\n    kind: set_membership\n"
        "    column: severity_level\n    value_set: []\n    basis: 测试口径\n",
        encoding="utf-8",
    )
    with pytest.raises(RuleError) as exc:
        load_ruleset(path)
    assert "第" in str(exc.value) and "行" in str(exc.value)
    assert "value_set" in str(exc.value)


def test_unknown_kind_rejected():
    with pytest.raises(Exception) as exc:
        RuleEntry(id="AE-X", kind="magic_rule", column="a", value_set=["x"], basis="测试")
    assert "未知规则族" in str(exc.value) or "kind" in str(exc.value)


def test_missing_basis_rejected():
    with pytest.raises(Exception) as exc:
        RuleEntry(id="AE-X", kind="not_null", column="incident_id", basis="  ")
    assert "basis" in str(exc.value)


def test_diff_lists_added_and_modified():
    diff = diff_rulesets("v2026-01", "v2026-02")
    assert "AE-SEV-CLASS-SET" in diff["added"]
    assert "AE-SEV-LEVEL" in diff["modified"]
    assert diff["removed"] == []


def test_version_selection_and_basis_snapshot():
    assert "v2026-01" in list_versions() and "v2026-02" in list_versions()
    _, version_old = load_ruleset_for_period("2026-01")
    _, version_new = load_ruleset_for_period("2026-03")
    assert version_old == "v2026-01"
    assert version_new == "v2026-02"
    _, locked = load_ruleset_for_period("2026-03", locked="v2026-01")
    assert locked == "v2026-01"
    snap = basis_snapshot(load_ruleset("v2026-01"))
    assert len(snap) == len(load_ruleset("v2026-01").entries)
    assert all(snap.values())


def test_ruleset_dir_is_inside_project():
    assert RULESET_DIR.exists()
    assert len(list(RULESET_DIR.glob("*.yaml"))) >= 2
    assert latest_version() == "v2026-02"
