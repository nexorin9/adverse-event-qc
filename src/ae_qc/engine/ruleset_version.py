"""套件版本绑定与旧版本复现（任务 19）。

业务口径
--------
* 期望套件是院内沉淀的口径资产，必须版本化：每批结果绑定当时的套件版本与所用
  条目的 basis 快照，使历史结果不依赖当前套件文件（升版后旧批次仍可回看口径）。
* 「这批数是用哪套口径查的」是审计问题，答案是 ``rule_set_version`` +
  ``basis_snapshot``；``replay_with_version`` 用**指定旧版本**重建执行计划并重跑
  同一批行，结果与原始逐条一致（复现性对照）。
* 只依赖已落盘底座：models / engine.ruleset / engine.executor，不 import 其它组模块。
"""
from __future__ import annotations

from typing import Any

from ..models import RuleSet
from .executor import build_plan, execute_plan
from .ruleset import basis_snapshot, load_ruleset


def _version_of(ruleset: Any) -> str:
    if isinstance(ruleset, dict):
        return str(ruleset.get("version", ""))
    return str(getattr(ruleset, "version", ""))


def _snapshot_of(ruleset: Any) -> dict[str, str]:
    """basis 快照：优先复用底座 basis_snapshot（RuleSet），兼容 dict 形态。"""
    if isinstance(ruleset, dict):
        return {
            str(e.get("id", "")): str(e.get("basis", ""))
            for e in ruleset.get("entries", [])
            if isinstance(e, dict)
        }
    return basis_snapshot(ruleset)


def bind_version(result_dict: dict, ruleset: RuleSet | dict) -> dict:
    """写 ``rule_set_version`` 与 ``basis_snapshot`` 到单条结果，返回新 dict。

    - ``rule_set_version``：本次执行所用套件版本（覆盖旧值，历史结果可回看）；
    - ``basis``：若结果缺 basis，用快照里该规则条目的 basis 补上；
    - ``basis_snapshot``：整份套件的 {rule_id: basis} 快照，历史结果不依赖当前
      套件文件，事后可逐条回看「当时依据是什么」。
    """
    out = dict(result_dict)
    version = _version_of(ruleset)
    snapshot = _snapshot_of(ruleset)
    out["rule_set_version"] = version
    if not out.get("basis"):
        out["basis"] = snapshot.get(str(out.get("rule_id", "")), "")
    out["basis_snapshot"] = snapshot
    return out


def _columns_from_rows(rows: list[dict]) -> list[str]:
    """从本批行推导可用列（下划线元数据不算业务列）；缺列不生成计划项。"""
    cols: set[str] = set()
    for r in rows or []:
        cols.update(str(k) for k in r if not str(k).startswith("_"))
    return sorted(cols)


def replay_with_version(rows: list[dict], version: str, context: dict | None = None) -> list[dict]:
    """用指定（旧）版本套件重跑历史批次，返回与原始逐条一致的结果列表。

    执行真源仍是 ``engine.executor.build_plan + execute_plan``，只是套件换成
    ``load_ruleset(version)``，因此结果结构、计数、样本与一次跑完完全相同。
    ``context`` 可含 ``available_columns`` 显式指定列集（与原始执行保持一致），
    以及 ``aliases`` / ``limit`` / ``ruleset_version`` 等常规执行上下文。
    """
    rs = load_ruleset(version)
    ctx = dict(context or {})
    cols = ctx.pop("available_columns", None) or _columns_from_rows(rows)
    ctx.setdefault("ruleset_version", str(rs.version))
    plan = build_plan(rs, list(cols))
    return list(execute_plan(plan, list(rows or []), ctx)["results"])


__all__ = ["bind_version", "replay_with_version"]
