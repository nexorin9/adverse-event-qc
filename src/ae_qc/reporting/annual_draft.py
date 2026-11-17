"""年度汇总表底稿：只出底稿，需院内人工上传国家平台，本产品不自动外发。

依据 272 号文：信息仅用于不良事件管理与医疗质量安全改进，不产出上报量排名／
扣分／绩效联动。底稿字段 期间 / 科室 / 类别 / 等级 / 条数 齐备；每个数字可
回溯到批次与规则版本（「口径来源」sheet 逐行记录 batch_ref / rule_set_version /
聚合来源），统计口径与台账一致，不另起一套统计。

【硬约束】年度底稿不自动外发：本模块只写本地文件，不提供任何自动报送或远程
传输入口，代码中不存在网络客户端与自动外发路径。人工确认是唯一放行口
（见 writeback.guard）；院内人工上传是年度汇总进入国家平台的唯一通道。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import Workbook

from .headers import ANNUAL_HEADERS, assert_no_forbidden

# 底稿内固定标注（spec 任务 42）；这是提示文案，不是任何外发动作
MANUAL_NOTICE = "需院内人工上传国家平台，本产品不自动外发"

_ROW_ALIASES: dict[str, tuple[str, ...]] = {
    "期间": ("period", "期间"),
    "科室": ("dept_code", "dept", "科室"),
    "类别": ("category", "event_category", "category_family", "类别"),
    "等级": ("level", "severity_level", "severity_class", "等级"),
    "条数": ("count", "n", "unexpected_count", "unexpected_rows", "条数"),
}


def _field(row: dict, header: str, default: Any = "") -> Any:
    for name in _ROW_ALIASES[header]:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return default


def _normalize(stats: Any) -> tuple[list[dict], str]:
    """接受行清单，或 {rows, rule_set_version} 形态；返回 (行清单, 默认规则版本)。"""
    if isinstance(stats, dict):
        rows = (stats.get("rows") or stats.get("stats") or stats.get("by_group") or [])
        version = str(stats.get("rule_set_version", "") or "")
    else:
        rows = list(stats)
        version = ""
    return [dict(r) for r in rows], version


def export_annual_draft(stats: Any, path) -> dict:
    """导出年度汇总表底稿；返回 {path, row_count, sheets, headers, notice, ...}。

    注入禁用字段／患者可识别字段到任一统计行时抛 ValueError（272 号文不变量）。
    """
    rows, default_version = _normalize(stats)
    assert_no_forbidden(rows)
    assert_no_forbidden(ANNUAL_HEADERS)

    records: list[dict] = []
    provenance: list[dict] = []
    for idx, row in enumerate(rows, start=1):
        rec = {h: _field(row, h) for h in ANNUAL_HEADERS}
        rec["条数"] = int(rec["条数"] or 0)
        assert_no_forbidden(rec)
        records.append(rec)
        # 每个数字可回溯到批次与规则版本，口径来自台账聚合，不另算
        provenance.append({
            "row_no": idx,
            "batch_ref": str(row.get("batch_ref") or row.get("批次号") or ""),
            "rule_set_version": str(row.get("rule_set_version") or row.get("规则版本")
                                    or default_version),
            "count": rec["条数"],
            "source": str(row.get("source") or "台账聚合结果（LedgerQueries.batch_stats / engine.aggregate）"),
        })

    wb = Workbook()
    ws = wb.active
    ws.title = "年度汇总底稿"
    ws.append(list(ANNUAL_HEADERS))
    for rec in records:
        ws.append([rec[h] for h in ANNUAL_HEADERS])

    # 「口径来源」sheet：逐行记录每个数字对应的批次与规则版本
    src = wb.create_sheet("口径来源")
    src.append(["行号", "批次号", "规则版本", "条数", "聚合来源"])
    for p in provenance:
        src.append([p["row_no"], p["batch_ref"], p["rule_set_version"],
                    p["count"], p["source"]])

    # 「说明」sheet：人工上传提示 + 非自动外发声明（纯文案）
    note = wb.create_sheet("说明")
    note.append(["年度汇总表底稿说明"])
    note.append([MANUAL_NOTICE])
    note.append(["依据 272 号文：信息仅用于不良事件管理与医疗质量安全改进，本底稿不含考核类字段。"])

    out = Path(path)
    if not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out))
    return {"path": str(out), "row_count": len(records), "sheets": wb.sheetnames,
            "headers": list(ANNUAL_HEADERS), "notice": MANUAL_NOTICE,
            "total_count": sum(r["条数"] for r in records),
            "provenance": provenance}


__all__ = ["export_annual_draft", "MANUAL_NOTICE"]
