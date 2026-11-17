"""入站 E：批次摘要与来源标注 · 增量/全量模式 · 摘要对比。

业务口径：每一批给出人能读的摘要——来源构成（文件/平台/混合）、各科室行数、
有效行数、被识别的脏数据形态分类计数、待确认列、挂起批次与原因、缺失科室清单。
增量模式只处理「新增科室或新增期间」（incremental_keys = 已处理过的键集合），
全量模式重跑整批；两者产出的行级结果可用 compare_summaries 对比。

平台不可达回落文件来源时，摘要须标注「本批仅含文件来源」，不得让用户误以为拿到全量。
272 号文：摘要不产出排名/扣分/绩效联动，不落患者可识别明细。
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from ..engine.rule_kinds import parse_datetime
from .batch_splitter import load_dept_master

# 七类脏数据形态的分类键（另含全空行、未映射列等读入层计数）
_DIRTY_KEYS = (
    "合计行", "全空行", "未映射列", "乱码还原", "超长截断",
    "生僻字", "空值", "疑似重复", "时间倒挂",
)


def _frame_view(frame: Any) -> dict:
    """BatchFrame 或 dict 统一视图（摘要所需字段）。"""
    if isinstance(frame, dict):
        return {
            "dept_code": str(frame.get("dept_code", "")),
            "period": str(frame.get("period", "")),
            "rows": list(frame.get("rows", []) or []),
            "source": str(frame.get("source", "file") or "file"),
            "file_name": str(frame.get("file_name", "") or ""),
            "pending_columns": list(frame.get("pending_columns", []) or []),
            "skipped": dict(frame.get("skipped", {}) or {}),
            "errors": list(frame.get("errors", []) or []),
        }
    return {
        "dept_code": str(getattr(frame, "dept_code", "")),
        "period": str(getattr(frame, "period", "")),
        "rows": list(getattr(frame, "rows", []) or []),
        "source": str(getattr(frame, "source", "file") or "file"),
        "file_name": str(getattr(frame, "file_name", "") or ""),
        "pending_columns": list(getattr(frame, "pending_columns", []) or []),
        "skipped": dict(getattr(frame, "skipped", {}) or {}),
        "errors": list(getattr(frame, "errors", []) or []),
    }


def _business_values(row: dict) -> list[Any]:
    """只取业务列值（下划线前缀为元数据，不参与业务规则）。"""
    return [v for k, v in row.items() if not k.startswith("_")]


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _has_rare(text: str) -> bool:
    """生僻字：CJK 扩展 A 区或扩展 B 及以后（非 BMP）。"""
    return any(ord(c) > 0xFFFF or 0x3400 <= ord(c) <= 0x4DBF for c in text)


def _time_inverted(row: dict) -> bool:
    """跨期时间倒挂：发生时间晚于上报时间（含跨期间的情形）。"""
    left = parse_datetime(row.get("occur_time"))
    right = parse_datetime(row.get("report_time"))
    return left is not None and right is not None and left > right


def _expected_depts() -> list[str]:
    try:
        return [str(c) for c in load_dept_master().get("codes", [])]
    except OSError:
        return []


def _key_variants(dept_code: str, period: str) -> set:
    """同时支持 (dept_code, period) 元组与 "dept-period" 字符串两种增量键。"""
    return {(dept_code, period), f"{dept_code}-{period}"}


def build_summary(frames: list[Any], *, mode: str = "full", incremental_keys: Any = None) -> dict:
    """构建批次摘要。

    mode="incremental" 时跳过 incremental_keys 中已处理过的 (dept, period)，
    只汇总新增科室或新增期间；mode="full" 汇总全部 frames。
    """
    is_incremental = (mode == "incremental")
    already_done: set = set(incremental_keys or []) if incremental_keys else set()

    selected: list[dict] = []
    processed: list[str] = []
    skipped_keys: list[str] = []
    for frame in frames or []:
        f = _frame_view(frame)
        keys = _key_variants(f["dept_code"], f["period"])
        if is_incremental and keys & already_done:
            skipped_keys.append(f"{f['dept_code']}-{f['period']}")
            continue
        processed.append(f"{f['dept_code']}-{f['period']}")
        selected.append(f)

    source_mix: Counter = Counter()
    dept_rows: Counter = Counter()
    skipped_dirty: Counter = Counter({k: 0 for k in _DIRTY_KEYS})
    pending_columns: list[dict] = []
    suspended_batches: list[dict] = []
    valid_rows = 0
    seen_depts: set[str] = set()
    seen_periods: set[str] = set()

    for f in selected:
        rows = f["rows"]
        valid_rows += len(rows)
        seen_depts.add(f["dept_code"])
        seen_periods.add(f["period"])
        source_mix[f["source"]] += len(rows)
        dept_rows[f["dept_code"]] += len(rows)

        sk = f["skipped"]
        skipped_dirty["合计行"] += int(sk.get("total_rows", 0) or 0)
        skipped_dirty["全空行"] += int(sk.get("empty_rows", 0) or 0)
        skipped_dirty["未映射列"] += int(sk.get("bad_columns", len(f["pending_columns"])) or 0)

        for item in f["pending_columns"]:
            record = dict(item)
            record.setdefault("dept_code", f["dept_code"])
            record.setdefault("period", f["period"])
            record.setdefault("source_file", f["file_name"])
            pending_columns.append(record)

        for error in f["errors"]:
            code = str(error.get("code", ""))
            if code in ("INGEST_FILE_CORRUPT", "INGEST_NO_SHEET", "INGEST_EMPTY_HEADER",
                        "INGEST_REQUIRED_COLUMN_MISSING", "INGEST_SUSPENDED_DEPT"):
                suspended_batches.append({
                    "batch_ref": f"{f['dept_code']}-{f['period']}",
                    "dept_code": f["dept_code"],
                    "period": f["period"],
                    "file_name": f["file_name"],
                    "code": code,
                    "reason": str(error.get("message", "整表不可用")),
                })

        for row in rows:
            if row.get("_encoding_fixed"):
                skipped_dirty["乱码还原"] += 1
            if row.get("_truncated_cols"):
                skipped_dirty["超长截断"] += 1
            if row.get("_suspect_duplicate"):
                skipped_dirty["疑似重复"] += 1
            values = _business_values(row)
            if any(isinstance(v, str) and _has_rare(v) for v in values):
                skipped_dirty["生僻字"] += 1
            if any(_is_empty(v) for v in values):
                skipped_dirty["空值"] += 1
            if _time_inverted(row):
                skipped_dirty["时间倒挂"] += 1

    missing_depts = sorted(set(_expected_depts()) - seen_depts)

    return {
        "source_mix": dict(source_mix),
        "dept_rows": dict(dept_rows),
        "valid_rows": valid_rows,
        "skipped_dirty": dict(skipped_dirty),
        "pending_columns": pending_columns,
        "suspended_batches": suspended_batches,
        "missing_depts": missing_depts,
        "mode": mode,
        "incremental": is_incremental,
        "processed_keys": processed,
        "skipped_keys": skipped_keys,
        "periods": sorted(seen_periods),
    }


def compare_summaries(full: dict, incremental: dict) -> dict:
    """对比全量与增量摘要：行数差、各科室差、新增科室与一致性判定。

    consistent=True 表示增量摘要是全量摘要的子集（每科室行数与总行数都不超过全量），
    用于「增量与全量产出的行级结果可对比」的验收。
    """
    f_rows = int(full.get("valid_rows", 0) or 0)
    i_rows = int(incremental.get("valid_rows", 0) or 0)
    f_dept = {str(k): int(v) for k, v in (full.get("dept_rows", {}) or {}).items()}
    i_dept = {str(k): int(v) for k, v in (incremental.get("dept_rows", {}) or {}).items()}
    all_depts = sorted(set(f_dept) | set(i_dept))
    dept_rows_delta = {d: i_dept.get(d, 0) - f_dept.get(d, 0) for d in all_depts}
    consistent = (i_rows <= f_rows
                  and all(i_dept.get(d, 0) <= f_dept.get(d, 0) for d in i_dept))
    return {
        "full_valid_rows": f_rows,
        "incremental_valid_rows": i_rows,
        "valid_rows_delta": i_rows - f_rows,
        "dept_rows_delta": dept_rows_delta,
        "new_depts": sorted(set(i_dept) - set(f_dept)),
        "missing_depts_full": list(full.get("missing_depts", []) or []),
        "missing_depts_incremental": list(incremental.get("missing_depts", []) or []),
        "modes": [full.get("mode"), incremental.get("mode")],
        "consistent": consistent,
    }


__all__ = ["build_summary", "compare_summaries"]
