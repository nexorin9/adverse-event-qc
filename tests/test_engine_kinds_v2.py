"""任务 11：补齐三族（time_order / cross_column_derived / alias_resolution）正反例。

验证跨时序倒挂被捕获、派生不一致给出两侧实际值、别名精确命中与未命中可区分。
未命中是「待模型归并」信号，而非空值。
"""
from __future__ import annotations

from ae_qc.engine.rule_kinds import evaluate
from ae_qc.models import RuleEntry


def _entry(rule_id: str, kind: str, column: str, **kw) -> RuleEntry:
    return RuleEntry(id=rule_id, kind=kind, column=column, basis="测试口径·v2边界", **kw)


def _run(entry: RuleEntry, rows: list[dict], ctx: dict | None = None):
    return evaluate(entry, [dict(r) for r in rows], ctx or {})


# ---------------- time_order ----------------

def test_time_order_positive_and_equal_pairs():
    e = _entry("TO", "time_order", "occur_time",
               params={"left": "occur_time", "right": "report_time", "direction": "lte"})
    rows = [
        {"_row_no": 1, "occur_time": "2026-01-02 10:00:00", "report_time": "2026-01-02 11:00:00"},
        {"_row_no": 2, "occur_time": "2026-01-03 09:00:00", "report_time": "2026-01-03 09:00:00"},
    ]
    r = _run(e, rows)
    assert r.unexpected_count == 0
    assert r.success is True
    assert r.observed_value["equal_pairs"] == 1     # 并列时间视为通过但计入观察值
    assert r.observed_value["checked_pairs"] == 2


def test_time_order_inversion_captured():
    e = _entry("TO", "time_order", "occur_time",
               params={"left": "occur_time", "right": "report_time", "direction": "lte"})
    rows = [{"_row_no": 1, "occur_time": "2026-01-05 12:00:00",
             "report_time": "2026-01-04 12:00:00"}]
    r = _run(e, rows)
    assert r.unexpected_count == 1
    assert r.success is False
    sample = r.unexpected_samples[0]
    assert "倒挂" in sample["reason"]
    assert sample["observed_value"]["delta_seconds"] > 0
    assert sample["observed_value"]["occur_time"] == "2026-01-05 12:00:00"
    assert sample["observed_value"]["report_time"] == "2026-01-04 12:00:00"


# ---------------- cross_column_derived ----------------

def test_cross_column_derived_positive():
    e = _entry("CCD", "cross_column_derived", "severity_level",
               params={"from_col": "severity_level", "derives": "severity_class"})
    rows = [
        {"_row_no": 1, "severity_level": "A", "severity_class": "Ⅳ"},
        {"_row_no": 2, "severity_level": "I", "severity_class": "Ⅰ"},
    ]
    r = _run(e, rows)
    assert r.unexpected_count == 0
    assert r.success is True


def test_cross_column_derived_mismatch_shows_both_sides():
    e = _entry("CCD", "cross_column_derived", "severity_level",
               params={"from_col": "severity_level", "derives": "severity_class"})
    rows = [{"_row_no": 1, "severity_level": "A", "severity_class": "Ⅰ"}]  # A 应为 Ⅳ
    r = _run(e, rows)
    assert r.unexpected_count == 1
    assert r.success is False
    mismatch = r.observed_value["mismatches"][0]
    assert mismatch["severity_level"] == "A"       # 来源列实际值
    assert mismatch["severity_class"] == "Ⅰ"       # 派生列实际值
    assert mismatch["expected"] == "Ⅳ"             # 应派生值
    assert r.unexpected_samples[0]["normalized_value"] == "Ⅳ"


# ---------------- alias_resolution ----------------

def test_alias_resolution_hit():
    e = _entry("AR", "alias_resolution", "event_type_text",
               params={"from_col": "event_type_text"})
    ctx = {"aliases": {"跌倒": "AE-CAT-08", "给药错误": "AE-CAT-02"}}
    rows = [
        {"_row_no": 1, "event_type_text": "跌倒"},
        {"_row_no": 2, "event_type_text": "给药错误"},
    ]
    r = _run(e, rows, ctx)
    assert r.unexpected_count == 0
    assert r.success is True
    assert r.observed_value["alias_hits"] == 2
    assert r.observed_value["alias_table_size"] == 2


def test_alias_resolution_miss_is_not_a_null_value():
    e = _entry("AR", "alias_resolution", "event_type_text",
               params={"from_col": "event_type_text"})
    ctx = {"aliases": {"跌倒": "AE-CAT-08"}}
    rows = [{"_row_no": 1, "event_type_text": "未登记的自由文本事件名"}]
    r = _run(e, rows, ctx)
    assert r.unexpected_count == 1
    assert r.success is False
    sample = r.unexpected_samples[0]
    assert sample["needs_model_merge"] is True
    assert sample["normalized_value"]          # 归一后值非空
    assert "未命中" in sample["reason"]


def test_alias_resolution_with_catalog_fixture(aliases, clean_rows):
    """用公共目录（catalogs/aliases.csv）与 clean_rows：三行事件名均可精确命中。"""
    e = _entry("AR", "alias_resolution", "event_type_text",
               params={"from_col": "event_type_text"})
    r = _run(e, clean_rows, {"aliases": aliases})
    assert r.unexpected_count == 0
    assert r.success is True
    assert r.observed_value["alias_hits"] == len(clean_rows)
