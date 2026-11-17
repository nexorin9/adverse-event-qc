"""统一结果结构与序列化。

对齐源仓库 `_validate` 的形状：unexpected_count → success = (unexpected_count == 0)
→ partial_unexpected_list。本产品额外保留 total_count / truncated_count，
使「界面只看前 20 条」不变成「台账只查得到 20 条」。
"""
from __future__ import annotations

from typing import Any

from ..models import ExpectedResult


def build_result(
    rule_id: str,
    column: str,
    unexpected: list[dict[str, Any]],
    *,
    observed_value: Any = None,
    limit: int = 20,
    severity: str = "error",
    kind: str = "",
    rule_set_version: str = "",
    basis: str = "",
    info_findings: list[dict[str, Any]] | None = None,
    allowed_unexpected_ratio: float = 0.0,
    row_count: int = 0,
    elapsed_ms: float = 0.0,
) -> ExpectedResult:
    """把逐行失败样本汇总成一条期望结果。

    allowed_unexpected_ratio > 0 时：未匹配比例 ≤ 阈值判 success（允许带病通过，
    但仍记录计数），超过阈值判失败并在 observed_value 中体现比例。
    """
    unexpected = list(unexpected or [])
    total = len(unexpected)
    samples = unexpected[: max(0, limit)]
    truncated = max(0, total - len(samples))

    ratio = total / max(1, row_count) if row_count else (1.0 if total else 0.0)
    if allowed_unexpected_ratio > 0:
        success = ratio <= allowed_unexpected_ratio + 1e-12
        observed_value = {
            "unexpected_count": total,
            "row_count": row_count,
            "unexpected_ratio": round(ratio, 6),
            "allowed_unexpected_ratio": allowed_unexpected_ratio,
            "note": "比例阈值内带病通过" if success else "比例超过阈值",
        }
    else:
        success = total == 0

    return ExpectedResult(
        rule_id=rule_id,
        column=column,
        success=success,
        observed_value=observed_value,
        unexpected_count=total,
        unexpected_samples=samples,
        total_count=total,
        truncated_count=truncated,
        status="executed",
        severity=severity,
        info_findings=list(info_findings or []),
        rule_set_version=rule_set_version,
        basis=basis,
        elapsed_ms=elapsed_ms,
        kind=kind,
    )


def empty_result(rule_id: str, column: str, **kw: Any) -> ExpectedResult:
    return build_result(rule_id, column, [], **kw)


def skipped_result(rule_id: str, column: str, reason: str, **kw: Any) -> ExpectedResult:
    """单条规则失败隔离：记 skipped，其余规则照跑；skipped 不进通过率分母。"""
    kw.pop("limit", None)
    result = ExpectedResult(
        rule_id=rule_id,
        column=column,
        success=False,
        observed_value=None,
        unexpected_count=0,
        unexpected_samples=[],
        total_count=0,
        truncated_count=0,
        status="skipped",
        skip_reason=reason,
        severity=kw.pop("severity", "error"),
        kind=kw.pop("kind", ""),
        rule_set_version=kw.pop("rule_set_version", ""),
        basis=kw.pop("basis", ""),
    )
    return result


def result_to_dict(result: ExpectedResult | dict[str, Any]) -> dict[str, Any]:
    return result.to_dict() if isinstance(result, ExpectedResult) else dict(result)


RESULT_KEYS = ("success", "observed_value", "unexpected_count", "unexpected_samples")
