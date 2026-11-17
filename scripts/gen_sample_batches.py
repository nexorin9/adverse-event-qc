"""数据获取阶梯 ②：配置化生成 10 科室 × 3 期、≥500 行脱敏上报表。

输出：inbox/<dept_code>/<period>.xlsx（表头为中文映射名，可被 column_mapping.csv 映射）。
刻意注入七类脏数据形态，供后续读入器/规则/摘要的专项断言核对：
  1) GBK/UTF-8 混编（GBK 字节被 latin-1 误解码的乱码单元格 + 正常 UTF-8 中文混排）
  2) 生僻字（CJK 扩展区）
  3) 空值（None / 空字符串）
  4) 合计行（首列「本月合计」）
  5) 全空行
  6) 错列名（额外未映射表头列，进待确认清单）
  7) 疑似重复行（同内容两行，读入层只标记不合并）
  另含：超长单元格、跨期时间倒挂、等级越界（供最窄切片 set_membership 校验）。

脱敏口径：标识一律占位（<EXAMPLE_LOT> / 示例科室 / 示例角色），
严禁拟真批次号、住院号、姓名、诊断明细；272 号文：不产出排名/扣分/绩效联动。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from openpyxl import Workbook

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ---- 配置化参数（可用函数入参或 CLI 覆盖）----
DEFAULT_CONFIG: dict[str, Any] = {
    "depts": [f"D{i:02d}" for i in range(1, 11)],     # 10 个示例科室编码
    "periods": ["2026-01", "2026-02", "2026-03"],     # 3 期
    "rows_per_sheet": 20,                             # 10 × 3 × 20 = 600 行（≥500）
    "sheet_name": "明细",
    "seed": 20260101,
}

# 表头顺序对应标准列：incident_id / dept_code / event_type_text / event_type_code /
# severity_level / severity_class / occur_time / report_time / reporter_role /
# harm_desc / root_cause_text
HEADERS = [
    "事件编号", "科室编码", "事件名称", "事件类别编码", "严重程度等级", "事件类别",
    "发生时间", "上报时间", "上报人角色", "后果描述", "初步原因",
]
WRONG_HEADER = "错列名_未登记备注"  # 未映射表头：进待确认清单，不猜

LEVELS = list("ABCDEFGHI")
CLASS_BY_LEVEL = {
    "A": "Ⅳ", "B": "Ⅲ", "C": "Ⅲ", "D": "Ⅲ",
    "E": "Ⅱ", "F": "Ⅱ", "G": "Ⅱ", "H": "Ⅱ", "I": "Ⅰ",
}
EVENT_NAMES = [
    "跌倒", "给药错误", "输液反应", "标本采集偏差",
    "设备报警未处置", "压疮", "转运交接疏漏", "药品存放偏差",
]
ROLES = ["示例安全员", "示例护士", "示例药师", "示例医师"]
RARE_CHAR = "𠮷"  # CJK 扩展 B 生僻字（非 BMP），用于「生僻字不乱码」断言
GBK_WORD = "跌倒"  # GBK 字节全部 ≥0xA1，latin-1 解码后不含 XML 非法控制字符
OUT_OF_SET_LEVELS = ("Z",)  # 等级越界值，供 set_membership 演示


def _gbk_mojibake(text: str) -> str:
    """构造 GBK 乱码单元格：GBK 字节被 latin-1 误解码后的字符串。"""
    return text.encode("gbk").decode("latin-1")


def _make_row(dept: str, period: str, n: int, di: int, pi: int) -> list[Any]:
    """生成一行脱敏明细，并按行号注入确定性的脏数据形态。"""
    base = datetime.strptime(period + "-01", "%Y-%m-%d")
    occur = base + timedelta(days=(n % 20) + 1, hours=8)
    report = occur + timedelta(hours=2)
    level = LEVELS[(n + di + pi) % len(LEVELS)]

    row: list[Any] = [
        f"<EXAMPLE_LOT>-{dept}-{period}-{n:04d}",          # incident_id（占位，非拟真）
        dept,                                              # dept_code（示例科室编码）
        EVENT_NAMES[(n + di) % len(EVENT_NAMES)],          # event_type_text
        f"AE-CAT-{(n % 8) + 1:02d}",                      # event_type_code
        level,                                             # severity_level
        CLASS_BY_LEVEL[level],                             # severity_class（派生一致）
        occur.strftime("%Y-%m-%d %H:%M:%S"),              # occur_time
        report.strftime("%Y-%m-%d %H:%M:%S"),             # report_time
        ROLES[(n + pi) % len(ROLES)],                      # reporter_role（仅角色）
        "未见影响" if n % 3 else "现场已处置",              # harm_desc（中性，无患者明细）
        "地面湿滑" if n % 2 else "流程提醒",                # root_cause_text
    ]

    # 1) GBK 乱码（与表内其它正常 UTF-8 中文混编）
    if n == 2:
        row[2] = _gbk_mojibake(GBK_WORD)
    # 2) 生僻字
    if n == 3:
        row[2] = f"现场{RARE_CHAR}字标记"
    # 3) 空值（None / 空字符串），注入到非必填列
    if n in (4, 5):
        row[3] = None
        row[9] = ""
        row[10] = None
    # 等级越界（供最窄切片 set_membership 校验；severity_level 本身不注入空值）
    if n in (6, 11, 12):
        row[4] = OUT_OF_SET_LEVELS[0]
    # 7) 跨期时间倒挂：发生时间跨到下一期间且晚于上报时间
    if n == 7:
        row[6] = (base + timedelta(days=45, hours=8)).strftime("%Y-%m-%d %H:%M:%S")
        row[7] = (base + timedelta(days=1, hours=9)).strftime("%Y-%m-%d %H:%M:%S")
    # 超长单元格（超过 config.toml 的 max_cell_chars=2000）
    if n == 10:
        row[9] = "示例后果描述" * 400
    return row


def generate_batches(inbox_dir: str | Path | None = None, *,
                     depts: list[str] | None = None,
                     periods: list[str] | None = None,
                     rows_per_sheet: int | None = None,
                     sheet_name: str | None = None,
                     config: dict | None = None) -> dict:
    """生成全部样例表，返回可核对的统计（文件数 / 行数 / 各批越界数 / 脏数据计数）。"""
    cfg = dict(DEFAULT_CONFIG)
    if config:
        cfg.update(config)
    if depts is not None:
        cfg["depts"] = depts
    if periods is not None:
        cfg["periods"] = periods
    if rows_per_sheet is not None:
        cfg["rows_per_sheet"] = rows_per_sheet
    if sheet_name is not None:
        cfg["sheet_name"] = sheet_name

    root = Path(inbox_dir) if inbox_dir else PROJECT_ROOT / "inbox"
    depts = list(cfg["depts"])
    periods = list(cfg["periods"])
    rps = int(cfg["rows_per_sheet"])

    stats: dict[str, Any] = {
        "files": 0,
        "rows": 0,
        "depts": list(depts),
        "periods": list(periods),
        "rows_per_sheet": rps,
        "inbox_dir": str(root),
        "out_of_set_by_batch": {},
        "dirty": {
            "gbk_mojibake_rows": 0,
            "rare_char_rows": 0,
            "empty_value_rows": 0,
            "total_rows": 0,
            "empty_rows": 0,
            "wrong_header_sheets": 0,
            "suspect_duplicate_rows": 0,
            "time_inversion_rows": 0,
            "truncated_rows": 0,
            "out_of_set_rows": 0,
        },
    }

    for di, dept in enumerate(depts):
        dept_dir = root / dept
        dept_dir.mkdir(parents=True, exist_ok=True)
        for pi, period in enumerate(periods):
            # 6) 错列名：部分表追加一个未映射表头列（进待确认清单）
            extra_header = (di + pi) % 2 == 0
            headers = list(HEADERS) + ([WRONG_HEADER] if extra_header else [])

            workbook = Workbook()
            ws = workbook.active
            ws.title = cfg["sheet_name"]
            ws.append(headers)

            out_of_set = 0
            for n in range(1, rps + 1):
                row = _make_row(dept, period, n, di, pi)
                if n == 9:
                    # 7) 疑似重复行：与第 8 行完全同内容（读入层只标记，不自动合并）
                    row = _make_row(dept, period, 8, di, pi)
                    stats["dirty"]["suspect_duplicate_rows"] += 2  # 第 8、9 行互标
                if row[4] not in LEVELS:
                    out_of_set += 1
                if extra_header:
                    row.append("占位备注")
                ws.append(row)
                stats["rows"] += 1

                if n == 2:
                    stats["dirty"]["gbk_mojibake_rows"] += 1
                if n == 3:
                    stats["dirty"]["rare_char_rows"] += 1
                if n in (4, 5):
                    stats["dirty"]["empty_value_rows"] += 1
                if n == 7:
                    stats["dirty"]["time_inversion_rows"] += 1
                if n == 10:
                    stats["dirty"]["truncated_rows"] += 1

            # 4) 合计行（首列含「合计」）
            ws.append(["本月合计"] + [None] * (len(headers) - 1))
            stats["dirty"]["total_rows"] += 1
            # 5) 全空行
            ws.append([None] * len(headers))
            stats["dirty"]["empty_rows"] += 1

            workbook.save(dept_dir / f"{period}.xlsx")
            stats["files"] += 1
            stats["dirty"]["wrong_header_sheets"] += int(extra_header)
            stats["dirty"]["out_of_set_rows"] += out_of_set
            stats["out_of_set_by_batch"][f"{dept}-{period}"] = out_of_set

    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成脱敏样例上报表（10 科室 × 3 期 ≥500 行）")
    parser.add_argument("--inbox", default=None, help="投递目录，默认项目根 inbox/")
    parser.add_argument("--rows", type=int, default=None, help="每张表的数据行数")
    parser.add_argument("--stats-out", default=None, help="把生成统计写入指定 JSON 文件")
    args = parser.parse_args(argv)

    stats = generate_batches(inbox_dir=args.inbox, rows_per_sheet=args.rows)
    if args.stats_out:
        Path(args.stats_out).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
