"""规模验收：≥500 行 × 10 科室 × 3 期跑批（正确性 + 性能双阈值）。

深度落点 (c)：正确性是「越界行数与人工抽查清单差异为 0」——人工抽查清单取自
样例生成器注入的越界标记（out_of_set_by_batch），不靠跑批自己给自己判卷；
性能阈值写在 config.toml [scale] max_batch_seconds，脚本可用 --max-seconds 覆盖。

结论落到 docs/规模验收结论.md；本轮未实连的真实端点范围一并写明。
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))            # 允许 import scripts.*
sys.path.insert(0, str(PROJECT_ROOT / "src"))   # 允许 import ae_qc.*

from ae_qc.config import load_config  # noqa: E402
from ae_qc.engine.executor import build_plan, execute_plan  # noqa: E402
from ae_qc.engine.ruleset import load_ruleset  # noqa: E402
from ae_qc.ingest.xlsx_reader import read_batch  # noqa: E402
from scripts.gen_sample_batches import generate_batches  # noqa: E402

DEFAULT_OUTPUT = PROJECT_ROOT / "docs" / "规模验收结论.md"
UNCONNECTED = (
    "院内不良事件报告平台 GET /api/v1/incident-reports（正式对接规范已实现，本轮无凭据未实连）",
    "质控管理平台 POST /api/v1/quality-findings（真实客户端已实现，本轮仅以本地协议替身验收，未实连生产）",
    "OpenAI-compatible 模型端点（本轮评测与归因均走确定性降级路径，未实连真实推理服务）",
)


def threshold_seconds(override: float | None = None) -> float:
    if override is not None:
        return float(override)
    cfg = load_config()
    return float(cfg.get("scale", {}).get("max_batch_seconds", 15))


def load_aliases() -> dict[str, str]:
    path = PROJECT_ROOT / "catalogs" / "aliases.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return {r["alias_text"].strip(): r["category_code"].strip() for r in csv.DictReader(fh)}


def run_scale_check(*, inbox_dir: str | Path | None = None,
                    max_seconds: float | None = None,
                    rows_per_sheet: int = 20,
                    output: str | Path | None = None,
                    keep_output: bool = True) -> dict[str, Any]:
    """生成样例 → 逐批读入 → 执行计划 → 与注入清单比对 → 出结论文档。"""
    own_temp = None
    if inbox_dir is None:
        own_temp = tempfile.TemporaryDirectory(prefix="ae_qc_scale_")
        inbox_dir = Path(own_temp.name) / "inbox"
    inbox = Path(inbox_dir)
    inbox.mkdir(parents=True, exist_ok=True)

    # ① 生成 10 科室 × 3 期 × 20 行 = 600 行（人工抽查清单随生成统计返回）
    stats = generate_batches(inbox_dir=inbox, rows_per_sheet=rows_per_sheet)
    injected: dict[str, int] = stats["out_of_set_by_batch"]

    ruleset = load_ruleset("v2026-01")
    aliases = load_aliases()
    threshold = threshold_seconds(max_seconds)
    batches: list[dict[str, Any]] = []

    files = sorted(inbox.glob("*/*.xlsx"))
    for path in files:
        frame = read_batch(path)
        start = time.perf_counter()
        plan = build_plan(ruleset, frame.columns)
        context = {"aliases": aliases, "ruleset_version": ruleset.version,
                   "limit": 20, "sample_display_limit": 20}
        result = execute_plan(plan, frame.rows, context)
        elapsed = time.perf_counter() - start
        detected = sum(int(r.get("unexpected_count", 0)) for r in result["results"]
                       if r.get("rule_id") == "AE-SEV-LEVEL" and r.get("status") == "executed")
        expected = int(injected.get(frame.batch_ref, 0))
        batches.append({
            "batch_ref": frame.batch_ref, "dept_code": frame.dept_code, "period": frame.period,
            "rows": len(frame.rows), "rule_items": len(plan["items"]),
            "skipped_rules": result["summary"].get("skipped", len(result["skipped"])),
            "elapsed_ms": round(elapsed * 1000, 3), "elapsed_seconds": round(elapsed, 6),
            "injected_out_of_set": expected, "detected_out_of_set": detected,
            "diff": detected - expected,
        })

    times = [b["elapsed_seconds"] for b in batches] or [0.0]
    rule_counts = [b["rule_items"] for b in batches] or [0]
    total_expected = sum(b["injected_out_of_set"] for b in batches)
    total_detected = sum(b["detected_out_of_set"] for b in batches)
    max_elapsed = max(times)
    threshold_passed = max_elapsed <= threshold
    result: dict[str, Any] = {
        "scale": {"depts": len(stats["depts"]), "periods": len(stats["periods"]),
                  "files": stats["files"], "rows": stats["rows"],
                  "rows_per_sheet": stats["rows_per_sheet"]},
        "threshold_seconds": threshold,
        "max_batch_seconds": round(max_elapsed, 6),
        "avg_batch_seconds": round(statistics.fmean(times), 6),
        "min_batch_seconds": round(min(times), 6),
        "p95_batch_seconds": round(sorted(times)[max(0, int(len(times) * 0.95) - 1)], 6),
        "threshold_passed": threshold_passed,
        "correctness": {"total_expected": total_expected, "total_detected": total_detected,
                        "diff": total_detected - total_expected,
                        "per_batch_max_abs_diff": max((abs(b["diff"]) for b in batches), default=0)},
        "rule_count_distribution": {"min": min(rule_counts), "max": max(rule_counts),
                                    "avg": round(statistics.fmean(rule_counts), 2)},
        "batches": batches,
        "unconnected_endpoints": list(UNCONNECTED),
    }
    if keep_output:
        target = Path(output) if output else DEFAULT_OUTPUT
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_markdown(result), encoding="utf-8")
        result["output_path"] = str(target)
    if own_temp is not None:
        own_temp.cleanup()
    return result


def render_markdown(result: dict[str, Any]) -> str:
    s = result["scale"]
    c = result["correctness"]
    lines = [
        "# 规模验收结论（≥500 行 × 10 科室 × 3 期）",
        "",
        "> 深度落点 (c)：真实规模输入与可度量验收。正确性对照的人工抽查清单取自"
        " `scripts/gen_sample_batches.py` 生成时注入的越界标记，不由跑批结果自己判卷。",
        "",
        "## 规模与阈值",
        "",
        "| 项 | 实测值 | 阈值 / 要求 | 结论 |",
        "|---|---|---|---|",
        f"| 科室 × 期间 | {s['depts']} × {s['periods']} = {s['files']} 批 | 10 × 3 | {'通过' if s['files'] >= 30 else '未通过'} |",
        f"| 明细总行数 | {s['rows']} | ≥500 行 | {'通过' if s['rows'] >= 500 else '未通过'} |",
        f"| 越界行数与人工抽查差异 | {c['diff']}（逐批最大绝对差 {c['per_batch_max_abs_diff']}） | 0 | {'通过' if c['diff'] == 0 and c['per_batch_max_abs_diff'] == 0 else '未通过'} |",
        f"| 单批最大耗时 | {result['max_batch_seconds']:.4f} s | ≤ {result['threshold_seconds']} s | {'通过' if result['threshold_passed'] else '未通过'} |",
        "",
        "## 耗时与规则数分布",
        "",
        f"- 单批耗时：最小 {result['min_batch_seconds']:.4f} s / 平均 {result['avg_batch_seconds']:.4f} s / "
        f"P95 {result['p95_batch_seconds']:.4f} s / 最大 {result['max_batch_seconds']:.4f} s",
        f"- 每批执行计划条目数：最小 {result['rule_count_distribution']['min']} / "
        f"平均 {result['rule_count_distribution']['avg']} / 最大 {result['rule_count_distribution']['max']}",
        f"- 期望套件版本：v2026-01（结果与版本绑定，见 `engine/ruleset_version.py`）",
        "",
        "| 批次 | 行数 | 计划条目 | 注入越界 | 检出越界 | 差异 | 耗时(s) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for b in result["batches"]:
        lines.append(f"| {b['batch_ref']} | {b['rows']} | {b['rule_items']} | "
                     f"{b['injected_out_of_set']} | {b['detected_out_of_set']} | {b['diff']} | "
                     f"{b['elapsed_seconds']:.4f} |")
    lines += ["", "## 本轮未实连的真实端点范围", ""]
    for item in result["unconnected_endpoints"]:
        lines.append(f"- {item}")
    lines += [
        "",
        "- 以上端点均已实现正式协议、字段规范与 env 凭据位；本地协议替身只出现在测试层，"
        "不构成最终对接方式（见 progress.txt）。",
        "- 重跑：`python scripts/scale_check.py`；阈值可用 `--max-seconds` 覆盖，"
        "或改 `config.toml [scale] max_batch_seconds`。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="规模验收：≥500 行 × 10 科室 × 3 期双阈值")
    parser.add_argument("--inbox", default=None, help="样例投递目录（默认临时目录，跑完清理）")
    parser.add_argument("--rows", type=int, default=20, help="每张表数据行数（默认 20，共 600 行）")
    parser.add_argument("--max-seconds", type=float, default=None, help="覆盖单批耗时阈值（秒）")
    parser.add_argument("--output", default=None, help="结论文档输出路径")
    args = parser.parse_args(argv)
    result = run_scale_check(inbox_dir=args.inbox, max_seconds=args.max_seconds,
                             rows_per_sheet=args.rows, output=args.output)
    print(f"规模：{result['scale']['depts']} 科室 × {result['scale']['periods']} 期 = "
          f"{result['scale']['files']} 批 / {result['scale']['rows']} 行")
    print(f"正确性：注入 {result['correctness']['total_expected']} 行越界，检出 "
          f"{result['correctness']['total_detected']} 行，差异 {result['correctness']['diff']}")
    print(f"性能：单批最大 {result['max_batch_seconds']:.4f}s / 阈值 {result['threshold_seconds']}s → "
          f"{'通过' if result['threshold_passed'] else '超阈值'}")
    print(f"结论：{result.get('output_path', '(未写文档)')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
