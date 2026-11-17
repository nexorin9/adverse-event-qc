"""ae-qc 命令行：运维通道与无人值守跑批通道，**不是日常业务入口**。

日常使用走桌面端三栏界面；本 CLI 只服务两类场景：信息科排障（doctor / rule-set
validate）与调度系统无人值守跑批（batch run / findings export / replay）。

退出码语义（统一，供无人值守调度器判定）：
    0  成功
    2  参数错（未知子命令 / 缺子命令 / 参数值非法 / 运行期参数类错误）
    3  配置缺（必需环境变量缺失，启动即报错并逐个点名变量名，不静默降级；
       目录资产缺失、台账不可写同属「环境未就绪」，也归 3）
    4  依赖端点不可达（doctor 探测到模型端点不可达；不阻断，可降级跑批）

命令一律复用已落盘的 ingest / engine / ledger / attribution / writeback / reporting
模块（执行真源是 engine.executor.build_plan + execute_plan），本文件不另写一套引擎
逻辑：只负责参数解析、按序编排、把结果落盘/打印、把分层异常映射成统一退出码。
凭据一律从环境变量读（config.require_env / QCClient 读 env），绝不硬编码。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any

# 让 `python -m ae_qc.cli` 从任意 cwd 都能 import scripts.*（样例生成器）
from . import config as _config
if str(_config.PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_config.PROJECT_ROOT))

from . import doctor  # noqa: E402
from .config import PROJECT_ROOT, require_env  # noqa: E402
from .errors import ConfigError, RuleError  # noqa: E402


def _cell(value: Any) -> str:
    """把任意单元格值转成可写 CSV 的字符串（dict/list 序列化，不夹带整行 dump）。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _load_aliases() -> dict[str, str]:
    """读院内别名表（规则层 alias_resolution 的确定性基线）。"""
    path = PROJECT_ROOT / "catalogs" / "aliases.csv"
    out: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            alias = str(row.get("alias_text") or "").strip()
            code = str(row.get("category_code") or "").strip()
            if alias and code:
                out[alias] = code
    return out


def _store_for(args: argparse.Namespace):
    from .ledger.store import Store
    return Store(args.db) if getattr(args, "db", None) else Store()


# ---- rule-set validate：校验并 diff 套件 ----
def _cmd_ruleset_validate(args: argparse.Namespace) -> int:
    from .engine.ruleset import diff_rulesets, latest_version, list_versions, load_ruleset
    versions = list_versions()
    if not versions:
        raise RuleError("catalogs/rulesets/ 下没有任何套件", code="RULESET_NOT_FOUND")
    target = args.version or latest_version()
    rs = load_ruleset(target)  # 加载期校验条目；版本非法抛 RuleError → 退出码 2
    if target in versions and versions.index(target) > 0:
        base_default = versions[versions.index(target) - 1]
    else:
        base_default = None
    base = args.against or base_default
    diff = diff_rulesets(base, target) if base else {"added": [], "removed": [], "modified": []}
    print(json.dumps({
        "command": "rule-set validate",
        "version": rs.version,
        "entries": len(rs.entries),
        "meta": rs.meta,
        "against": base,
        "diff": diff,
    }, ensure_ascii=False))
    return 0


