"""入站 A 读入器专项测试：表头驱动、脏行计数、编码回退、超长截断、损坏文件错误码。

全部断言真实业务行为；xlsx 由 openpyxl 写入 tmp_path，绝不连真实端点。
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from ae_qc.ingest.xlsx_reader import (
    BatchFrame, _decode_mojibake, read_batch, read_xlsx,
)

HEADERS = [
    "事件编号", "科室编码", "事件名称", "事件类别编码", "严重程度等级", "事件类别",
    "发生时间", "上报时间", "上报人角色", "后果描述", "初步原因",
]


def _write(path: Path, headers: list, rows: list, sheet_title: str = "明细") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title
    ws.append(headers)
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


def _row(n: int = 1, **overrides) -> list:
    base = [
        f"<EXAMPLE_LOT>-D01-2026-01-{n:04d}", "D01", "跌倒", "AE-CAT-08",
        "A", "Ⅳ", "2026-01-02 10:00:00", "2026-01-02 11:00:00",
        "示例安全员", "未见影响", "地面湿滑",
    ]
    for key, value in overrides.items():
        idx = HEADERS.index(key)
        base[idx] = value
    return base


def _table(tmp_path: Path, rows: list, headers: list | None = None) -> Path:
    return _write(tmp_path / "D01" / "2026-01.xlsx", headers or HEADERS, rows)


def test_header_driven_columns_and_row_metadata(tmp_path):
    """表头驱动取列：中文表头映射成标准列，每行带来源定位元数据。"""
    path = _table(tmp_path, [_row(1), _row(2)])
    res = read_xlsx(path)
    assert res["ok"] is True
    assert res["code"] is None
    assert res["headers"] == HEADERS
    assert len(res["rows"]) == 2
    first = res["rows"][0]
    assert first["incident_id"] == "<EXAMPLE_LOT>-D01-2026-01-0001"
    assert first["dept_code"] == "D01"
    assert first["severity_level"] == "A"
    assert first["_row_no"] == 2  # 表头占第 1 行
    assert first["_dept_code"] == "D01"
    assert first["_period"] == "2026-01"
    assert first["_batch_ref"] == "D01-2026-01"
    assert first["_file_name"] == "2026-01.xlsx"
    assert first["_sheet_name"] == "明细"
    assert first["_row_key"] == "D01|2026-01|2026-01|r2"


def test_total_and_empty_rows_counted_not_silently_dropped(tmp_path):
    """合计行与全空行跳过并分别计数，不静默丢行。"""
    path = _table(tmp_path, [
        _row(1),
        ["本月合计", None, None, None, None, None, None, None, None, None, None],
        [None] * len(HEADERS),
        _row(2),
        ["小计", None, None, None, None, None, None, None, None, None, None],
    ])
    res = read_xlsx(path)
    assert res["ok"] is True
    assert len(res["rows"]) == 2  # 只保留两条正常明细
    assert res["skipped"]["total_rows"] == 2  # 合计 + 小计
    assert res["skipped"]["empty_rows"] == 1
    assert res["skipped"]["total"] == 3
    assert res["skipped"]["bad_columns"] == 0


def test_gbk_mojibake_cell_restored(tmp_path):
    """GBK 字节被 latin-1 误解码的单元格按 latin-1→gbk 回退还原中文。"""
    gbk = "跌倒".encode("gbk").decode("latin-1")
    path = _table(tmp_path, [_row(1, 事件名称=gbk)])
    res = read_xlsx(path)
    row = res["rows"][0]
    assert row["event_type_text"] == "跌倒"
    assert "event_type_text" in row["_encoding_fixed"]


def test_utf8_mojibake_restored_and_normal_text_untouched():
    """UTF-8 混编乱码可还原；正常中文与纯 ASCII 不被误转换。"""
    u8 = "中文".encode("utf-8").decode("latin-1")
    assert _decode_mojibake(u8) == "中文"
    assert _decode_mojibake("跌倒") == "跌倒"
    assert _decode_mojibake("D01-2026-01") == "D01-2026-01"


def test_rare_char_not_garbled(tmp_path):
    """CJK 扩展区生僻字（非 BMP）读入后逐字符一致，不乱码不丢字。"""
    rare = "现场𠮷字标记"
    path = _table(tmp_path, [_row(1, 事件名称=rare)])
    res = read_xlsx(path)
    assert res["rows"][0]["event_type_text"] == rare
    assert "𠮷" in res["rows"][0]["event_type_text"]
    assert res["rows"][0]["_encoding_fixed"] == []


def test_long_cell_truncated_and_recorded(tmp_path):
    """超长单元格截断到配置上限并记录原长度，不静默吞掉。"""
    long_text = "示例后果描述" * 20  # 120 字符
    path = _table(tmp_path, [_row(1, 后果描述=long_text)])
    res = read_xlsx(path, config={"max_cell_chars": 40})
    assert len(res["rows"][0]["harm_desc"]) == 40
    assert len(res["truncated_cells"]) == 1
    record = res["truncated_cells"][0]
    assert record["column"] == "harm_desc"
    assert record["original_length"] == len(long_text)
    assert record["kept_length"] == 40
    assert "harm_desc" in res["rows"][0]["_truncated_cols"]


def test_corrupt_file_returns_structured_error_code(tmp_path):
    """损坏文件返回 INGEST_FILE_CORRUPT 而不抛栈中断整批。"""
    path = tmp_path / "D01" / "broken.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is definitely not a zip/xlsx workbook")
    res = read_xlsx(path)
    assert res["ok"] is False
    assert res["code"] == "INGEST_FILE_CORRUPT"
    assert res["rows"] == []
    assert res["errors"][0]["code"] == "INGEST_FILE_CORRUPT"
    assert res["errors"][0]["action"]  # 处置建议非空


def test_missing_sheet_returns_structured_error_code(tmp_path):
    """指定工作表缺失 → INGEST_NO_SHEET。"""
    path = _table(tmp_path, [_row(1)])
    res = read_xlsx(path, config={"sheet": "不存在的工作表"})
    assert res["ok"] is False
    assert res["code"] == "INGEST_NO_SHEET"


def test_empty_header_returns_structured_error_code(tmp_path):
    """表头全空 → INGEST_EMPTY_HEADER，不按猜测取列。"""
    path = _write(tmp_path / "D01" / "2026-01.xlsx", [None] * len(HEADERS), [_row(1)])
    res = read_xlsx(path)
    assert res["ok"] is False
    assert res["code"] == "INGEST_EMPTY_HEADER"


def test_unmapped_column_goes_to_pending(tmp_path):
    """错列名（未映射表头）进待确认清单，含来源文件与出现次数，且不参与取列。"""
    headers = HEADERS + ["错列名_未登记备注"]
    path = _table(tmp_path, [_row(1) + ["占位备注"]], headers=headers)
    res = read_xlsx(path)
    assert res["ok"] is True
    assert res["skipped"]["bad_columns"] == 1
    assert len(res["pending_columns"]) == 1
    pending = res["pending_columns"][0]
    assert pending["header"] == "错列名_未登记备注"
    assert pending["count"] == 1
    assert pending["source_file"] == "2026-01.xlsx"
    assert pending["ambiguous"] is False
    assert "错列名_未登记备注" not in res["rows"][0]
    assert any(e["code"] == "INGEST_UNMAPPED_COLUMN" for e in res["errors"])


def test_ambiguous_header_suspended_with_candidates(tmp_path):
    """一表头映射多列（事件时间）挂起并列候选，相关规则置 skipped 提示。"""
    headers = HEADERS + ["事件时间"]
    path = _table(tmp_path, [_row(1) + ["2026-01-02 10:00:00"]], headers=headers)
    res = read_xlsx(path)
    pending = [p for p in res["pending_columns"] if p["header"] == "事件时间"]
    assert len(pending) == 1
    assert pending[0]["ambiguous"] is True
    assert set(pending[0]["candidates"]) == {"occur_time", "report_time"}
    ambiguous_errors = [e for e in res["errors"] if e["code"] == "INGEST_AMBIGUOUS_HEADER"]
    assert len(ambiguous_errors) == 1
    assert set(ambiguous_errors[0]["candidates"]) == {"occur_time", "report_time"}


def test_missing_required_column_makes_table_unusable(tmp_path):
    """必填列缺失 → 整表判不可用，不产出半截结果。"""
    path = _table(tmp_path, [["<EXAMPLE_LOT>-1", "D01"]], headers=["事件编号", "科室编码"])
    res = read_xlsx(path)
    assert res["ok"] is False
    assert res["code"] == "INGEST_REQUIRED_COLUMN_MISSING"
    assert res["rows"] == []
    err = [e for e in res["errors"] if e["code"] == "INGEST_REQUIRED_COLUMN_MISSING"][0]
    assert "occur_time" in err["missing_required"]
    assert "report_time" in err["missing_required"]


def test_suspect_duplicate_marked_but_not_merged(tmp_path):
    """疑似重复行只标记并互指，不自动合并（指纹冲突挂起不猜）。"""
    path = _table(tmp_path, [_row(1), _row(1)])
    res = read_xlsx(path)
    assert len(res["rows"]) == 2
    marked = [r for r in res["rows"] if r["_suspect_duplicate"]]
    assert len(marked) == 2
    assert marked[0]["_duplicate_with"] == marked[1]["_row_no"]
    assert marked[1]["_duplicate_with"] == marked[0]["_row_no"]


def test_batch_frame_to_dict_and_read_batch(tmp_path):
    """BatchFrame 含契约字段，to_dict 可序列化；read_batch 附带定位信息。"""
    path = _table(tmp_path, [_row(1)])
    frame = read_batch(path)
    assert isinstance(frame, BatchFrame)
    payload = frame.to_dict()
    for key in ("dept_code", "period", "batch_ref", "rows", "columns", "source",
                "pending_columns", "skipped", "errors", "file_name", "sheet_name"):
        assert key in payload
    assert payload["batch_ref"] == "D01-2026-01"
    assert payload["source"] == "file"
    assert "incident_id" in payload["columns"]
    assert frame.ok is True
    assert frame.mtime > 0
    assert frame.source_path == str(path)
