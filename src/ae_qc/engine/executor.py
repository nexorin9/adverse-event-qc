"""批量执行计划：规则 × 可用列、导出复核、单条隔离执行与全量明细保留。

业务口径 ↔ 源机制对照
----------------------
* 源仓库一条期望的判定链路是 ``unexpected_count → success = (unexpected_count == 0)
  → partial_unexpected_list``；本产品由 ``engine.result.build_result`` 落地该口径，
  本模块只负责「按计划把每条规则跑出 ExpectedResult」并额外保留全量明细。
* 源仓库批量执行按「指标 × 列」笛卡尔积生成配置清单并允许逐条剔除，剔除项进
  已中止/审计清单。本模块 ``build_plan`` 改为「规则 × 其适用列」，``cross_product``
  为真时是「规则数 × 可用列数」全笛卡尔积；剔除必写 ``audit``，不静默消失。
* 源仓库对单项计算开启「捕获异常」：抛错的指标被收进已中止清单而不拖垮整批。
  本模块 ``execute_plan`` 逐条 try/except，异常规则记 ``skipped`` 并点名原因，
  其余规则照跑，且 skipped 不进通过率分母。
* 源仓库的 ``partial_unexpected_list`` 是展示样本；本模块在结果里额外保留
  ``unexpected_detail``（全量），使「界面只看前 N 条」不会变成「台账只查得到 N 条」。

只依赖已落盘底座：models / engine.rule_kinds / engine.result，不 import 其它组模块。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from ..models import ExpectedResult, RuleEntry, RuleSet
from . import rule_kinds

# 取 evaluate 的全量失败样本后，再由展示层按 display_limit 截断。evaluate 内部用
# ctx["limit"] 控制 build_result 的截断上限，给一个极大值即可得到全量。
_FULL_LIMIT = 10 ** 9

# 样本行/落盘一律剔除的患者可识别与考核类字段（与 models.FORBIDDEN_TOKENS 口径一致）。
_SENSITIVE_TOKENS = (
    "patient_name", "姓名", "inpatient_no", "住院号", "diagnosis_detail", "诊断明细",
    "score", "rank", "penalty", "绩效", "排名", "扣分", "罚分",
)


def _entries(ruleset: Any) -> list[RuleEntry]:
    if isinstance(ruleset, RuleSet):
        return list(ruleset.entries)
    if isinstance(ruleset, dict):
        return [
            e if isinstance(e, RuleEntry) else RuleEntry.model_validate(dict(e))
            for e in ruleset.get("entries", [])
        ]
    return list(getattr(ruleset, "entries", []))


def _version(ruleset: Any) -> str:
    if isinstance(ruleset, dict):
        return str(ruleset.get("version", ""))
    return str(getattr(ruleset, "version", ""))


def _as_pair(obj: Any) -> tuple[str, str]:
    if isinstance(obj, dict):
        return str(obj.get("rule_id", "")), str(obj.get("column", ""))
    if isinstance(obj, (tuple, list)) and len(obj) == 2:
        return str(obj[0]), str(obj[1])
    return "", ""


def _normalize_exclusions(exclusions: Any) -> dict[str, set]:
    """把多种剔除写法归一成 {tokens, rules, columns, pairs} 集合。

    - 字符串 / 字符串列表：既按规则 id 也按列名匹配（"按规则 id/列名剔除"）；
    - dict：``rules``/``columns`` 分别按 id / 列名，``pairs`` 或 ``{rule_id,column}``
      为「规则+列」精确剔除；
    - (rule_id, column) 二元序列：精确剔除。
    """
    out: dict[str, set] = {"tokens": set(), "rules": set(), "columns": set(), "pairs": set()}
    if not exclusions:
        return out
    if isinstance(exclusions, str):
        out["tokens"].add(exclusions)
        return out
    if isinstance(exclusions, dict):
        for key in ("rules", "rule_ids", "by_rule"):
            for v in exclusions.get(key) or []:
                out["rules"].add(str(v))
        for key in ("columns", "cols", "by_column"):
            for v in exclusions.get(key) or []:
                out["columns"].add(str(v))
        for pair in exclusions.get("pairs") or []:
            out["pairs"].add(_as_pair(pair))
        if "rule_id" in exclusions or "column" in exclusions:
            out["pairs"].add(_as_pair(exclusions))
        return out
    for item in exclusions:
        if isinstance(item, str):
            out["tokens"].add(item)
        elif isinstance(item, dict):
            if "rule_id" in item and "column" in item:
                out["pairs"].add(_as_pair(item))
            elif "rule_id" in item:
                out["rules"].add(str(item["rule_id"]))
            elif "column" in item:
                out["columns"].add(str(item["column"]))
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            out["pairs"].add(_as_pair(item))
    return out


def _exclusion_reason(rule_id: str, column: str, ex: dict[str, set]) -> str:
    if (rule_id, column) in ex["pairs"]:
        return "按规则+列精确剔除"
    if rule_id in ex["rules"]:
        return "按规则 id 剔除"
    if column in ex["columns"]:
        return "按列名剔除"
    if rule_id in ex["tokens"] or column in ex["tokens"]:
        return "按规则 id/列名剔除"
    return ""


def build_plan(
    ruleset: Any,
    available_columns: list[str],
    exclusions: Any = None,
    cross_product: bool = False,
) -> dict[str, Any]:
    """生成执行计划。

    ``cross_product=False``（默认）：每条规则 × 它的适用列（``columns_for`` 与本批
    可用列的交集），缺列不生成并记入 audit。``cross_product=True``：规则数 ×
    可用列数的全笛卡尔积（每条规则都配每个可用列）。剔除按规则 id 或列名生效，
    被剔除项写进 ``audit`` 而非静默消失。

    返回 ``{"items": [...], "audit": [...], "version": str}``；item 含
    ``plan_id/rule_id/column/params/basis/kind/severity``，并额外携带 ``entry``
    （RuleEntry 完整快照），使导出的计划自包含、可离线重建规则并回放。
    """
    available = [str(c) for c in (available_columns or [])]
    avail_set = set(available)
    ex = _normalize_exclusions(exclusions)
    version = _version(ruleset)
    items: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    idx = 0

    for entry in _entries(ruleset):
        declared = [str(c) for c in rule_kinds.columns_for(entry)]
        if cross_product:
            # 全笛卡尔积：规则数 × 可用列数，列维度只取本批真实存在的列。
            candidate_columns = list(available)
        else:
            # 规则 × 其适用列：适用列不在本批可用列时不生成，并逐条记审计。
            candidate_columns = [c for c in declared if c in avail_set]
            for c in declared:
                if c not in avail_set:
                    audit.append({
                        "action": "missing_column",
                        "rule_id": entry.id,
                        "column": c,
                        "message": f"规则 {entry.id} 的适用列 {c} 不在本批可用列，未生成计划项",
                    })

        for col in candidate_columns:
            idx += 1
            plan_id = f"P{idx:04d}:{entry.id}:{col}"
            item = {
                "plan_id": plan_id,
                "rule_id": entry.id,
                "column": col,
                "params": dict(entry.params),
                "basis": entry.basis,
                "kind": entry.kind,
                "severity": entry.severity,
                "entry": entry.model_dump(mode="json"),
            }
            reason = _exclusion_reason(entry.id, col, ex)
            if reason:
                audit.append({
                    "action": "excluded",
                    "plan_id": plan_id,
                    "rule_id": entry.id,
                    "column": col,
                    "reason": reason,
                    "message": f"计划项 {plan_id} 已剔除：{reason}",
                })
                continue
            items.append(item)

    return {"items": items, "audit": audit, "version": version}


def export_plan(plan: dict[str, Any], path: str | Path) -> str:
    """把计划导出成 JSON，供质控员事前复核「这批按哪几条口径查」。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)


