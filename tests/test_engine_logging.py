"""任务 21：引擎结构化日志 + 错误码 + 每批运行摘要。

覆盖：每行日志可 json.loads 且字段齐；错误码表每码有说明；运行摘要数字与实际
结果一致；并行追加时只断言自身事件存在，不做全局计数断言。
"""
from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor

from ae_qc.engine import engine_logging as el
from ae_qc.errors import ERROR_CODES
from ae_qc.logging_util import ERROR_LOG_PATH, LOG_FIELDS, RunSummary, read_events


def _results():
    return [
        {"rule_id": "R-OK", "column": "severity_level", "success": True,
         "unexpected_count": 0, "elapsed_ms": 1.5,
         "rule_set_version": "v2026-01", "kind": "set_membership", "status": "executed"},
        {"rule_id": "R-FAIL", "column": "occur_time", "success": False,
         "unexpected_count": 3, "elapsed_ms": 2.5,
         "rule_set_version": "v2026-01", "kind": "time_order", "status": "executed"},
        {"rule_id": "R-SKIP", "column": "severity_class", "status": "skipped",
         "skip_reason": "注入失败被隔离", "unexpected_count": 0, "elapsed_ms": 0.0,
         "rule_set_version": "v2026-01", "kind": "cross_column_derived"},
    ]


def test_log_lines_are_json_with_all_fields():
    batch_ref = "TEST-LOG-" + uuid.uuid4().hex[:10]
    summary = el.log_run(_results(), batch_ref)

    mine = [e for e in read_events() if e.get("batch_ref") == batch_ref]
    assert len(mine) >= len(_results())  # 只断言自身事件存在，不做全局计数
    for event in mine:
        for field in LOG_FIELDS:
            assert field in event, f"日志缺字段 {field}"
        assert isinstance(event["elapsed_ms"], float)
        assert event["level"] in ("INFO", "WARN", "ERROR")
    skip_events = [e for e in mine if e.get("rule_id") == "R-SKIP"]
    assert skip_events
    assert any(e["code"] == "RULE_EXEC_FAILED" and e["code"] in ERROR_CODES
               for e in skip_events)  # 错误码引用底座表
    assert isinstance(summary, RunSummary)


def test_error_code_catalog_every_code_documented():
    catalog = el.code_catalog()
    assert catalog == ERROR_CODES  # 复用同一张表，不另造
    assert catalog
    for code, meta in catalog.items():
        assert meta.get("layer") and meta.get("desc") and meta.get("action"), code
    layers = {m["layer"] for m in catalog.values()}
    assert {"配置", "入站", "规则", "归因", "写回"} <= layers
    assert el.describe_code("RULE_EXEC_FAILED")["desc"]
    assert el.describe_code("NOT_A_REAL_CODE")["layer"] == "未分类"


def test_run_summary_numbers_match_results():
    batch_ref = "TEST-SUM-" + uuid.uuid4().hex[:10]
    d = el.run_summary(_results(), batch_ref=batch_ref,
                       source_mix={"file": 5, "platform": 0}, elapsed_ms=12.5)
    assert d["batch_ref"] == batch_ref
    assert d["rule_count"] == len(_results())
    assert d["skipped_rules"] == 1
    assert d["unexpected_rows"] == 3  # 只累加已执行结果的越界数
    assert d["elapsed_ms"] == 12.5
    assert d["source_mix"] == {"file": 5, "platform": 0}
    assert d["rule_set_version"] == "v2026-01"  # 从结果推断
    assert d["skipped_reasons"]


def test_log_run_summary_consistent():
    batch_ref = "TEST-RUN-" + uuid.uuid4().hex[:10]
    summary = el.log_run(_results(), batch_ref, source_mix={"file": 2})
    d = summary.to_dict()
    assert d["batch_ref"] == batch_ref
    assert d["rule_count"] == len(_results())
    assert d["unexpected_rows"] == 3
    assert d["skipped_rules"] == 1
    assert d["elapsed_ms"] == 4.0  # 1.5 + 2.5 + 0.0（结果累计耗时）
    assert d["source_mix"] == {"file": 2}
    assert d["rule_set_version"] == "v2026-01"


def test_parallel_append_lines_are_intact():
    refs = [f"TEST-PAR-{uuid.uuid4().hex[:8]}-{i}" for i in range(8)]

    def _write(ref):
        return el.log_run([
            {"rule_id": f"R-{ref}", "column": "c", "success": True, "unexpected_count": 0},
        ], ref)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_write, refs))

    lines = [ln for ln in ERROR_LOG_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()]
    parsed = [json.loads(ln) for ln in lines]  # 并行追加不撕裂：每行都是合法 JSON
    mine = [e for e in parsed if e.get("batch_ref") in refs]
    assert len(mine) >= len(refs)  # 自身事件都在，不做全局计数断言
    assert all(set(LOG_FIELDS) <= set(e) for e in mine)