# ---- batch run：读投递目录 → 跑一批 → 出越界行清单到 out/ ----
def _cmd_batch_run(args: argparse.Namespace) -> int:
    require_env()  # 启动期配置校验：缺项抛 ConfigError → 主函数映射退出码 3
    from scripts.gen_sample_batches import generate_batches

    from .engine.executor import build_plan, execute_plan
    from .engine.row_locator import locate
    from .engine.ruleset import load_ruleset_for_period
    from .ingest.batch_splitter import split_batches
    from .ingest.xlsx_reader import read_batch
    from .ledger.queries import LedgerQueries

    inbox = Path(args.inbox) if args.inbox else PROJECT_ROOT / "inbox"
    out = Path(args.out) if args.out else PROJECT_ROOT / "out"
    out.mkdir(parents=True, exist_ok=True)

    files = sorted(inbox.glob("*/*.xlsx"))
    if not files:
        # 空投递目录时生成一份最小脱敏样例，保证无人值守跑批「有数可跑」
        generate_batches(inbox_dir=inbox, depts=["D01"], periods=["2026-01"],
                         rows_per_sheet=int(args.rows or 12))
        files = sorted(inbox.glob("*/*.xlsx"))
    if not files:
        raise RuntimeError("投递目录无 xlsx，样例生成失败")

    frames = [read_batch(p) for p in files]
    split = split_batches(frames)
    batches = [b for b in split["batches"]
               if (not args.dept or b["dept_code"] == args.dept)
               and (not args.period or b["period"] == args.period)]
    if not batches:
        raise RuntimeError("未切出可用批次（科室主数据或读入异常）")
    batch = sorted(batches, key=lambda b: b["batch_ref"])[0]
    rows = batch["rows"]
    batch_ref = batch["batch_ref"]

    locked = args.version or os.environ.get("AE_RULE_SET", "").strip() or None
    ruleset, version = load_ruleset_for_period(batch["period"], locked=locked)
    columns = sorted({k for r in rows for k in r if not str(k).startswith("_")})
    plan = build_plan(ruleset, columns)
    executed = execute_plan(plan, rows,
                            {"ruleset_version": version, "aliases": _load_aliases()})

    evidence: list[dict[str, Any]] = []
    for res in executed["results"]:
        for ev in locate(res, rows):
            record = dict(ev)
            record["basis"] = res.get("basis", "")
            record["batch_ref"] = batch_ref
            evidence.append(record)

    # 越界行清单（全量，不做展示截断）
    list_path = out / f"越界行清单_{batch_ref}.csv"
    with list_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["批次号", "科室编码", "期间", "行号", "规则", "越界列",
                         "现值", "应改为", "依据"])
        for ev in evidence:
            writer.writerow([
                batch_ref, ev.get("dept_code", ""), ev.get("period", ""),
                ev.get("row_no", ""), ev.get("rule_id", ""), ev.get("column", ""),
                _cell(ev.get("present_value")), _cell(ev.get("expected_value")),
                _cell(ev.get("basis")),
            ])

    # 台账落库：供 findings export / replay 读取同一批结果（数字同源）
    store = _store_for(args)
    try:
        queries = LedgerQueries(store)
        queries.save_batch(batch_ref, dept_code=batch["dept_code"], period=batch["period"],
                           source=batch.get("source", "file"), rule_set_version=version,
                           valid_rows=len(rows), state="validated")
        for res in executed["results"]:
            queries.save_result(batch_ref, res)
        for ev in evidence:
            queries.save_evidence(batch_ref, ev.get("rule_id", ""), ev)
        db_path = store.db_path
    finally:
        store.close()

    print(json.dumps({
        "command": "batch run",
        "batch_ref": batch_ref,
        "rule_set_version": version,
        "checked_rows": len(rows),
        "executed_rules": len(executed["results"]),
        "skipped_rules": len(executed["skipped"]),
        "unexpected_rows": len(evidence),
        "list_path": str(list_path),
        "db": db_path,
    }, ensure_ascii=False))
    return 0


# ---- findings export：从台账导整改清单（复用 reporting.rectify_list）----
def _cmd_findings_export(args: argparse.Namespace) -> int:
    require_env()
    from .ledger.queries import LedgerQueries
    from .reporting.rectify_list import export_rectify_list

    store = _store_for(args)
    try:
        rows = LedgerQueries(store).unexpected_rows(batch_ref=args.batch_ref or None)
    finally:
        store.close()

    out = Path(args.out) if args.out else PROJECT_ROOT / "out"
    out.mkdir(parents=True, exist_ok=True)
    tag = args.batch_ref or "all"
    path = out / f"整改清单_{tag}.xlsx"
    meta = export_rectify_list(rows, path)
    print(json.dumps({
        "command": "findings export",
        "path": meta["path"],
        "row_count": meta["row_count"],
        "sheets": meta["sheets"],
    }, ensure_ascii=False))
    return 0


# ---- replay：用指定（旧）套件版本重放历史批次 ----
def _cmd_replay(args: argparse.Namespace) -> int:
    require_env()
    from scripts.gen_sample_batches import generate_batches

    from .engine.ruleset_version import replay_with_version
    from .ingest.batch_splitter import split_batches
    from .ingest.xlsx_reader import read_batch

    inbox = Path(args.inbox) if args.inbox else PROJECT_ROOT / "inbox"
    files = sorted(inbox.glob("*/*.xlsx"))
    if not files:
        generate_batches(inbox_dir=inbox, depts=["D01"], periods=["2026-01"],
                         rows_per_sheet=int(args.rows or 12))
        files = sorted(inbox.glob("*/*.xlsx"))
    frames = [read_batch(p) for p in files]
    batches = [b for b in split_batches(frames)["batches"]
               if not args.batch_ref or b["batch_ref"] == args.batch_ref]
    if not batches:
        raise RuntimeError("无可重放的历史批次")
    batch = sorted(batches, key=lambda b: b["batch_ref"])[0]
    rows = batch["rows"]
    version = args.version or "v2026-01"
    columns = sorted({k for r in rows for k in r if not str(k).startswith("_")})
    context = {"available_columns": columns, "aliases": _load_aliases(),
               "ruleset_version": version}
    results = replay_with_version(rows, version, context)
    unexpected = sum(len(r.get("unexpected_detail") or r.get("unexpected_samples") or [])
                     for r in results)
    print(json.dumps({
        "command": "replay",
        "batch_ref": batch["batch_ref"],
        "version": version,
        "results": len(results),
        "unexpected_rows": unexpected,
    }, ensure_ascii=False))
    return 0


