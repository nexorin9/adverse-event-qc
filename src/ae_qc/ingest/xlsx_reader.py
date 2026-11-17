"""入站 A：xlsx 读入器（表头驱动取列 · 生产级）。

业务口径：
- 列名从表头读，经 catalogs/column_mapping.csv 映射到标准列；未映射列进待确认清单，不猜。
- 合计行（首列含「合计/小计/总计」）与全空行跳过并计数，绝不静默丢行。
- GBK/UTF-8 混编：单元格若含乱码字节（latin-1 误解码），按编码嗅探回退解码还原中文。
- 超长单元格截断到配置上限并记录原长度，防止自由文本拖垮读入。
- 单表失败（文件损坏 / 无工作表 / 空表头 / 必填列缺失）返回结构化错误码，不抛栈中断整批。
- 每行附带来源定位元数据（_row_no/_file_name/_sheet_name/_dept_code/_period/_batch_ref）
  与内容指纹（_content_fp），疑似重复行只标记不自动合并（指纹冲突挂起不猜）。

272 号文：读入层不产出排名/扣分/绩效联动，也不落姓名、住院号、诊断明细等患者可识别字段。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from ..config import config_value
from ..errors import describe
from ..logging_util import log_event
from ..models import STANDARD_COLUMNS
from .column_mapping import load_mapping, map_headers

# 合计/小计/总计行的识别词（首列命中即跳过并计数）
_TOTAL_TOKENS = ("合计", "小计", "总计")


def _err(code: str, message: str, **extra: Any) -> dict:
    """结构化错误项：错误码 + 说明 + 处置建议（取自统一错误码表）。"""
    item: dict[str, Any] = {
        "code": code,
        "message": message,
        "action": describe(code).get("action", ""),
    }
    item.update(extra)
    return item


def _has_cjk(text: str) -> bool:
    """是否含中文/中日韩文字或中文标点（含 CJK 扩展区生僻字）。"""
    return any(
        0x2E80 <= ord(c) <= 0x9FFF        # 部首 + 基本汉字
        or 0x3000 <= ord(c) <= 0x303F     # 中文标点
        or 0xFF00 <= ord(c) <= 0xFFEF     # 全角符号
        or 0x20000 <= ord(c) <= 0x3FFFF   # CJK 扩展 B 及以后（生僻字）
        for c in text
    )


def _decode_mojibake(value: Any) -> Any:
    """编码嗅探回退解码：还原「乱码字节被 latin-1 误当字符串」的单元格。

    先尝试 latin-1→utf-8（对应 UTF-8 混编导致的乱码），失败再按契约要求
    latin-1→gbk 还原 GBK 乱码；只有还原结果出现中文才采纳，避免误伤正常
    拉丁字母与已经正常的中文。已是正常中文的文本直接原样返回。
    """
    if not isinstance(value, str) or not value:
        return value
    if _has_cjk(value):
        return value  # 已是正常中文（含量正常生僻字），不做二次转换
    try:
        raw = value.encode("latin-1")
    except UnicodeEncodeError:
        return value  # 含非 latin-1 字符，不是经典 mojibake
    if not any(b >= 0x80 for b in raw):
        return value  # 纯 ASCII，不处理
    for encoding in ("utf-8", "gbk"):
        try:
            fixed = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if fixed != value and _has_cjk(fixed) and "\ufffd" not in fixed:
            return fixed
    return value


def _cell_to_str(value: Any) -> Any:
    """统一单元格取值：日期转 ISO 字符串，其余转 str；None 保持 None（空值口径）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return str(value)


def _all_blank(cells: list[Any]) -> bool:
    """全空行：所有单元格均为 None / 空字符串 / 纯空白。"""
    for c in cells:
        if c is None:
            continue
        if isinstance(c, str) and c.strip() == "":
            continue
        return False
    return True


