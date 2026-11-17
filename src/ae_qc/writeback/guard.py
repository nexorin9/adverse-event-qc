"""写回前置校验 + body 白名单构造（272 号文硬约束）。

人工确认是唯一放行口：状态必须为 confirmed 且未降级才允许出站；模型产出、
降级行、任何非 confirmed 状态一律拒绝，此时出站请求数 = 0（由 qc_client 在
发请求前调用本模块保证）。模型产出永不推进状态；年度底稿不自动外发。
"""
from __future__ import annotations

from typing import Any

from ..ledger.state_machine import DEGRADED_STATE, can_write_back
from ..reporting.headers import (
    FINDING_BODY_FIELDS, FORBIDDEN_FIELD_TOKENS, assert_no_forbidden,
)

# 允许出现在 finding 里、但按 spec 不进入出站 body 的元字段：
# row_key 用于算幂等键；degraded / attribution_state 用于降级判定。
ALLOWED_META_FIELDS: frozenset[str] = frozenset({
    "row_key", "_row_key", "degraded", "_degraded", "attribution_state",
})


def check_writeback(finding: dict, state: str, degraded: bool = False) -> dict[str, Any]:
    """前置校验（不抛异常，返回 allowed/reasons），供 qc_client / queue 复用。

    三重判定：① 状态机许可（confirmed 且未降级）；② 无禁用字段（考核类 / PII）；
    ③ 无白名单外字段。任一不满足即 allowed=False，并逐条给出 reasons。
    """
    reasons: list[str] = []
    if not can_write_back(state, degraded):
        if degraded or state == DEGRADED_STATE:
            reasons.append(
                "WRITEBACK_DEGRADED：归因降级行（pending_manual_classification）"
                "不得出站，须先人工完成归类；人工确认是唯一放行口"
            )
        elif state != "confirmed":
            reasons.append(
                f"WRITEBACK_NOT_CONFIRMED：状态={state!r} 非 confirmed，"
                "模型产出永不推进状态，人工确认是唯一放行口，出站请求数 = 0"
            )
        else:
            reasons.append("WRITEBACK_NOT_CONFIRMED：状态机拒绝出站")
    try:
        assert_no_forbidden(finding)
    except ValueError as exc:
        reasons.append(f"WRITEBACK_FORBIDDEN_FIELD：{exc}")
    extra = sorted(k for k in finding
                   if k not in FINDING_BODY_FIELDS and k not in ALLOWED_META_FIELDS)
    if extra:
        reasons.append(
            f"WRITEBACK_FORBIDDEN_FIELD：body 出现白名单外字段 {extra}，"
            "须按 spec 14 字段白名单重建（272 号文：不产出考核类字段）"
        )
    return {"allowed": not reasons, "reasons": reasons}


def build_body(finding: dict) -> dict[str, Any]:
    """按 spec 14 字段白名单构造出站 body。

    禁用字段（score/rank/penalty/绩效/排名/扣分 与患者可识别字段）与白名单外
    字段一律**直接拒绝构造**（抛 ValueError），绝不静默丢弃后出站；缺任一白名单
    字段同样拒绝，保证 body 与 spec 逐字段一致。
    """
    try:
        assert_no_forbidden(finding)  # 命中禁用/PII 字段即拒绝
    except ValueError as exc:
        raise ValueError(f"WRITEBACK_FORBIDDEN_FIELD：{exc}") from exc
    extra = sorted(k for k in finding
                   if k not in FINDING_BODY_FIELDS and k not in ALLOWED_META_FIELDS)
    if extra:
        raise ValueError(
            f"WRITEBACK_FORBIDDEN_FIELD：body 出现白名单外字段 {extra}，"
            "按 spec 14 字段白名单重建后方可出站"
        )
    missing = [f for f in FINDING_BODY_FIELDS if f not in finding]
    if missing:
        raise ValueError(
            f"WRITEBACK_FORBIDDEN_FIELD：body 缺 spec 白名单字段 {missing}，"
            "14 字段须逐字段齐备"
        )
    body = {f: finding[f] for f in FINDING_BODY_FIELDS}
    try:
        assert_no_forbidden(body)
    except ValueError as exc:
        raise ValueError(f"WRITEBACK_FORBIDDEN_FIELD：{exc}") from exc
    return body


__all__ = [
    "FINDING_BODY_FIELDS", "FORBIDDEN_FIELD_TOKENS", "ALLOWED_META_FIELDS",
    "check_writeback", "build_body",
]
