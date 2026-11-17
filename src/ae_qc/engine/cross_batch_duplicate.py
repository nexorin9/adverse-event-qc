"""跨批次重复上报识别（指纹跨批次索引，任务 18）。

业务口径
--------
* 同一事件在下一期被再报一次是不良事件质控的常见问题（科室换了措辞或重复填报）。
  本模块建立「事件身份指纹」的跨批次索引：只取稳定标识字段（科室 / 事件编号 /
  发生时间 / 事件类别编码），**不含自由文本措辞**（event_type_text / harm_desc /
  root_cause_text / reporter_role 等），所以换措辞的同一事件仍能命中，而措辞相近
  但编号或发生时间不同的两个不同事件不会被误判为重复。
* 命中只生成一条「疑似重复上报」提示：``severity="info"``，
  ``counts_as_violation=False``，不计入错误越界数、不自动写回质控平台、
  不改变批次通过率口径，只进人工确认面板，并给出历史批号 + 行号。
* 无历史库时降级为「无历史可比」的显式说明（不是空列表、不是静默通过）：
  返回一条带 ``no_history=True`` 与 ``notes`` 的提示级记录，让质控员知道本次
  没有可比基础。

索引落在系统临时目录（不进项目发布子目录），可用 ``root`` 显式指定；按批次覆盖
写入，重复 ``index`` 同一批次不增行。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from .rule_kinds import parse_datetime


def _default_root() -> Path:
    return Path(tempfile.gettempdir()) / "ae_qc_cross_batch"


def _safe(name: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(name).strip()) or "default"


def _norm(value: Any) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return unicodedata.normalize("NFKC", text).strip().casefold()


def _time_key(value: Any) -> str:
    """发生时间归一：多候选格式解析成统一 ISO 形态，避免因写法不同漏判。"""
    dt = parse_datetime(value)
    return dt.isoformat(sep=" ") if dt is not None else _norm(value)


def event_fingerprint(row: dict) -> str:
    """事件身份指纹：只取稳定标识，措辞变化不改变指纹。"""
    parts = {
        "dept_code": _norm(row.get("_dept_code") or row.get("dept_code")),
        "incident_id": _norm(row.get("incident_id")),
        "occur_time": _time_key(row.get("occur_time")),
        "event_type_code": _norm(row.get("event_type_code")),
    }
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _dept(row: dict) -> str:
    return str(row.get("_dept_code") or row.get("dept_code") or "")


def _period(row: dict) -> str:
    return str(row.get("_period") or row.get("period") or "")


def _no_history(checked_rows: int) -> list[dict]:
    """可识别的「无历史可比」说明，不静默当作通过。"""
    return [{
        "no_history": True,
        "severity": "info",
        "counts_as_violation": False,
        "counts_as_error": False,
        "message": "无历史可比：尚未建立任何历史批次指纹索引，本次不做跨批重复判定（不视为通过）",
        "notes": [
            "无历史库：未找到任何历史批次的指纹索引",
            "不静默当作通过：本条为提示级说明，不计入越界错误数，需人工留意后续补齐历史库",
            "处置建议：先对历史批次调用 index() 建立索引，再执行 check()",
        ],
        "checked_rows": checked_rows,
        "hits": 0,
    }]


class CrossBatchIndex:
    """按批次存放事件身份指纹的跨批次索引。"""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else _default_root()
        self._path = self.root / "cross_batch_index.json"

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)  # 原子替换，避免并行读半截文件

    def index(self, batch_ref: str, rows: list[dict]) -> int:
        """把一个批次的行纳入索引；同批次重复索引按覆盖处理，不增行不翻倍。"""
        data = self._load()
        records: list[dict[str, Any]] = []
        for r in rows or []:
            records.append({
                "fp": event_fingerprint(r),
                "dept_code": _dept(r),
                "period": _period(r),
                "row_no": int(r.get("_row_no", 0) or 0),
                "row_key": str(r.get("_row_key") or ""),
                "incident_id": str(r.get("incident_id") or ""),
            })
        data[str(batch_ref)] = {"rows": records}
        self._save(data)
        return len(records)

    def batches(self) -> list[str]:
        """已建索引的批次号（断点/历史可查）。"""
        return sorted(str(b) for b in self._load())

    def check(self, batch_ref: str, rows: list[dict]) -> list[dict]:
        """比对历史批次，返回疑似重复上报提示（提示级 info）。

        - 有历史但无命中 → 返回空列表（确实没有重复）；
        - 无历史库（或索引里只有本批次自身）→ 返回带 ``no_history`` 的说明；
        - 命中 → 每条带 ``history`` 明细、``history_batch_ref``、``history_row_no``。
        """
        data = self._load()
        history: dict[str, list[dict]] = {}
        for b, payload in data.items():
            if str(b) == str(batch_ref) or not isinstance(payload, dict):
                continue
            recs = payload.get("rows")
            if isinstance(recs, list):
                history[str(b)] = recs
        if not history:
            return _no_history(len(list(rows or [])))

        lookup: dict[tuple[str, str], list[tuple[str, dict]]] = {}
        for b, recs in history.items():
            for rec in recs:
                if not isinstance(rec, dict):
                    continue
                lookup.setdefault((str(rec.get("fp", "")), str(rec.get("dept_code", ""))), []).append((b, rec))

        findings: list[dict[str, Any]] = []
        for r in rows or []:
            fp = event_fingerprint(r)
            hits = lookup.get((fp, _dept(r)), [])
            if not hits:
                continue
            hist = [{
                "history_batch_ref": b,
                "history_row_no": int(rec.get("row_no", 0) or 0),
                "history_period": str(rec.get("period", "") or ""),
                "history_row_key": str(rec.get("row_key", "") or ""),
            } for b, rec in hits]
            findings.append({
                "severity": "info",
                "counts_as_violation": False,
                "counts_as_error": False,
                "is_error": False,
                "message": "疑似重复上报（提示级）：命中历史批次同一事件，仅进人工确认面板",
                "note": "提示级，不计入错误越界数、不自动写回、不改变批次通过率口径",
                "row_no": int(r.get("_row_no", 0) or 0),
                "row_key": str(r.get("_row_key") or ""),
                "dept_code": _dept(r),
                "period": _period(r),
                "fingerprint": fp,
                "history": hist,
                "history_batch_ref": hist[0]["history_batch_ref"],
                "history_row_no": hist[0]["history_row_no"],
            })
        return findings


__all__ = ["CrossBatchIndex", "event_fingerprint"]
