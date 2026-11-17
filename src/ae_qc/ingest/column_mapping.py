"""表头 → 标准列映射层（入站 B）。

业务口径：列名从科室上报表的表头读，映射表是可编辑的院内资产
（catalogs/column_mapping.csv）。未映射列一律不猜，进「待确认」清单交人工；
同一表头映射到多个标准列时挂起并列候选（不得任选其一），相关规则置 skipped；
必填标准列缺失则该表整体判不可用（usable=False），不产出半截结果。

272 号文口径：本层只处理列结构，不产出排名/扣分，也不落姓名、住院号、诊断明细。
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT
from ..models import REQUIRED_COLUMNS

DEFAULT_MAPPING_PATH = PROJECT_ROOT / "catalogs" / "column_mapping.csv"


def load_mapping(path: str | Path | None = None) -> dict[str, list[str]]:
    """加载列映射表，返回 {表头: [标准列, ...]}。

    csv 允许同一表头出现多行（一对多候选），用 list 保留全部候选；
    utf-8-sig 兼容带 BOM 的院内导出的映射表。
    """
    p = Path(path) if path else DEFAULT_MAPPING_PATH
    mapping: dict[str, list[str]] = {}
    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        for record in csv.DictReader(fh):
            header = str(record.get("header") or "").strip()
            column = str(record.get("standard_column") or "").strip()
            if not header or not column:
                continue
            candidates = mapping.setdefault(header, [])
            if column not in candidates:
                candidates.append(column)
    return mapping


def _normalize_mapping(mapping: dict | None) -> dict[str, list[str]]:
    """把外部注入的映射统一成 {表头: [标准列]} 形状。"""
    out: dict[str, list[str]] = {}
    for header, columns in (mapping or {}).items():
        key = str(header).strip()
        if isinstance(columns, (list, tuple, set)):
            out[key] = [str(c).strip() for c in columns if str(c).strip()]
        else:
            value = str(columns).strip()
            out[key] = [value] if value else []
    return out


def map_headers(headers: list[Any], mapping: dict | None = None) -> dict:
    """把一组表头映射成标准列，返回结构化诊断结果。

    - mapped: 仅「一对一」映射 {表头: 标准列}；挂起候选不进 mapped。
    - pending: 待确认列清单（未映射 + 一表头多列），含表头名、出现次数、原因、并列候选。
    - ambiguous: {表头: [候选标准列]}，需人工指定，相关规则置 skipped。
    - missing_required: 必填标准列中未被任何表头覆盖的列。
    - usable: 必填列齐备且至少映射上一列；缺失则整表不可用。
    """
    mp = load_mapping() if mapping is None else _normalize_mapping(mapping)

    counts: dict[str, int] = {}
    for h in headers or []:
        key = "" if h is None else str(h).strip()
        counts[key] = counts.get(key, 0) + 1

    mapped: dict[str, str] = {}
    ambiguous: dict[str, list[str]] = {}
    pending: list[dict] = []

    for header in counts:  # dict 保序，按表头首次出现顺序处理
        if not header:
            continue  # 空表头不猜测，也不计入待确认列（由读入层判空表头）
        candidates = mp.get(header)
        if not candidates:
            pending.append({
                "header": header,
                "count": counts[header],
                "reason": "未映射列，不猜测（进待确认清单）",
                "candidates": [],
                "ambiguous": False,
            })
        elif len(candidates) == 1:
            mapped[header] = candidates[0]
        else:
            ambiguous[header] = list(candidates)
            pending.append({
                "header": header,
                "count": counts[header],
                "reason": "一表头映射到多列，挂起并列候选，需人工指定；相关规则置 skipped",
                "candidates": list(candidates),
                "ambiguous": True,
            })

    mapped_standards = set(mapped.values())
    # 过滤掉映射表里的非标准列噪声（如辅助列 category_family 允许存在，但不参与必填判定）
    missing_required = [c for c in REQUIRED_COLUMNS if c not in mapped_standards]
    usable = (not missing_required) and bool(mapped)

    return {
        "mapped": mapped,
        "pending": pending,
        "ambiguous": ambiguous,
        "missing_required": missing_required,
        "usable": usable,
    }


__all__ = ["load_mapping", "map_headers", "DEFAULT_MAPPING_PATH"]
