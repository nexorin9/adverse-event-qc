"""入站 E 批次摘要专项测试：九键结构、来源构成、脏数据分类、增量/全量与对比。"""
from __future__ import annotations

from ae_qc.ingest.batch_summary import build_summary, compare_summaries
from ae_qc.ingest.xlsx_reader import BatchFrame


def _frame(dept: str, period: str, *, rows: list | None = None, n: int = 2,
           skipped: dict | None = None, pending: list | None = None,
           errors: list | None = None, source: str = "file") -> BatchFrame:
    if rows is None:
        rows = [{"incident_id": f"<EXAMPLE_LOT>-{dept}-{i}", "severity_level": "A"}
                for i in range(n)]
    return BatchFrame(
        dept_code=dept,
        period=period,
        batch_ref=f"{dept}-{period}",
        rows=rows,
        columns=["incident_id", "severity_level"],
        source=source,
        pending_columns=pending or [],
        skipped=skipped or {"total": 0, "total_rows": 0, "empty_rows": 0, "bad_columns": 0},
        errors=errors or [],
        file_name=f"{period}.xlsx",
        sheet_name="明细",
    )


REQUIRED_KEYS = {
    "source_mix", "dept_rows", "valid_rows", "skipped_dirty", "pending_columns",
    "suspended_batches", "missing_depts", "mode", "incremental",
}


def test_summary_has_all_contract_keys():
    """摘要含契约规定的全部键。"""
    summary = build_summary([_frame("D01", "2026-01")])
    assert REQUIRED_KEYS <= set(summary)
    assert summary["mode"] == "full"
    assert summary["incremental"] is False


def test_source_mix_dept_rows_and_valid_rows():
    """来源构成 / 各科室行数 / 有效行数按行真实汇总。"""
    frames = [
        _frame("D01", "2026-01", n=3),
        _frame("D01", "2026-02", n=2),
        _frame("D02", "2026-01", n=4, source="platform"),
    ]
    summary = build_summary(frames)
    assert summary["valid_rows"] == 9
    assert summary["dept_rows"] == {"D01": 5, "D02": 4}
    assert summary["source_mix"] == {"file": 5, "platform": 4}
    assert summary["periods"] == ["2026-01", "2026-02"]


def test_skipped_dirty_classified_counts():
    """七类脏数据形态各有分类计数；合计/空行/未映射列来自 skipped 字典。"""
    rows = [
        {"incident_id": "EX-1", "event_type_text": "跌倒", "_encoding_fixed": ["event_type_text"]},
        {"incident_id": "EX-2", "harm_desc": "", "_truncated_cols": ["harm_desc"]},
        {"incident_id": "EX-3", "_suspect_duplicate": True},
        {"incident_id": "EX-4", "event_type_text": "现场𠮷字标记"},
        {"incident_id": "EX-5", "harm_desc": None},
        {"incident_id": "EX-6", "occur_time": "2026-02-10 10:00:00",
         "report_time": "2026-02-01 09:00:00"},
    ]
    frame = _frame(
        "D01", "2026-01", rows=rows,
        skipped={"total": 2, "total_rows": 1, "empty_rows": 1, "bad_columns": 1},
        pending=[{"header": "错列名_X", "count": 1}],
    )
    dirty = build_summary([frame])["skipped_dirty"]
    assert dirty["合计行"] == 1
    assert dirty["全空行"] == 1
    assert dirty["未映射列"] == 1
    assert dirty["乱码还原"] == 1
    assert dirty["超长截断"] == 1
    assert dirty["疑似重复"] == 1
    assert dirty["生僻字"] == 1
    assert dirty["空值"] >= 1
    assert dirty["时间倒挂"] == 1


