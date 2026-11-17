"""任务 9：三族（set_membership / not_null / type_format）各一正一反。

对齐源机制口径：``unexpected_count → success = (unexpected_count == 0)
→ partial_unexpected_list``（本产品为 unexpected_samples）。验证 unexpected_count
与展示样本条数一致、success 判定严格等价于 unexpected_count == 0。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ae_qc.engine.rule_kinds import evaluate
from ae_qc.models import RuleEntry

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "rule_pairs"


def _entry(rule_id: str, kind: str, column: str, **kw) -> RuleEntry:
    return RuleEntry(id=rule_id, kind=kind, column=column, basis="测试口径·边界样例", **kw)


def _run(entry: RuleEntry, rows: list[dict], ctx: dict | None = None):
    # 每行浅拷贝：alias_resolution 会写 _alias_hit，避免用例间互相污染。
    return evaluate(entry, [dict(r) for r in rows], ctx or {})


def test_set_membership_positive():
    e = _entry("SET", "set_membership", "severity_level", value_set=["A", "B", "I"])
    r = _run(e, [{"_row_no": 1, "severity_level": "A"}, {"_row_no": 2, "severity_level": "I"}])
    assert r.success is True
    assert r.unexpected_count == 0
    assert r.unexpected_samples == []


def test_set_membership_negative():
    e = _entry("SET", "set_membership", "severity_level", value_set=["A", "B", "I"])
    r = _run(e, [{"_row_no": 1, "severity_level": "Z"}, {"_row_no": 2, "severity_level": ""}])
    assert r.success is False
    assert r.unexpected_count == 2
    assert {s["row_no"] for s in r.unexpected_samples} == {1, 2}


def test_not_null_positive():
    e = _entry("NN", "not_null", "incident_id")
    r = _run(e, [{"_row_no": 1, "incident_id": "EX-0001"}, {"_row_no": 2, "incident_id": "EX-0002"}])
    assert r.success is True
    assert r.unexpected_count == 0


def test_not_null_negative():
    e = _entry("NN", "not_null", "incident_id")
    r = _run(e, [
        {"_row_no": 1, "incident_id": None},
        {"_row_no": 2, "incident_id": ""},
        {"_row_no": 3, "incident_id": "   "},
    ])
    assert r.success is False
    assert r.unexpected_count == 3
    assert {s["row_no"] for s in r.unexpected_samples} == {1, 2, 3}


def test_type_format_positive():
    e = _entry("TF", "type_format", "occur_time", params={"type": "iso_datetime"})
    r = _run(e, [
        {"_row_no": 1, "occur_time": "2026-01-02 10:00:00"},
        {"_row_no": 2, "occur_time": "2026/01/03 08:30"},
    ])
    assert r.success is True
    assert r.unexpected_count == 0


def test_type_format_negative():
    e = _entry("TF", "type_format", "occur_time", params={"type": "iso_datetime"})
    r = _run(e, [{"_row_no": 1, "occur_time": "不是时间"}, {"_row_no": 2, "occur_time": ""}])
    assert r.success is False
    assert r.unexpected_count == 2
    assert {s["row_no"] for s in r.unexpected_samples} == {1, 2}


def test_unexpected_count_matches_sample_count():
    """unexpected_count == 展示样本条数 == total_count（未截断时三者相等）。"""
    cases = [
        (_entry("SET", "set_membership", "severity_level", value_set=["A"]),
         [{"_row_no": 1, "severity_level": "Z"}, {"_row_no": 2, "severity_level": ""}]),
        (_entry("NN", "not_null", "incident_id"),
         [{"_row_no": 1, "incident_id": None}, {"_row_no": 2, "incident_id": ""}]),
        (_entry("TF", "type_format", "occur_time", params={"type": "iso_datetime"}),
         [{"_row_no": 1, "occur_time": "非法"}, {"_row_no": 2, "occur_time": ""}]),
    ]
    for entry, rows in cases:
        r = _run(entry, rows)
        assert r.unexpected_count == len(r.unexpected_samples) == r.total_count


def test_success_equivalent_to_zero_unexpected():
    """success 严格等价于 unexpected_count == 0（ratio=0 口径）。"""
    cases = [
        (_entry("SET", "set_membership", "severity_level", value_set=["A"]),
         [{"_row_no": 1, "severity_level": "A"}]),
        (_entry("SET2", "set_membership", "severity_level", value_set=["A"]),
         [{"_row_no": 1, "severity_level": "Z"}]),
        (_entry("NN", "not_null", "incident_id"),
         [{"_row_no": 1, "incident_id": "X"}]),
        (_entry("NN2", "not_null", "incident_id"),
         [{"_row_no": 1, "incident_id": None}]),
        (_entry("TF", "type_format", "occur_time", params={"type": "iso_datetime"}),
         [{"_row_no": 1, "occur_time": "2026-01-02 10:00:00"}]),
        (_entry("TF2", "type_format", "occur_time", params={"type": "iso_datetime"}),
         [{"_row_no": 1, "occur_time": "非法"}]),
    ]
    for entry, rows in cases:
        r = _run(entry, rows)
        assert r.success == (r.unexpected_count == 0)


@pytest.mark.parametrize("kind", ["set_membership", "not_null", "type_format"])
def test_rule_pairs_fixture_contract(kind: str):
    """用 tests/fixtures/rule_pairs 的声明式边界样例复核三族正/反行为。"""
    data = json.loads((FIXTURES / f"{kind}.json").read_text(encoding="utf-8"))
    entry = RuleEntry.model_validate(data["entry"])
    ctx = dict(data.get("context") or {})
    for which in ("positive", "negative"):
        case = data[which]
        r = _run(entry, case["rows"], ctx)
        assert r.success == case["expect"]["success"], f"{kind}/{which} success 不符"
        assert r.unexpected_count == case["expect"]["unexpected_count"], f"{kind}/{which} 计数不符"