def _content_fp(values: dict[str, Any]) -> str:
    """业务列内容指纹（不含下划线元数据），用于疑似重复标记。"""
    payload = json.dumps(
        {k: v for k, v in sorted(values.items())},
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _mark_duplicates(rows: list[dict]) -> None:
    """同一表内相同内容标「疑似重复」并互指行号；不自动合并（挂起不猜）。"""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(str(row.get("_content_fp", "")), []).append(row)
    for group in groups.values():
        if len(group) < 2:
            continue
        row_nos = [r["_row_no"] for r in group]
        for row in group:
            row["_suspect_duplicate"] = True
            others = [n for n in row_nos if n != row["_row_no"]]
            row["_duplicate_with"] = others[0] if others else None


def _infer_location(path: Path, config: dict) -> tuple[str, str]:
    """按 inbox/<dept_code>/<period>.xlsx 推断科室与期间；config 可显式覆盖。"""
    dept_code = str(config.get("dept_code") or (path.parent.name if path.parent.name else "")).strip()
    period = str(config.get("period") or path.stem).strip()
    return dept_code, period


def read_xlsx(path: str | Path, config: dict | None = None) -> dict:
    """读取一张上报表，返回结构化结果（不抛栈）。

    config 可覆盖：max_cell_chars / mapping / dept_code / period / sheet。
    skipped 口径：
      - total       被跳过的数据行总数（= total_rows + empty_rows）
      - total_rows  合计/小计/总计行数
      - empty_rows  全空行数
      - bad_columns 表头中未映射（错列名）列数，进待确认清单，不参与取列
    """
    cfg = dict(config or {})
    p = Path(path)
    file_name = p.name
    dept_code, period = _infer_location(p, cfg)
    try:
        max_chars = int(cfg.get("max_cell_chars")
                        or config_value("max_cell_chars", 2000, section="ingest") or 2000)
    except (TypeError, ValueError):
        max_chars = 2000
    mapping = cfg.get("mapping") or load_mapping()

    result: dict[str, Any] = {
        "ok": False,
        "headers": [],
        "rows": [],
        "skipped": {"total": 0, "total_rows": 0, "empty_rows": 0, "bad_columns": 0},
        "pending_columns": [],
        "errors": [],
        "code": None,
        "file_name": file_name,
        "sheet_name": "",
        "truncated_cells": [],
    }

    # 1) 打开工作簿：任何异常都收敛为 INGEST_FILE_CORRUPT，不抛栈
    try:
        import openpyxl
        workbook = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
    except Exception as exc:  # BadZipFile / InvalidFileException / OSError ...
        result["code"] = "INGEST_FILE_CORRUPT"
        result["errors"].append(_err("INGEST_FILE_CORRUPT", f"{file_name} 无法作为工作簿打开：{exc}"))
        log_event("ERROR", code="INGEST_FILE_CORRUPT", message=f"{file_name} 读入失败", batch_ref=f"{dept_code}-{period}")
        return result

    # 2) 取工作表与表头
    try:
        wanted_sheet = cfg.get("sheet")
        ws = None
        if wanted_sheet:
            ws = workbook[wanted_sheet] if wanted_sheet in workbook.sheetnames else None
        elif workbook.worksheets:
            ws = workbook.worksheets[0]
        if ws is None:
            result["code"] = "INGEST_NO_SHEET"
            result["errors"].append(_err("INGEST_NO_SHEET", f"{file_name} 无可用的明细工作表"))
            log_event("ERROR", code="INGEST_NO_SHEET", message=f"{file_name} 工作表缺失", batch_ref=f"{dept_code}-{period}")
            return result
        result["sheet_name"] = ws.title
        raw_rows = list(ws.iter_rows(values_only=True))
    except Exception as exc:
        result["code"] = "INGEST_FILE_CORRUPT"
        result["errors"].append(_err("INGEST_FILE_CORRUPT", f"{file_name} 读取工作表失败：{exc}"))
        return result
    finally:
        try:
            workbook.close()
        except Exception:
            pass

    if not raw_rows:
        result["code"] = "INGEST_EMPTY_HEADER"
        result["errors"].append(_err("INGEST_EMPTY_HEADER", f"{file_name} 表头为空，无法按表头驱动取列"))
        return result

    # 表头也过一遍乱码还原（GBK/UTF-8 混编的表头同样能映射上）
    headers: list[str] = []
    for cell in raw_rows[0]:
        text = _cell_to_str(cell)
        text = _decode_mojibake(text)
        headers.append(text if isinstance(text, str) else ("" if text is None else str(text)))
    result["headers"] = headers
    if not any(h.strip() for h in headers):
        result["code"] = "INGEST_EMPTY_HEADER"
        result["errors"].append(_err("INGEST_EMPTY_HEADER", f"{file_name} 表头全空，无法按表头驱动取列"))
        return result

    # 3) 表头 → 标准列映射
    mm = map_headers(headers, mapping=mapping)
    std_to_idx: dict[str, int] = {}
    for idx, header in enumerate(headers):
        std = mm["mapped"].get(header.strip())
        if std and std not in std_to_idx:
            std_to_idx[std] = idx
    columns = [c for c in STANDARD_COLUMNS if c in std_to_idx]

    pending: list[dict] = []
    for item in mm["pending"]:
        record = dict(item)
        record["source_file"] = file_name
        record["dept_code"] = dept_code
        record["period"] = period
        pending.append(record)
    result["pending_columns"] = pending
    result["skipped"]["bad_columns"] = len(pending)

    errors: list[dict] = []
    if pending:
        errors.append(_err("INGEST_UNMAPPED_COLUMN",
                           f"{file_name} 存在 {len(pending)} 个未映射/挂起列，已进待确认清单",
                           headers=[p["header"] for p in pending]))
    for header, candidates in mm["ambiguous"].items():
        errors.append(_err("INGEST_AMBIGUOUS_HEADER",
                           f"{file_name} 表头「{header}」映射到多列，挂起并列候选，相关规则置 skipped",
                           header=header, candidates=list(candidates)))
    if not mm["usable"]:
        # 必填列缺失：整表判不可用，不产出半截结果
        errors.append(_err("INGEST_REQUIRED_COLUMN_MISSING",
                           f"{file_name} 必填标准列缺失：{', '.join(mm['missing_required'])}",
                           missing_required=list(mm["missing_required"])))
        result["code"] = "INGEST_REQUIRED_COLUMN_MISSING"
        result["errors"] = errors
        log_event("ERROR", code="INGEST_REQUIRED_COLUMN_MISSING",
                  message=f"{file_name} 整表不可用", batch_ref=f"{dept_code}-{period}")
        return result
    result["errors"] = errors

    # 4) 逐行取列、脏行计数、乱码还原、超长截断
    rows: list[dict] = []
    truncated: list[dict] = []
    total_rows = 0
    empty_rows = 0
    sheet_name = result["sheet_name"]
    for row_no, raw in enumerate(raw_rows[1:], start=2):
        cells = list(raw)
        if _all_blank(cells):
            empty_rows += 1
            continue
        first = _cell_to_str(cells[0]) if cells else ""
        if isinstance(first, str) and any(token in first for token in _TOTAL_TOKENS):
            total_rows += 1
            continue

        values: dict[str, Any] = {}
        fixed_cols: list[str] = []
        trunc_cols: list[str] = []
        for std, idx in std_to_idx.items():
            value = _cell_to_str(cells[idx] if idx < len(cells) else None)
            if isinstance(value, str):
                decoded = _decode_mojibake(value)
                if decoded != value:
                    fixed_cols.append(std)
                    value = decoded
                if len(value) > max_chars:
                    truncated.append({
                        "row_no": row_no,
                        "column": std,
                        "header": headers[idx] if idx < len(headers) else "",
                        "original_length": len(value),
                        "kept_length": max_chars,
                    })
                    trunc_cols.append(std)
                    value = value[:max_chars]
            values[std] = value

        record: dict[str, Any] = dict(values)
        record.update({
            "_row_key": f"{dept_code}|{period}|{p.stem}|r{row_no}",
            "_row_no": row_no,
            "_file_name": file_name,
            "_sheet_name": sheet_name,
            "_dept_code": dept_code,
            "_period": period,
            "_batch_ref": f"{dept_code}-{period}",
            "_content_fp": _content_fp(values),
            "_encoding_fixed": fixed_cols,
            "_truncated_cols": trunc_cols,
            "_suspect_duplicate": False,
            "_duplicate_with": None,
        })
        rows.append(record)

    _mark_duplicates(rows)
    result["rows"] = rows
    result["truncated_cells"] = truncated
    result["skipped"]["total_rows"] = total_rows
    result["skipped"]["empty_rows"] = empty_rows
    result["skipped"]["total"] = total_rows + empty_rows
    result["ok"] = True
    return result


@dataclass
class BatchFrame:
    """读入结果统一载体：一批行 + 列结构 + 来源定位 + 诊断信息。

    source: "file" / "platform" / "mixed"；to_dict() 供摘要与切批层消费。
    """

    dept_code: str = ""
    period: str = ""
    batch_ref: str = ""
    rows: list[dict] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    source: str = "file"
    pending_columns: list[dict] = field(default_factory=list)
    skipped: dict = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)
    file_name: str = ""
    sheet_name: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def read_batch(path: str | Path, config: dict | None = None) -> BatchFrame:
    """read_xlsx 的便捷封装：直接产出 BatchFrame，并附上切批/摘要所需的定位信息。

    动态属性（ok/code/headers/truncated_cells/source_path/mtime）不进 to_dict()，
    仅作为同层模块协作的补充信息，不改变契约字段。
    """
    cfg = dict(config or {})
    res = read_xlsx(path, config=cfg)
    p = Path(path)
    dept_code, period = _infer_location(p, cfg)
    columns = [k for k in (res["rows"][0] if res["rows"] else {}) if not k.startswith("_")]

    frame = BatchFrame(
        dept_code=dept_code,
        period=period,
        batch_ref=f"{dept_code}-{period}",
        rows=res["rows"],
        columns=columns,
        source="file",
        pending_columns=res["pending_columns"],
        skipped=res["skipped"],
        errors=res["errors"],
        file_name=res["file_name"],
        sheet_name=res["sheet_name"],
    )
    frame.ok = res["ok"]
    frame.code = res["code"]
    frame.headers = res["headers"]
    frame.truncated_cells = res["truncated_cells"]
    frame.source_path = str(p)
    try:
        frame.mtime = p.stat().st_mtime
    except OSError:
        frame.mtime = 0.0
    return frame


__all__ = ["read_xlsx", "read_batch", "BatchFrame", "_decode_mojibake"]
