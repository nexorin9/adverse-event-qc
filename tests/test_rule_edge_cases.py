"""任务 22：7 种规则族 × ≥3 个边界用例，参数化一次跑完。

每个用例都写明「期望行为 + 为什么这样期望」；用例数与 EDGE_INDEX.md 索引表
条数一致，且与规则族一一对应。执行真源是底座 engine.rule_kinds.evaluate。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from ae_qc.engine import rule_kinds
from ae_qc.models import RULE_KINDS, RuleEntry

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CASES = json.loads((FIXTURES / "rule_edge_cases.json").read_text(encoding="utf-8"))["cases"]
INDEX_MD = (FIXTURES / "EDGE_INDEX.md").read_text(encoding="utf-8")


def _assert_expectation(case: dict, exp: dict) -> None:
    if "load_error" in exp:
        # 加载期拒绝：构造 RuleEntry 时抛错并点名原因（pydantic ValidationError 是 ValueError 子类）
        with pytest.raises(ValueError) as caught:
            RuleEntry.model_validate(case["entry"])
        assert exp["load_error"] in str(caught.value)
        return

    entry = RuleEntry.model_validate(case["entry"])
    res = rule_kinds.evaluate(entry, case["rows"], case.get("context", {}))

    if "success" in exp:
        assert bool(res.success) is bool(exp["success"]), case["id"]
    if "unexpected_count" in exp:
        assert res.unexpected_count == exp["unexpected_count"], case["id"]
    if "info_findings_count" in exp:
        assert len(res.info_findings) == exp["info_findings_count"], case["id"]
    for key, want in exp.get("observed", {}).items():
        assert res.observed_value.get(key) == want, (case["id"], key)
    for key, length in exp.get("observed_len", {}).items():
        assert len(res.observed_value.get(key, [])) == length, (case["id"], key)
    for flag, want in exp.get("sample_flags", {}).items():
        assert res.unexpected_samples, case["id"]
        assert all(bool(s.get(flag)) is bool(want) for s in res.unexpected_samples), (case["id"], flag)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_edge_case(case):
    # 每条用例字段齐备，且写明「期望行为 + 为什么这样期望」
    assert {"kind", "case", "expectation", "why", "rows", "entry"} <= set(case)
    assert case["kind"] in RULE_KINDS
    assert case["case"] and case["why"]
    assert "期望" in case["why"] and "原因" in case["why"]
    assert isinstance(case["rows"], list) and case["rows"]
    assert isinstance(case["entry"], dict) and case["entry"]["basis"]
    _assert_expectation(case, case["expectation"])


def test_case_count_matches_index_table():
    index_ids = re.findall(r"\|\s*([A-Z]{2,3}-\d{2})\s*\|", INDEX_MD)
    assert len(CASES) >= 21
    assert len(index_ids) == len(CASES)
    assert set(index_ids) == {c["id"] for c in CASES}


def test_edge_cases_one_to_one_with_rule_families():
    assert {c["kind"] for c in CASES} == set(RULE_KINDS)
    counts = Counter(c["kind"] for c in CASES)
    assert all(n >= 3 for n in counts.values())

    data_rows = [ln for ln in INDEX_MD.splitlines()
                 if re.match(r"^\|\s*[A-Z]{2,3}-\d{2}\s*\|", ln)]
    assert len(data_rows) == len(CASES)
    md_counts: Counter = Counter()
    for ln in data_rows:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        md_counts[cells[1]] += 1
    assert dict(md_counts) == dict(counts)  # 索引表与 JSON 逐族一一对应