def replay_plan(path: str | Path, rows: list[dict], context: dict | None = None) -> dict[str, Any]:
    """由同一份计划文件回放，得到与直接 execute_plan 完全相同的一批结果。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    plan = payload.get("plan", payload) if isinstance(payload, dict) else payload
    return execute_plan(plan, rows, context)


def _rebuild_entry(item: dict[str, Any]) -> RuleEntry:
    """优先用计划里自带的完整 entry 快照重建（含 set_membership 的 value_set）。"""
    raw = item.get("entry")
    if isinstance(raw, dict):
        return RuleEntry.model_validate(dict(raw))
    return RuleEntry(
        id=item["rule_id"],
        kind=item["kind"],
        column=item.get("column") or item["rule_id"],
        value_set=list(item.get("value_set") or []),
        basis=item.get("basis") or "计划回放",
        severity=item.get("severity", "error"),
        params=dict(item.get("params") or {}),
    )


def _display_limit(ctx: dict) -> int:
    v = ctx.get("limit", ctx.get("sample_display_limit", 20))
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 20


def _finalize(res: Any, display_limit: int) -> dict[str, Any]:
    """把 ExpectedResult 转成结果 dict：展示样本截断、计数与全量明细同时保留。

    ``unexpected_samples`` 是展示层样本（截断到 display_limit），``total_count``
    与 ``unexpected_count`` 恒为全量，``truncated_count`` 是被截断条数，
    ``unexpected_detail`` 是供 row_locator / detail_store 消费的全量明细。
    """
    d = res.to_dict() if isinstance(res, ExpectedResult) else dict(res)
    full = list(d.get("unexpected_samples") or [])
    d["unexpected_samples"] = full[:display_limit]
    d["total_count"] = len(full)
    d["unexpected_count"] = len(full)
    d["truncated_count"] = max(0, len(full) - display_limit)
    d["unexpected_detail"] = full
    return d


def _safe_sample_row(rows: list[dict], column: str) -> dict[str, Any]:
    """隔离记录里的「样本行」：只放行定位信息与该列值，不夹带患者可识别明细。"""
    if not rows:
        return {}
    r = rows[0]
    out: dict[str, Any] = {
        "row_no": r.get("_row_no", 0),
        "row_key": r.get("_row_key", ""),
        "dept_code": r.get("_dept_code", r.get("dept_code", "")),
        "period": r.get("_period", ""),
    }
    if column and not any(tok in column for tok in _SENSITIVE_TOKENS):
        out[column] = r.get(column)
    return out


def _summarize(
    items: list[dict], results: list[dict], skipped: list[dict], version: str,
) -> dict[str, Any]:
    pass_count = sum(1 for r in results if r.get("success"))
    # 通过率分母 = 成功执行（executed）的结果条数；skipped 显式排除，不静默计入。
    denominator = len(results)
    reasons: dict[str, int] = {}
    for s in skipped:
        key = s.get("exception_type", "Unknown")
        reasons[key] = reasons.get(key, 0) + 1
    return {
        "version": version,
        "total_items": len(items),
        "executed": len(results),
        "skipped_count": len(skipped),
        "excluded_skipped": len(skipped),
        "pass_count": pass_count,
        "fail_count": len(results) - pass_count,
        "denominator": denominator,
        "pass_rate": round(pass_count / denominator, 6) if denominator else 0.0,
        "total_unexpected": sum(int(r.get("unexpected_count", 0)) for r in results),
        "skip_reasons": reasons,
        "skip_rule_ids": [s.get("rule_id", "") for s in skipped],
    }


def execute_plan(
    plan: dict[str, Any],
    rows: list[dict],
    context: dict | None = None,
    *,
    call_hook: Callable | None = None,
) -> dict[str, Any]:
    """按计划逐条执行，返回 ``{"results", "skipped", "summary"}``。

    执行真源是 ``ae_qc.engine.rule_kinds.evaluate``；``context["_force_fail_rule"]``
    可注入必抛规则用于隔离测试。``call_hook`` 为可选钩子：每条调用一次，返回非
    None 时接管该条结果（ExpectedResult/dict），抛异常则该条被隔离成 skipped，
    返回 None 则仍走 evaluate。

    逐条 try/except 隔离：单条规则异常只让该条进 skipped（带 rule_id / 异常类型 /
    列名 / 样本行 / skip_reason），其余规则照跑；skipped 不进通过率分母。
    """
    ctx = dict(context or {})
    version = str(plan.get("version", "")) if isinstance(plan, dict) else ""
    ctx.setdefault("ruleset_version", version)
    display_limit = _display_limit(ctx)

    eval_ctx = dict(ctx)
    eval_ctx["limit"] = _FULL_LIMIT  # 让 evaluate 产出全量样本，展示截断在此统一做

    # 每行浅拷贝：evaluate 内 alias_resolution 会写 _alias_hit，不污染调用方 rows，
    # 也保证同一 rows 多次执行（直接执行 / 回放）结果确定一致。
    work_rows = [dict(r) for r in (rows or [])]
    items = list(plan.get("items", [])) if isinstance(plan, dict) else []
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for item in items:
        rule_id = item.get("rule_id", "")
        column = item.get("column", "")
        try:
            entry = _rebuild_entry(item)
            if call_hook is not None:
                produced = call_hook(entry, work_rows, dict(ctx))
                res = produced if produced is not None else rule_kinds.evaluate(entry, work_rows, eval_ctx)
            else:
                res = rule_kinds.evaluate(entry, work_rows, eval_ctx)
            results.append(_finalize(res, display_limit))
        except Exception as exc:  # 单条隔离：绝不让一条规则拖垮整批
            skipped.append({
                "plan_id": item.get("plan_id", ""),
                "rule_id": rule_id,
                "column": column,
                "kind": item.get("kind", ""),
                "status": "skipped",
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
                "skip_reason": f"规则执行异常被隔离（{type(exc).__name__}）：{exc}",
                "sample_row": _safe_sample_row(work_rows, column),
            })

    return {
        "results": results,
        "skipped": skipped,
        "summary": _summarize(items, results, skipped, version),
    }


__all__ = [
    "build_plan", "export_plan", "replay_plan", "execute_plan",
]
