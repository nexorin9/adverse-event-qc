"""行级证据定位与展示层样本截断。

业务口径 ↔ 源机制对照
----------------------
* 源机制 ``unexpected_count`` 是一次期望的越界计数，``partial_unexpected_list``
  是定位到行的失败样本；``success = (unexpected_count == 0)``。本模块 ``locate``
  把计数（全量明细 ``unexpected_detail``，或展示样本 ``unexpected_samples``）展开
  成 ``RowEvidence`` 形状的行级证据清单，使「有多少行越界」能落到「具体哪几行」。
* 源仓库可在一次期望结果里取回具体记录定位；本产品的行级证据必须能回指原始行：
  ``file_name + sheet_name + row_no``（配合 ``row_key`` / ``batch_ref``）。
* 展示只看前 N 条，故 ``truncate_samples`` 只截断展示项并标注 ``total_count`` 与
  ``truncated_count``，不让「只看前 20 条」变成「漏看后 27 条」。
"""
from __future__ import annotations

from typing import Any

from ..models import RowEvidence


def _row_index(rows: list[dict]) -> tuple[dict, dict]:
    """按行号 / row_key 建索引，用于从原始行补齐来源定位与业务列快照。"""
    by_no: dict[Any, dict] = {}
    by_key: dict[str, dict] = {}
    for r in rows or []:
        no = r.get("_row_no")
        if no is not None and no not in by_no:
            by_no[no] = r
        key = r.get("_row_key")
        if key and key not in by_key:
            by_key[key] = r
    return by_no, by_key


def locate(result: dict, rows: list[dict]) -> list[dict]:
    """把结果的越界明细展开为 RowEvidence 形状的行级证据清单。

    优先消费 ``result["unexpected_detail"]``（execute_plan 保留的全量明细），否则
    回退到 ``result["unexpected_samples"]``（展示样本）。每条证据保留原值
    ``present_value`` 与归一后值 ``normalized_value``，并回指
    ``file_name / sheet_name / row_no``；条数在全量明细下等于 ``unexpected_count``。
    """
    detail = result.get("unexpected_detail")
    samples = detail if detail else (result.get("unexpected_samples") or [])
    by_no, by_key = _row_index(rows)
    rule_id = result.get("rule_id", "")
    column = result.get("column", "")

    out: list[dict[str, Any]] = []
    for s in samples:
        no = s.get("row_no")
        key = s.get("row_key")
        row = by_no.get(no) or by_key.get(key) or {}

        # 原始行业务列快照：优先用失败样本自带的 raw（底座 _sample 已保留），
        # 否则从原始行重建；两者都不夹带下划线前缀的元数据。
        raw = s.get("raw")
        if not isinstance(raw, dict) or not raw:
            raw = {k: v for k, v in row.items() if not str(k).startswith("_")}

        ev = RowEvidence(
            row_key=str(key or row.get("_row_key", "") or ""),
            row_no=int(no if no is not None else row.get("_row_no", 0) or 0),
            dept_code=str(s.get("dept_code") or row.get("_dept_code", row.get("dept_code", "")) or ""),
            period=str(s.get("period") or row.get("_period", "") or ""),
            raw_values=dict(raw),
            column=str(s.get("column") or column or ""),
            present_value=s.get("value"),
            expected_value=s.get("expected"),
            normalized_value=s.get("normalized_value", s.get("value")),
            file_name=str(s.get("file_name") or row.get("_file_name", "") or ""),
            sheet_name=str(s.get("sheet_name") or row.get("_sheet_name", "") or ""),
            batch_ref=str(row.get("_batch_ref", "") or s.get("batch_ref", "") or ""),
            rule_id=str(s.get("rule_id") or rule_id or ""),
        )
        out.append(ev.to_dict())
    return out


def truncate_samples(samples: list[dict], limit: int) -> dict[str, Any]:
    """展示层截断：返回 ``{"items", "total_count", "truncated_count"}``。

    只影响展示项，``total_count`` 恒为全量条数，``truncated_count`` 为被截掉条数；
    全量明细的可查条数不受此函数影响。
    """
    items = list(samples or [])
    total = len(items)
    cap = max(0, int(limit))
    shown = items[:cap]
    return {
        "items": shown,
        "total_count": total,
        "truncated_count": max(0, total - len(shown)),
    }


__all__ = ["locate", "truncate_samples"]