# ---- doctor：运维体检 ----
def _cmd_doctor(args: argparse.Namespace) -> int:
    report = doctor.run_doctor()
    sys.stdout.write(doctor.render_doctor(report) + "\n")
    return int(report["exit_code"])


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ae-qc",
        description="不良事件上报数据质检系统 · 运维与无人值守跑批通道（非日常入口）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # rule-set validate
    p_rs = sub.add_parser("rule-set", help="期望套件管理")
    rs_sub = p_rs.add_subparsers(dest="action", required=True)
    p_val = rs_sub.add_parser("validate", help="校验套件并输出与对照版本的 diff")
    p_val.add_argument("--version", default=None, help="待校验套件版本（默认最新生效版本）")
    p_val.add_argument("--against", default=None, help="diff 的对照版本（默认前一版本）")
    p_val.set_defaults(func=_cmd_ruleset_validate)

    # batch run
    p_batch = sub.add_parser("batch", help="批次跑批")
    batch_sub = p_batch.add_subparsers(dest="action", required=True)
    p_run = batch_sub.add_parser("run", help="跑一批并输出越界行清单到 out/")
    p_run.add_argument("--inbox", default=None, help="投递目录（空则生成最小样例）")
    p_run.add_argument("--out", default=None, help="清单输出目录（默认 out/）")
    p_run.add_argument("--db", default=None, help="台账 SQLite 路径（默认用户数据目录）")
    p_run.add_argument("--dept", default=None, help="只跑指定科室编码")
    p_run.add_argument("--period", default=None, help="只跑指定期间 yyyy-mm")
    p_run.add_argument("--version", default=None, help="锁定套件版本（默认按期间推断）")
    p_run.add_argument("--rows", type=int, default=12, help="空目录时样例每张表行数")
    p_run.set_defaults(func=_cmd_batch_run)

    # findings export
    p_find = sub.add_parser("findings", help="发现单导出")
    find_sub = p_find.add_subparsers(dest="action", required=True)
    p_exp = find_sub.add_parser("export", help="从台账导出整改清单")
    p_exp.add_argument("--db", default=None, help="台账 SQLite 路径")
    p_exp.add_argument("--out", default=None, help="导出目录（默认 out/）")
    p_exp.add_argument("--batch-ref", default=None, help="只导出指定批次（默认全部）")
    p_exp.set_defaults(func=_cmd_findings_export)

    # replay
    p_replay = sub.add_parser("replay", help="用指定套件版本重放历史批次")
    p_replay.add_argument("--inbox", default=None, help="投递目录（空则生成最小样例）")
    p_replay.add_argument("--batch-ref", default=None, help="指定重放批次（默认首个）")
    p_replay.add_argument("--version", default="v2026-01", help="重放使用的套件版本")
    p_replay.add_argument("--rows", type=int, default=12, help="空目录时样例每张表行数")
    p_replay.set_defaults(func=_cmd_replay)

    # doctor
    p_doc = sub.add_parser("doctor", help="运维体检（配置/资产/台账/模型端点）")
    p_doc.set_defaults(func=_cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口；返回 int 退出码。--help 与参数错误由 argparse 的 SystemExit 承接。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse：--help → 0；未知/缺参 → 2。均映射为统一退出码语义。
        return int(exc.code) if isinstance(exc.code, int) else 0
    try:
        return int(args.func(args))
    except ConfigError as exc:
        # 退出码 3：配置缺（缺环境变量，启动即报错并点名，不静默降级）
        print(f"[配置缺 / 退出码 3] {exc}", file=sys.stderr)
        return 3
    except RuleError as exc:
        # 退出码 2：参数类错误（如指定了不存在的套件版本）
        print(f"[参数错 / 退出码 2] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # 其余运行期错误按参数/用法错误收敛，保证无人值守可判定
        print(f"[运行失败 / 退出码 2] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
