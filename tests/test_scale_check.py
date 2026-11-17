"""任务 54：≥500 行 × 10 科室 × 3 期跑批的正确性与性能双阈值验收。"""
from __future__ import annotations

from pathlib import Path

from scripts.scale_check import render_markdown, run_scale_check, threshold_seconds


def test_scale_and_correctness(tmp_path):
    result = run_scale_check(inbox_dir=tmp_path / "inbox", output=tmp_path / "conclusion.md")
    assert result["scale"]["rows"] >= 500
    assert result["scale"]["depts"] == 10 and result["scale"]["periods"] == 3
    assert result["scale"]["files"] == 30
    assert len(result["batches"]) == 30
    assert all(b["rows"] > 0 and b["rule_items"] > 0 for b in result["batches"])
    assert result["correctness"]["diff"] == 0
    assert result["correctness"]["per_batch_max_abs_diff"] == 0
    assert result["correctness"]["total_expected"] == result["correctness"]["total_detected"] > 0


def test_performance_threshold_is_enforced(tmp_path):
    normal = run_scale_check(inbox_dir=tmp_path / "inbox", max_seconds=15,
                             output=tmp_path / "normal.md")
    assert normal["threshold_passed"] is True
    assert normal["max_batch_seconds"] <= 15
    tight = run_scale_check(inbox_dir=tmp_path / "inbox", max_seconds=0.000001,
                            output=tmp_path / "tight.md")
    assert tight["threshold_passed"] is False
    assert tight["max_batch_seconds"] > tight["threshold_seconds"]


def test_threshold_comes_from_config_and_can_be_overridden():
    assert threshold_seconds() == 15.0
    assert threshold_seconds(3.5) == 3.5


def test_script_repeatable_and_document_has_numbers(tmp_path):
    first = run_scale_check(inbox_dir=tmp_path / "inbox", output=tmp_path / "a.md")
    second = run_scale_check(inbox_dir=tmp_path / "inbox", output=tmp_path / "a.md")
    assert first["scale"] == second["scale"]
    assert first["correctness"] == second["correctness"]
    assert len(first["batches"]) == len(second["batches"]) == 30
    text = Path(first["output_path"]).read_text(encoding="utf-8")
    assert "≥500 行" in text and "15" in text
    assert "未实连" in text and "人工抽查" in text
    assert "| 批次 |" in text  # 逐批耗时分布表
    assert render_markdown(first).count("D01-2026-01") >= 1


def test_unconnected_scope_is_declared(tmp_path):
    result = run_scale_check(inbox_dir=tmp_path / "inbox", output=tmp_path / "a.md")
    assert len(result["unconnected_endpoints"]) >= 3
    assert any("POST" in x for x in result["unconnected_endpoints"])
    assert any("模型" in x for x in result["unconnected_endpoints"])
