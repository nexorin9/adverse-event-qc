"""最窄端到端切片：生成样例 → 读表 → 切批 → 一条 set_membership → 越界行清单。

这是 task 3 要求跑通的主干（后续任务都是对它的加深）：
  gen_sample_batches → xlsx_reader.read_batch → batch_splitter.split_batches
  → rule_kinds.evaluate(AE-SEV-LEVEL) → out/越界行清单_<批次>_<规则>.csv

清单文件名含批次号与规则标识；行数为全量越界行（不展示截断），
与生成时注入的越界等级行数可人工核对。

272 号文：清单只给「哪一行改成哪个编码」，不排名、不扣分、不联动绩效，
且不含姓名、住院号、诊断明细等患者可识别字段。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from ae_qc.engine.rule_kinds import evaluate
from ae_qc.ingest.batch_splitter import split_batches
from ae_qc.ingest.xlsx_reader import read_batch
from ae_qc.models import RuleEntry
from scripts.gen_sample_batches import PROJECT_ROOT, generate_batches

RULE_ID = "AE-SEV-LEVEL"
LEVEL_SET = list("ABCDEFGHI")


def run(inbox_dir: str | Path | None = None, out_dir: str | Path | None = None) -> dict:
    """跑一次最窄切片，返回可核对的统计字典。"""
    inbox = Path(inbox_dir) if inbox_dir else PROJECT_ROOT / "out" / "demo_inbox"
    out = Path(out_dir) if out_dir else PROJECT_ROOT / "out"
    out.mkdir(parents=True, exist_ok=True)

    # 1) 生成脱敏样例（10 科室 × 3 期 ≥500 行）
    stats = generate_batches(inbox_dir=inbox)

    # 2) 读表 → BatchFrame
    frames = [read_batch(p) for p in sorted(inbox.glob("*/*.xlsx"))]
    if not frames:
        raise RuntimeError("样例目录内没有任何 xlsx，生成或读入失败")

    # 3) 两级切批
    split = split_batches(frames)
    if not split["batches"]:
        raise RuntimeError("未切出任何可用批次（科室主数据或读入异常）")
    batch = sorted(split["batches"], key=lambda b: b["batch_ref"])[0]

    # 4) 执行一条 set_membership 校验（等级必须落在 A–I）
    entry = RuleEntry(
        id=RULE_ID,
        kind="set_membership",
        column="severity_level",
        value_set=LEVEL_SET,
        basis="《医疗质量安全不良事件分级分类标准》附件2（已核原文）· 最窄切片演示",
        severity="error",
        params={"normalize": ["trim", "width", "casefold"],
                "null_policy": "count_as_unexpected"},
    )
    # limit 放大到全量：清单是「全量越界行」，不受界面展示截断影响
    result = evaluate(entry, batch["rows"], {"limit": 10 ** 9, "ruleset_version": "v2026-01"})

    # 5) 输出越界行清单（文件名含批次与规则标识）
    list_path = out / f"越界行清单_{batch['batch_ref']}_{RULE_ID}.csv"
    with list_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "批次号", "科室编码", "期间", "行号", "越界列", "现值",
            "应改为", "依据", "文件名", "工作表",
        ])
        for sample in result.unexpected_samples:
            writer.writerow([
                batch["batch_ref"],
                sample.get("dept_code", batch["dept_code"]),
                batch["period"],
                sample.get("row_no", ""),
                entry.column,
                sample.get("value", ""),
                "A–I 之一（院内分级分类标准）",
                entry.basis,
                sample.get("file_name", ""),
                sample.get("sheet_name", ""),
            ])

    return {
        "generated_files": stats["files"],
        "generated_rows": stats["rows"],
        "frames": len(frames),
        "batches": len(split["batches"]),
        "suspended": len(split["suspended"]),
        "batch_ref": batch["batch_ref"],
        "rule_id": RULE_ID,
        "rows_checked": len(batch["rows"]),
        "unexpected_count": result.unexpected_count,
        "list_path": str(list_path),
        "list_rows": len(result.unexpected_samples),
        "expected_out_of_set": stats["out_of_set_by_batch"].get(batch["batch_ref"], 0),
    }


def main(argv: list[str] | None = None) -> int:
    summary = run()
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
