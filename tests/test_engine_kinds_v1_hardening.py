"""任务 10：规则族加固（空值三口径 / 归一 / 多格式日期 / 比例阈值边界）。

对齐源机制：集合成员性与格式校验允许按条目参数归一后再比对，样本保留原值与
归一后值两份；allowed_unexpected_ratio 让「低于阈值带病通过、超阈值判失败」，
但越界计数始终如实记录（unexpected_count 不因 success 被抹掉）。
"""
from __future__ import annotations

from ae_qc.engine.rule_kinds import evaluate
from ae_qc.models import RuleEntry


def _entry(rule_id: str, kind: str, column: str, **kw) -> RuleEntry:
    return RuleEntry(id=rule_id, kind=kind, column=column, basis="测试口径·加固边界", **kw)


def _run(entry: RuleEntry, rows: list[dict], ctx: dict | None = None):
    return evaluate(entry, [dict(r) for r in rows], ctx or {})


def test_null_three_kinds_counted_separately():
    """None / 空字符串 / 纯空白三种空值口径分别计数，并各自定位到行。"""
    e = _entry("NN", "not_null", "incident_id")
    rows = [
        {"_row_no": 1, "incident_id": None},
        {"_row_no": 2, "incident_id": ""},
        {"_row_no": 3, "incident_id": "   "},
        {"_row_no": 4, "incident_id": "EX-0004"},
    ]
    r = _run(e, rows)
    assert r.unexpected_count == 3
    assert r.observed_value["null_counts"] == {"none": 1, "empty": 1, "blank": 1}
    reasons = {s["reason"] for s in r.unexpected_samples}
    assert any("空值口径=none" in x for x in reasons)
    assert any("空值口径=empty" in x for x in reasons)
    assert any("空值口径=blank" in x for x in reasons)

    allowed = _entry("NN-ALLOW", "not_null", "incident_id",
                     params={"null_policy": "allow"})
    r2 = _run(allowed, rows)
    # null_policy=allow：三种空值不计越界，但 null_counts 仍如实分别统计。
    assert r2.unexpected_count == 0
    assert r2.observed_value["null_counts"] == {"none": 1, "empty": 1, "blank": 1}


def test_normalize_width_space_case_pass_and_keep_both_values():
    """全角 / 首尾空格 / 大小写差异归一后比对通过，样本保留原值与归一后值。"""
    e = _entry("SET", "set_membership", "severity_level", value_set=["A", "B"],
               params={"normalize": ["trim", "width", "casefold"]})
    rows = [
        {"_row_no": 1, "severity_level": "Ａ"},   # 全角 A
        {"_row_no": 2, "severity_level": " a "},  # 首尾空格 + 小写
        {"_row_no": 3, "severity_level": "B"},
    ]
    r = _run(e, rows)
    assert r.unexpected_count == 0
    assert r.success is True

    # 越界样本必须同时保留原值与归一后值两份，不丢原始证据。
    r2 = _run(e, [{"_row_no": 9, "severity_level": "Ｚ"}])
    assert r2.unexpected_count == 1
    sample = r2.unexpected_samples[0]
    assert sample["value"] == "Ｚ"           # 原值（全角）
    assert sample["normalized_value"] == "z"  # 归一后（半角小写）
    assert sample["value"] != sample["normalized_value"]


def test_multi_format_dates_normalized_pass():
    """多种常见日期/时间写法归一后均可解析通过，失败原因分类为空。"""
    e = _entry("TF", "type_format", "occur_time", params={"type": "iso_datetime"})
    rows = [
        {"_row_no": 1, "occur_time": "2026-01-02 10:00:00"},
        {"_row_no": 2, "occur_time": "2026/1/2 08:30"},
        {"_row_no": 3, "occur_time": "2026年1月2日"},
        {"_row_no": 4, "occur_time": "2026-01-02T10:00:00"},
    ]
    r = _run(e, rows)
    assert r.unexpected_count == 0
    assert r.success is True
    assert r.observed_value["declared_type"] == "iso_datetime"
    assert r.observed_value["failure_reasons"] == {}

    # 非法值需分类说明期望格式，并给出归一后值。
    r2 = _run(e, [{"_row_no": 5, "occur_time": "不是时间"}])
    assert r2.unexpected_count == 1
    assert r2.observed_value["failure_reasons"]
    assert isinstance(r2.unexpected_samples[0]["normalized_value"], str)


def _ratio_entry(threshold: float) -> RuleEntry:
    return _entry("RATIO", "set_membership", "severity_level", value_set=["A"],
                  params={"allowed_unexpected_ratio": threshold})


def test_allowed_ratio_equal_threshold_passes_but_records_count():
    """比例恰好等于阈值：判 success（带病通过）但 unexpected_count 如实记录。"""
    rows = [{"_row_no": i, "severity_level": "Z" if i == 1 else "A"} for i in range(1, 11)]
    r = _run(_ratio_entry(0.1), rows)
    assert r.unexpected_count == 1          # 越界仍计数，不因 success 被抹掉
    assert r.success is True                # 1/10 = 0.1 ≤ 阈值
    assert r.observed_value["unexpected_ratio"] == 0.1
    assert r.observed_value["allowed_unexpected_ratio"] == 0.1
    assert "带病通过" in r.observed_value["note"]


def test_allowed_ratio_exceed_threshold_fails():
    """比例超过阈值：判 success=False，并在 observed_value 中体现比例。"""
    rows = [{"_row_no": i, "severity_level": "Z" if i <= 2 else "A"} for i in range(1, 11)]
    r = _run(_ratio_entry(0.1), rows)
    assert r.unexpected_count == 2
    assert r.success is False               # 2/10 = 0.2 > 阈值
    assert r.observed_value["unexpected_ratio"] == 0.2
    assert r.observed_value["allowed_unexpected_ratio"] == 0.1
    assert "超过阈值" in r.observed_value["note"]
