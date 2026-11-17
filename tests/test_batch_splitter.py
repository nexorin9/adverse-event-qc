"""入站 C 切批专项测试：确定性 batch_ref、两级分组、多码挂起、重复投递取最新。"""
from __future__ import annotations

from ae_qc.ingest.batch_splitter import (
    derive_batch_ref, load_dept_master, split_batches,
)
from ae_qc.ingest.xlsx_reader import BatchFrame


def _frame(dept: str, period: str, file_name: str, *, mtime: float = 1.0,
           incident: str = "EX-0001", rows: int = 2, ok: bool = True,
           errors: list | None = None) -> BatchFrame:
    frame = BatchFrame(
        dept_code=dept,
        period=period,
        batch_ref=f"{dept}-{period}",
        rows=[
            {
                "incident_id": f"{incident}-{i}",
                "severity_level": "A",
                "_row_no": i + 1,
                "_row_key": f"{dept}|{period}|{file_name}|r{i + 1}",
                "_file_name": file_name,
                "_source": "file",
            }
            for i in range(rows)
        ],
        columns=["incident_id", "severity_level"],
        source="file",
        file_name=file_name,
        sheet_name="明细",
        errors=errors or [],
    )
    frame.mtime = mtime
    frame.ok = ok
    frame.code = None if ok else "INGEST_FILE_CORRUPT"
    return frame


def test_derive_batch_ref_is_deterministic():
    """同一输入必得同一 batch_ref；不同输入必不同；格式与读入层元数据一致。"""
    a = derive_batch_ref("D01", "2026-01")
    b = derive_batch_ref("D01", "2026-01")
    assert a == b == "D01-2026-01"
    assert derive_batch_ref("D02", "2026-01") != a
    assert derive_batch_ref("D01", "2026-02") != a
    assert derive_batch_ref(" D01 ", " 2026-01 ") == "D01-2026-01"


def test_split_by_dept_and_period_two_levels():
    """一批 = 一个 dept_code × period，三组输入切出三批。"""
    frames = [
        _frame("D01", "2026-01", "2026-01.xlsx"),
        _frame("D01", "2026-02", "2026-02.xlsx"),
        _frame("D02", "2026-01", "2026-01.xlsx"),
    ]
    result = split_batches(frames)
    refs = sorted(b["batch_ref"] for b in result["batches"])
    assert refs == ["D01-2026-01", "D01-2026-02", "D02-2026-01"]
    assert all(b["row_count"] == 2 for b in result["batches"])
    assert result["suspended"] == []
    assert result["superseded"] == []


def test_rows_get_batch_ref_backfilled():
    """切批后每行回填 _batch_ref / _dept_code / _period。"""
    result = split_batches([_frame("D03", "2026-03", "2026-03.xlsx")])
    row = result["batches"][0]["rows"][0]
    assert row["_batch_ref"] == "D03-2026-03"
    assert row["_dept_code"] == "D03"
    assert row["_period"] == "2026-03"


def test_same_incident_id_across_tables_does_not_conflict():
    """跨表同 incident_id 不冲突：各批保留自己的行，行键含科室维度。"""
    frames = [
        _frame("D01", "2026-01", "2026-01.xlsx", incident="SAME"),
        _frame("D02", "2026-01", "2026-01.xlsx", incident="SAME"),
    ]
    result = split_batches(frames)
    assert len(result["batches"]) == 2
    keys = [b["rows"][0]["_row_key"] for b in result["batches"]]
    assert keys[0] != keys[1]
    assert all("SAME" in b["rows"][0]["incident_id"] for b in result["batches"])


def test_dept_name_multi_code_suspended_with_candidates():
    """科室名映射到多码 → 挂起并列候选，不猜、不进批次。"""
    frame = _frame("示例内科", "2026-01", "2026-01.xlsx")
    master = {"codes": ["D01", "D02"], "name_to_codes": {"示例内科": ["D01", "D02"]}}
    result = split_batches([frame], dept_master=master)
    assert result["batches"] == []
    assert len(result["suspended"]) == 1
    suspended = result["suspended"][0]
    assert suspended["code"] == "INGEST_SUSPENDED_DEPT"
    assert suspended["candidates"] == ["D01", "D02"]
    assert "多码" in suspended["reason"]


def test_unknown_dept_code_suspended():
    """dept_code 不在主数据 → 该批挂起并记原因。"""
    result = split_batches([_frame("D99", "2026-01", "2026-01.xlsx")])
    assert result["batches"] == []
    assert len(result["suspended"]) == 1
    assert "主数据" in result["suspended"][0]["reason"]


def test_duplicate_delivery_takes_latest_and_records_superseded():
    """同科室同期多版本只取最新（版本/修改时间），其余进 superseded 可查。"""
    frames = [
        _frame("D01", "2026-01", "2026-01_v1.xlsx", mtime=1.0, incident="OLD1"),
        _frame("D01", "2026-01", "2026-01_v3.xlsx", mtime=3.0, incident="NEW3"),
        _frame("D01", "2026-01", "2026-01_v2.xlsx", mtime=2.0, incident="MID2"),
    ]
    result = split_batches(frames)
    batches = [b for b in result["batches"] if b["batch_ref"] == "D01-2026-01"]
    assert len(batches) == 1
    winner = batches[0]
    assert winner["file_name"] == "2026-01_v3.xlsx"
    assert winner["rows"][0]["incident_id"].startswith("NEW3")
    assert len(result["superseded"]) == 2
    assert all(s["superseded_by"] == "2026-01_v3.xlsx" for s in result["superseded"])
    assert {s["file_name"] for s in result["superseded"]} == {
        "2026-01_v1.xlsx", "2026-01_v2.xlsx",
    }
    assert set(winner["superseded_files"]) == {
        "2026-01_v1.xlsx", "2026-01_v2.xlsx",
    }


def test_superseded_selected_by_filename_version_when_mtime_equal():
    """修改时间相同退化为文件名版本号取最新。"""
    frames = [
        _frame("D01", "2026-01", "2026-01_v1.xlsx", mtime=1.0, incident="OLD"),
        _frame("D01", "2026-01", "2026-01_v4.xlsx", mtime=1.0, incident="NEW"),
    ]
    result = split_batches(frames)
    assert result["batches"][0]["file_name"] == "2026-01_v4.xlsx"
    assert len(result["superseded"]) == 1


def test_unusable_frame_suspended_not_in_batches():
    """读入失败（ok=False）的源文件整批挂起，不混入批次。"""
    result = split_batches([_frame("D01", "2026-01", "broken.xlsx", ok=False)])
    assert result["batches"] == []
    assert len(result["suspended"]) == 1
    assert result["suspended"][0]["code"] == "INGEST_FILE_CORRUPT"


def test_load_dept_master_has_ten_active_codes():
    """默认科室主数据含 10 个示例科室编码。"""
    master = load_dept_master()
    assert len(master["codes"]) == 10
    assert master["codes"][0] == "D01"
    assert master["codes"][-1] == "D10"
