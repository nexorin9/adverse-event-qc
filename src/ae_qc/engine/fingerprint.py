"""行内容指纹、幂等行键与疑似重复标记（任务 17）。

业务口径
--------
* 科室自编号不保证跨表唯一，跨表同 incident_id 可能指两件事。行键固定为
  ``dept_code|period|content_fingerprint|row_no``：科室 / 期间是「定位维度」，
  内容指纹是「事件内容维度」，行号消除同内容多行的歧义。
* 内容指纹只取业务列，不取下划线元数据（来源定位、批次、行号），也不把
  ``dept_code/period/batch_ref`` 混进内容哈希——它们已在行键里单列，既避免
  「同内容因科室不同被当成不同内容」，也让「不同科室同内容不误判重复」交给
  ``mark_duplicates`` 的分组键来把关。
* 指纹相同的两行视为「疑似重复」：只标记、给 ``_duplicate_with``，绝不自动
  合并、绝不丢弃——合并是业务判断，不是程序判断。
* 指纹冲突（同一行键对应不同业务内容）标 ``_suspended=True`` 挂起交人工：
  程序不自动合并不丢弃，两行都原样保留。
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

# 不算「事件内容」的定位维度：下划线开头的元数据整类排除；这三个即便以普通列名
# 出现也是行键维度，不进内容哈希。
_LOCATION_KEYS = frozenset({"dept_code", "period", "batch_ref"})


def _is_content_key(key: Any) -> bool:
    text = str(key)
    if text.startswith("_"):
        return False
    return text not in _LOCATION_KEYS


def _norm(value: Any) -> str:
    """规范化：trim + NFKC（全角半角）+ casefold，保证同一内容不同写法指纹一致。"""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return unicodedata.normalize("NFKC", text).strip().casefold()


def content_fingerprint(row: dict) -> str:
    """关键字段规范化后哈希，稳定、与行序无关（按字段名排序，dict 插入顺序无关）。

    同一行内容无论放在列表第几位、字段以什么顺序写入，都得到同一指纹。
    返回 sha256 前 16 位十六进制，长度短且足够区分批次内行内容。
    """
    parts = sorted(
        (str(k), _norm(v))
        for k, v in (row or {}).items()
        if _is_content_key(k)
    )
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def row_key(dept_code: str, period: str, row: dict, row_no: Any = None) -> str:
    """幂等行键 ``dept_code|period|fp|row_no``；同一输入必得同一行键。

    ``row_no`` 缺省时回退到行的 ``_row_no``，再缺省为 0（不猜、不随机）。
    """
    if row_no is None:
        row_no = (row or {}).get("_row_no", 0)
    if row_no is None or row_no == "":
        row_no = 0
    fp = content_fingerprint(row or {})
    return f"{dept_code}|{period}|{fp}|{row_no}"


def _row_dept(row: dict) -> str:
    return str(row.get("_dept_code") or row.get("dept_code") or "")


def _row_period(row: dict) -> str:
    return str(row.get("_period") or row.get("period") or "")


def mark_duplicates(rows: list[dict]) -> list[dict]:
    """标记批内疑似重复并挂起指纹冲突，返回带标记的新行列表（不改动入参）。

    判定顺序：
    0. 先给缺行键的行补上幂等行键（便于落库回查，也保证二次运行结果一致）。
    1. 指纹冲突：多行带**完全相同** ``_row_key`` 但内容指纹不同 → 全部
       ``_suspended=True``，交人工，不参与后续重复合并（不合并、不丢弃）。
    2. 疑似重复：按 ``(科室, 期间, 内容指纹)`` 分组，组内多行互标
       ``_duplicate_note="疑似重复"`` 与 ``_duplicate_with``（对方行号列表）；
       科室 / 期间不同则不判重复。
    """
    out = [dict(r) for r in (rows or [])]
    real_fps = [content_fingerprint(r) for r in out]

    # ⓪ 先补全幂等行键（缺才补，显式已有保留 → 冲突证据不被覆盖）。放在最前，
    #    使「二次运行」（输入已带行键）与「首次运行」的标记结果完全一致。
    for i, r in enumerate(out):
        if not r.get("_row_key"):
            r["_row_key"] = row_key(_row_dept(r), _row_period(r), r, r.get("_row_no"))

    # ① 指纹冲突：同键不同内容，挂起交人工
    by_key: dict[str, list[int]] = {}
    for i, r in enumerate(out):
        key = str(r.get("_row_key") or "")
        if key:
            by_key.setdefault(key, []).append(i)
    suspended: set[int] = set()
    for _key, idxs in by_key.items():
        if len(idxs) > 1 and len({real_fps[i] for i in idxs}) > 1:
            for i in idxs:
                suspended.add(i)
                out[i]["_suspended"] = True
                out[i]["_suspend_reason"] = (
                    "指纹冲突：同一行键对应不同业务内容，挂起交人工，"
                    "程序不自动合并、不自动丢弃"
                )

    # ② 疑似重复：分组键含科室与期间，不同科室 / 不同期间同内容不误判
    groups: dict[tuple[str, str, str], list[int]] = {}
    for i, r in enumerate(out):
        if i in suspended:
            continue
        groups.setdefault((_row_dept(r), _row_period(r), real_fps[i]), []).append(i)

    for _grp, idxs in groups.items():
        if len(idxs) <= 1:
            continue
        nos = [int(out[j].get("_row_no", 0) or 0) for j in idxs]
        keys = [str(out[j].get("_row_key") or "") for j in idxs]
        for pos, j in enumerate(idxs):
            out[j]["_duplicate"] = True
            out[j]["_duplicate_note"] = "疑似重复"
            out[j]["_duplicate_with"] = [n for p, n in enumerate(nos) if p != pos]
            out[j]["_duplicate_with_keys"] = [k for p, k in enumerate(keys) if p != pos]

    return out


__all__ = ["content_fingerprint", "row_key", "mark_duplicates"]
