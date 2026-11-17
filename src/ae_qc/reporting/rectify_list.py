"""整改清单 Excel：发给科室安全员的「哪一行改成哪个编码」，不是一张被退回的表。

列 科室 / 行号 / 越界项 / 现值 / 应改为 / 依据（依据取规则条目 basis）。
行数 == 全量越界行数：输入一行一条，不另算、不截断；按科室分 sheet
（``by_dept=True``）便于一次发给多科室。导出前经 reporting.headers 统一检查器，
不含姓名／住院号／诊断明细，也不含 score/rank/penalty/绩效/排名/扣分 类字段
（272 号文硬约束）。人工确认是唯一放行口（见 writeback.guard）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from .headers import EXPORT_HEADERS, assert_no_forbidden, sanitize

# 输入行（行级证据）字段 → 6 列的投影映射；兼容多种来源字段名，不把整行 dump 进产物
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "科室": ("dept_code", "_dept_code", "dept", "科室"),
    "行号": ("row_no", "_row_no", "row", "行号"),
    "越界项": ("column", "rule_id", "越界项", "item"),
    "现值": ("present_value", "value", "observed_value", "现值", "raw_value"),
    "应改为": ("expected_value", "expected", "suggested_code", "应改为"),
    "依据": ("basis", "rule_basis", "依据"),
}


def _pick(row: dict, header: str) -> Any:
    for name in _FIELD_ALIASES[header]:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return ""


def _cell(value: Any) -> str:
    """单元格值统一转字符串并截断，避免把原始整行 dict 带进产物。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)
    return text[:500]


def export_rectify_list(rows: list[dict], path, *, by_dept: bool = False) -> dict:
    """导出整改清单；返回 {path, row_count, sheets, headers, by_dept}。

    注入禁用字段／患者可识别字段到任一输入行时抛 ValueError（272 号文不变量）。
    """
    rows = list(rows)
    # 272 号文 + 脱敏：输入行与最终表头、投影后的行都要过统一检查器
    assert_no_forbidden(rows)
    assert_no_forbidden(EXPORT_HEADERS)

    projected: list[dict[str, str]] = []
    for row in rows:
        clean = sanitize(row) if isinstance(row, dict) else row
        record = {h: _cell(_pick(clean, h)) for h in EXPORT_HEADERS}
        assert_no_forbidden(record)
        projected.append(record)

    wb = Workbook()
    sheets: list[str] = []
    if by_dept:
        # 按科室分 sheet；同科室聚合，保持输入顺序，数据行总数仍 == 全量越界行数
        groups: dict[str, list[dict[str, str]]] = {}
        for rec in projected:
            groups.setdefault(rec["科室"] or "未分科室", []).append(rec)
        first = True
        for dept, recs in groups.items():
            if first:
                ws = wb.active
                ws.title = str(dept)[:31]
                first = False
            else:
                ws = wb.create_sheet(title=str(dept)[:31])
            ws.append(list(EXPORT_HEADERS))
            for rec in recs:
                ws.append([rec[h] for h in EXPORT_HEADERS])
            sheets.append(ws.title)
    else:
        ws = wb.active
        ws.title = "整改清单"
        ws.append(list(EXPORT_HEADERS))
        for rec in projected:
            ws.append([rec[h] for h in EXPORT_HEADERS])
        sheets.append(ws.title)

    out = Path(path)
    if not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out))
    return {"path": str(out), "row_count": len(projected), "sheets": sheets,
            "headers": list(EXPORT_HEADERS), "by_dept": by_dept}


__all__ = ["export_rectify_list"]
