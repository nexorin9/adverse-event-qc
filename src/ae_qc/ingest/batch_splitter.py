"""入站 C：科室 × 期间两级切批 · batch_ref 确定性派生 · 重复投递取最新。

业务口径：一批 = 一个 dept_code × period；batch_ref 由二者确定性派生，
保证可复现与可复跑。科室名映射到多码或编码不在主数据 → 挂起并列候选，不猜。
同一科室同期反复改表（重复投递）只取最新一版（文件名版本 / 修改时间），
其余记入 superseded 可查，不把多版混进同一批。

272 号文：切批只做实体绑定与分组，不产出排名/扣分，不落患者可识别明细。
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT
from ..errors import describe
from ..logging_util import log_event

DEFAULT_DEPT_MASTER_PATH = PROJECT_ROOT / "catalogs" / "dept_master.csv"
_VERSION_RE = re.compile(r"(?:_v|v|_r|rev|版|\()(\d+)", re.IGNORECASE)


def derive_batch_ref(dept_code: str, period: str) -> str:
    """由 dept_code 与 period 确定性派生批次号（同一输入必得同一输出）。

    格式与读入层元数据 _batch_ref 及 conftest 样例保持一致：D01-2026-01。
    """
    return f"{str(dept_code or '').strip()}-{str(period or '').strip()}"


def load_dept_master(path: str | Path | None = None) -> dict:
    """加载院内科室主数据：{codes, name_to_codes, path}。"""
    p = Path(path) if path else DEFAULT_DEPT_MASTER_PATH
    codes: list[str] = []
    name_to_codes: dict[str, list[str]] = {}
    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        for record in csv.DictReader(fh):
            code = str(record.get("dept_code") or "").strip()
            name = str(record.get("dept_name") or "").strip()
            active = str(record.get("active") or "1").strip().lower() not in ("0", "false", "no", "")
            if not code:
                continue
            if active and code not in codes:
                codes.append(code)
            if name:
                name_to_codes.setdefault(name, [])
                if code not in name_to_codes[name]:
                    name_to_codes[name].append(code)
    return {"codes": codes, "name_to_codes": name_to_codes, "path": str(p)}


def _resolve_master(dept_master: Any) -> dict:
    """把多种注入形式统一成 {codes, name_to_codes}（便于测试替身）。"""
    if dept_master is None:
        return load_dept_master()
    if isinstance(dept_master, dict) and "codes" in dept_master:
        return {
            "codes": [str(c) for c in dept_master.get("codes", [])],
            "name_to_codes": {str(k): [str(x) for x in v]
                              for k, v in dept_master.get("name_to_codes", {}).items()},
            "path": str(dept_master.get("path", "injected")),
        }
    if isinstance(dept_master, (list, tuple, set)):
        codes: list[str] = []
        name_to_codes: dict[str, list[str]] = {}
        for item in dept_master:
            if isinstance(item, dict):
                code = str(item.get("dept_code", "")).strip()
                name = str(item.get("dept_name", "")).strip()
                if code and code not in codes:
                    codes.append(code)
                if name:
                    name_to_codes.setdefault(name, [])
                    if code not in name_to_codes[name]:
                        name_to_codes[name].append(code)
            elif str(item).strip():
                codes.append(str(item).strip())
        return {"codes": codes, "name_to_codes": name_to_codes, "path": "injected"}
    if isinstance(dept_master, dict):
        # {name: [codes]} 形式：科室名 → 多码候选
        codes: list[str] = []
        name_to_codes = {}
        for key, value in dept_master.items():
            if isinstance(value, (list, tuple, set)):
                cols = [str(x) for x in value]
                name_to_codes[str(key)] = cols
                codes.extend(cols)
            else:
                codes.append(str(key))
        return {"codes": list(dict.fromkeys(codes)), "name_to_codes": name_to_codes, "path": "injected"}
    return load_dept_master()


def _as_frame(frame: Any) -> dict:
    """BatchFrame 或 dict 统一成可消费的字典视图。"""
    if isinstance(frame, dict):
        out = dict(frame)
        out.setdefault("ok", True)
        out.setdefault("mtime", 0.0)
        return out
    return {
        "dept_code": getattr(frame, "dept_code", ""),
        "period": getattr(frame, "period", ""),
        "batch_ref": getattr(frame, "batch_ref", ""),
        "rows": list(getattr(frame, "rows", []) or []),
        "columns": list(getattr(frame, "columns", []) or []),
        "source": getattr(frame, "source", "file"),
        "pending_columns": list(getattr(frame, "pending_columns", []) or []),
        "skipped": dict(getattr(frame, "skipped", {}) or {}),
        "errors": list(getattr(frame, "errors", []) or []),
        "file_name": getattr(frame, "file_name", ""),
        "sheet_name": getattr(frame, "sheet_name", ""),
        "ok": bool(getattr(frame, "ok", True)),
        "code": getattr(frame, "code", None),
        "mtime": float(getattr(frame, "mtime", 0.0) or 0.0),
    }


def _resolve_dept(dept: str, codes: set[str], name_to_codes: dict[str, list[str]]) -> dict:
    """科室编码/科室名解析：多码或无法唯一映射一律挂起，不猜。"""
    if dept in codes:
        return {"status": "ok", "dept_code": dept}
    candidates = name_to_codes.get(dept, [])
    if len(candidates) == 1 and candidates[0] in codes:
        return {"status": "ok", "dept_code": candidates[0]}
    if len(candidates) >= 2:
        return {"status": "suspended", "reason": "科室名映射到多码，挂起并列候选不猜",
                "candidates": candidates}
    return {"status": "suspended", "reason": "科室编码不在主数据且无法唯一映射",
            "candidates": []}


def _version_key(frame: dict) -> tuple:
    """重复投递排序键：修改时间 > 文件名版本号 > 文件名（取最大者为最新）。"""
    name = str(frame.get("file_name") or "")
    match = _VERSION_RE.search(name)
    version = int(match.group(1)) if match else 0
    try:
        mtime = float(frame.get("mtime") or 0.0)
    except (TypeError, ValueError):
        mtime = 0.0
    return (mtime, version, name)


def split_batches(frames: list[Any], dept_master: Any = None) -> dict:
    """按 dept_code × period 两级切批。

    返回 {"batches": list[dict], "suspended": list[dict], "superseded": list[dict]}。
    每个 batch 的 rows 统一回填 _batch_ref / _dept_code / _period。
    """
    master = _resolve_master(dept_master)
    codes = {str(c).strip() for c in master.get("codes", [])}
    name_to_codes = {str(k): [str(x) for x in v]
                     for k, v in master.get("name_to_codes", {}).items()}

    groups: dict[tuple[str, str], list[tuple[dict, Any, str]]] = {}
    suspended: list[dict] = []

    for frame in frames or []:
        f = _as_frame(frame)
        dept = str(f.get("dept_code") or "").strip()
        period = str(f.get("period") or "").strip()
        batch_ref = derive_batch_ref(dept, period)

        if not f.get("ok", True):
            suspended.append({
                "batch_ref": batch_ref, "dept_code": dept, "period": period,
                "file_name": f.get("file_name", ""),
                "reason": "源文件不可用（读入失败），整批挂起",
                "code": f.get("code") or "INGEST_FILE_CORRUPT",
                "candidates": [],
            })
            log_event("WARN", code="INGEST_FILE_CORRUPT", message="不可用源文件挂起", batch_ref=batch_ref)
            continue

        resolution = _resolve_dept(dept, codes, name_to_codes)
        if resolution["status"] == "suspended":
            suspended.append({
                "batch_ref": batch_ref, "dept_code": dept, "period": period,
                "file_name": f.get("file_name", ""),
                "reason": resolution["reason"],
                "code": "INGEST_SUSPENDED_DEPT",
                "candidates": list(resolution["candidates"]),
            })
            log_event("WARN", code="INGEST_SUSPENDED_DEPT", message=resolution["reason"], batch_ref=batch_ref)
            continue

        dept_code = resolution["dept_code"]
        groups.setdefault((dept_code, period), []).append((f, frame, dept_code))

    batches: list[dict] = []
    superseded: list[dict] = []
    for (dept_code, period), members in sorted(groups.items()):
        # 重复投递：按版本/修改时间降序，取第一个（最新），其余进被取代记录
        members.sort(key=lambda m: _version_key(m[0]), reverse=True)
        winner = members[0]
        batch_ref = derive_batch_ref(dept_code, period)
        for loser in members[1:]:
            superseded.append({
                "batch_ref": batch_ref,
                "dept_code": dept_code,
                "period": period,
                "file_name": loser[0].get("file_name", ""),
                "superseded_by": winner[0].get("file_name", ""),
                "reason": "重复投递，仅取最新版本；本文件记入被取代列表",
            })
            log_event("INFO", code="INGEST_DUPLICATE_DELIVERY",
                      message=f"{loser[0].get('file_name','')} 被 {winner[0].get('file_name','')} 取代",
                      batch_ref=batch_ref)

        w = winner[0]
        rows: list[dict] = []
        for row in w.get("rows", []):
            record = dict(row)
            record["_batch_ref"] = batch_ref
            record["_dept_code"] = dept_code
            record["_period"] = period
            rows.append(record)
        batches.append({
            "batch_ref": batch_ref,
            "dept_code": dept_code,
            "period": period,
            "rows": rows,
            "row_count": len(rows),
            "source": w.get("source", "file"),
            "file_name": w.get("file_name", ""),
            "sheet_name": w.get("sheet_name", ""),
            "pending_columns": list(w.get("pending_columns", []) or []),
            "skipped": dict(w.get("skipped", {}) or {}),
            "errors": list(w.get("errors", []) or []),
            "superseded_files": [m[0].get("file_name", "") for m in members[1:]],
        })

    # 挂起项的处置建议统一来自错误码表
    for item in suspended:
        item["action"] = describe(item.get("code", "INGEST_SUSPENDED_DEPT")).get("action", "")

    return {"batches": batches, "suspended": suspended, "superseded": superseded}


__all__ = ["derive_batch_ref", "load_dept_master", "split_batches"]
