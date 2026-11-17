"""批次通过率与规则族 / 科室维度聚合（任务 20）。

业务口径
--------
* 主窗口左栏的通过率细条、整改清单摘要、批次摘要**必须来自同一份聚合结果**，
  否则「界面显示 92%、导出写 47 条」这类对不上的事会误导质控员。本模块提供
  ``aggregate`` 作为唯一取数源，``numbers_for_display`` 及三处消费入口统一取数。
* 分母固定排除 ``skipped``：一条因数据形态崩掉的规则不该把通过率压低。聚合结果
  标出 ``denominator``（分母基数）与 ``excluded_skipped``（被排除项数 / 规则 id）。
* 越界行数优先复用 ``execute_plan`` 保留的全量 ``unexpected_detail``：展示层的
  截断（``unexpected_samples`` 只看前 N 条）不影响聚合口径，避免「清单只显示
  20 条、数字却写着 47 条」。
* 提示级发现（severity=info / info_findings）不混入错误越界计数。
"""
from __future__ import annotations

from typing import Any


def _details(result: dict) -> list[dict]:
    """取全量失败明细；没有 unexpected_detail 时回退到展示样本。"""
    detail = result.get("unexpected_detail")
    if isinstance(detail, list):
        return detail
    samples = result.get("unexpected_samples")
    return list(samples) if isinstance(samples, list) else []


def _row_index(rows: list[dict]) -> tuple[dict, dict]:
    """按行号 / row_key 建索引，用于给失败样本补齐科室维度。"""
    by_no: dict[Any, dict] = {}
    by_key: dict[str, dict] = {}
    for r in rows or []:
        no = r.get("_row_no")
        if no is not None and no not in by_no:
            by_no[no] = r
        key = r.get("_row_key")
        if key and key not in by_key:
            by_key[key] = r
    return by_no, by_key


def _sample_dept(sample: dict, by_no: dict, by_key: dict) -> str:
    dept = sample.get("dept_code")
    if dept:
        return str(dept)
    row = by_no.get(sample.get("row_no")) or by_key.get(sample.get("row_key")) or {}
    return str(row.get("_dept_code") or row.get("dept_code") or "")


def aggregate(results: list[dict], rows: list[dict]) -> dict:
    """聚合一批期望结果。

    返回 ``pass_rate / denominator / excluded_skipped / by_kind / by_dept /
    total_unexpected_rows`` 及辅助计数：
    - ``by_kind[kind]``：该规则族的规则数、通过 / 失败数、越界行数；
    - ``by_dept[dept]``：该科室的越界行数与涉及规则 id（行级口径，累加）；
    - ``total_unexpected_rows``：全量越界行数（各规则明细条数之和）。
    """
    results = list(results or [])
    executed = [r for r in results if str(r.get("status", "executed")) != "skipped"]
    skipped = [r for r in results if str(r.get("status", "")) == "skipped"]

    denominator = len(executed)
    pass_count = sum(1 for r in executed if bool(r.get("success")))
    pass_rate = round(pass_count / denominator, 6) if denominator else 0.0

    by_no, by_key = _row_index(rows)
    by_kind: dict[str, dict[str, Any]] = {}
    by_dept: dict[str, dict[str, Any]] = {}
    total_unexpected_rows = 0

    for r in executed:
        kind = str(r.get("kind") or "") or "unknown"
        bk = by_kind.setdefault(kind, {
            "rule_count": 0, "pass_count": 0, "fail_count": 0, "unexpected_rows": 0,
        })
        bk["rule_count"] += 1
        if r.get("success"):
            bk["pass_count"] += 1
        else:
            bk["fail_count"] += 1

        details = _details(r)
        count = len(details)
        bk["unexpected_rows"] += count
        total_unexpected_rows += count

        rule_id = str(r.get("rule_id", "") or "")
        for sample in details:
            dept = _sample_dept(sample, by_no, by_key) or "未知科室"
            bd = by_dept.setdefault(dept, {"unexpected_rows": 0, "rule_ids": []})
            bd["unexpected_rows"] += 1
            if rule_id and rule_id not in bd["rule_ids"]:
                bd["rule_ids"].append(rule_id)

    return {
        "pass_rate": pass_rate,
        "denominator": denominator,
        "pass_count": pass_count,
        "fail_count": len(executed) - pass_count,
        "excluded_skipped": len(skipped),
        "excluded_skip_rule_ids": [str(r.get("rule_id", "") or "") for r in skipped],
        "by_kind": by_kind,
        "by_dept": by_dept,
        "total_unexpected_rows": total_unexpected_rows,
        "total_rules": len(results),
        "executed_rules": len(executed),
    }


def numbers_for_display(agg: dict) -> dict:
    """界面 / 导出 / 摘要三处共用的唯一取数函数。

    任何一处要显示通过率、分母、越界行数，都只准取这里返回的数字；这样三处
    数字必然一致，且都与 ``aggregate`` 的口径同源。
    """
    return {
        "pass_rate": agg["pass_rate"],
        "denominator": agg["denominator"],
        "pass_count": agg["pass_count"],
        "fail_count": agg["fail_count"],
        "excluded_skipped": agg["excluded_skipped"],
        "total_unexpected_rows": agg["total_unexpected_rows"],
        "rule_count": agg["executed_rules"],
    }


def numbers_for_export(agg: dict) -> dict:
    """整改清单 / 导出摘要的消费入口：复用同一取数函数。"""
    return numbers_for_display(agg)


def numbers_for_summary(agg: dict) -> dict:
    """批次摘要的消费入口：复用同一取数函数。"""
    return numbers_for_display(agg)


__all__ = [
    "aggregate", "numbers_for_display", "numbers_for_export", "numbers_for_summary",
]