def test_pending_columns_aggregated_with_location():
    """待确认列汇总并补齐科室/期间/来源文件定位。"""
    frame = _frame("D01", "2026-01", pending=[{"header": "错列名_X", "count": 2}])
    summary = build_summary([frame])
    assert len(summary["pending_columns"]) == 1
    item = summary["pending_columns"][0]
    assert item["header"] == "错列名_X"
    assert item["dept_code"] == "D01"
    assert item["period"] == "2026-01"
    assert item["source_file"] == "2026-01.xlsx"


def test_suspended_batches_from_severe_errors():
    """严重错误（文件损坏 / 必填缺失 / 科室挂起）进挂起批次并带原因。"""
    frame = _frame("D01", "2026-01", errors=[
        {"code": "INGEST_FILE_CORRUPT", "message": "文件损坏，整表挂起"},
        {"code": "INGEST_UNMAPPED_COLUMN", "message": "仅待确认，不挂起"},
    ])
    summary = build_summary([frame])
    assert len(summary["suspended_batches"]) == 1
    item = summary["suspended_batches"][0]
    assert item["code"] == "INGEST_FILE_CORRUPT"
    assert item["batch_ref"] == "D01-2026-01"
    assert "文件损坏" in item["reason"]


def test_missing_depts_lists_absent_active_depts():
    """缺失科室清单 = 主数据 active 科室 - 本批已到科室。"""
    summary = build_summary([_frame("D01", "2026-01"), _frame("D03", "2026-01")])
    assert "D01" not in summary["missing_depts"]
    assert "D03" not in summary["missing_depts"]
    assert "D02" in summary["missing_depts"]
    assert "D10" in summary["missing_depts"]
    assert len(summary["missing_depts"]) == 8


def test_incremental_mode_only_processes_new_keys():
    """增量模式跳过已处理的 (dept, period)，只汇总新增科室/期间。"""
    frames = [
        _frame("D01", "2026-01", n=2),
        _frame("D02", "2026-01", n=2),
        _frame("D03", "2026-01", n=2),
    ]
    full = build_summary(frames, mode="full")
    inc = build_summary(frames, mode="incremental", incremental_keys={("D01", "2026-01")})
    assert full["valid_rows"] == 6
    assert inc["incremental"] is True
    assert inc["mode"] == "incremental"
    assert inc["valid_rows"] == 4
    assert "D01-2026-01" in inc["skipped_keys"]
    assert "D01" not in inc["dept_rows"]
    assert set(inc["processed_keys"]) == {"D02-2026-01", "D03-2026-01"}


def test_incremental_accepts_batch_ref_string_keys():
    """增量键也接受 "dept-period" 字符串形式。"""
    frames = [_frame("D01", "2026-01", n=2), _frame("D02", "2026-01", n=2)]
    inc = build_summary(frames, mode="incremental", incremental_keys={"D01-2026-01"})
    assert inc["valid_rows"] == 2
    assert inc["dept_rows"] == {"D02": 2}


def test_compare_summaries_consistent():
    """全量 vs 增量对比：增量是全量的子集，consistent=True。"""
    frames = [
        _frame("D01", "2026-01", n=2),
        _frame("D02", "2026-01", n=3),
    ]
    full = build_summary(frames, mode="full")
    inc = build_summary(frames, mode="incremental", incremental_keys={("D01", "2026-01")})
    cmp = compare_summaries(full, inc)
    assert cmp["full_valid_rows"] == 5
    assert cmp["incremental_valid_rows"] == 3
    assert cmp["valid_rows_delta"] == -2
    assert cmp["dept_rows_delta"]["D01"] == -2
    assert cmp["consistent"] is True
    assert cmp["modes"] == ["full", "incremental"]


def test_compare_summaries_detects_inconsistent():
    """增量行数超过全量时判定不一致。"""
    full = {"valid_rows": 3, "dept_rows": {"D01": 3}, "mode": "full", "missing_depts": []}
    inc = {"valid_rows": 5, "dept_rows": {"D01": 5}, "mode": "incremental", "missing_depts": []}
    cmp = compare_summaries(full, inc)
    assert cmp["consistent"] is False
    assert cmp["valid_rows_delta"] == 2
