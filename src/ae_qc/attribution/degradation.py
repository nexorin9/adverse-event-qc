"""降级路径：模型不可达时归因待人工，越界照常计数，一律不写回。

模型产出永不推进状态、人工确认是唯一放行口；降级行连草稿状态都不推进到
confirmed，更不允许进入写回候选（出站请求数恒为 0）。降级原因写入审计流水、
批次摘要，界面错误态文案亦由此处提供，绝不假装成功。
"""
from __future__ import annotations

from typing import Any

from ..errors import describe
from ..ledger.audit import AuditLog
from ..ledger.state_machine import DEGRADED_STATE, can_write_back
from ..models import AttributionDraft


def _as_dict(draft: Any) -> dict[str, Any]:
    if isinstance(draft, AttributionDraft):
        return draft.to_dict()
    if isinstance(draft, dict):
        return dict(draft)
    return {}


def degrade_row(row: dict, reason_code: str) -> AttributionDraft:
    """把一行归因降级为人工指定：state=pending_manual_classification，原因码可查。"""
    row = row if isinstance(row, dict) else {}
    row_key = str(row.get("_row_key", row.get("row_key", "")))
    code = str(reason_code or "MODEL_UNAVAILABLE")
    info = describe(code)
    return AttributionDraft(
        row_key=row_key,
        draft_text=(f"模型不可用，本行归因降级为人工指定：{info['desc']}；"
                    f"处置建议：{info['action']}。越界计数照常计入，但不写回。"),
        cause_type="missing_info",
        suggested_code="",
        confidence=0.0,
        missing_info=["模型归因结论不可用，需人工指定目录编码与归因类型"],
        state=DEGRADED_STATE,
        source="degraded",
        degraded_reason=code,
    )


def is_degraded(draft: Any) -> bool:
    """降级判定：状态 / 来源 / 原因码任一命中即为降级行。"""
    d = _as_dict(draft)
    return (str(d.get("state", "")) == DEGRADED_STATE
            or str(d.get("source", "")) == "degraded"
            or bool(d.get("degraded_reason")))


def degraded_count(drafts: list) -> int:
    return sum(1 for d in (drafts or []) if is_degraded(d))


def degraded_reason(draft: Any) -> dict[str, Any]:
    """降级原因可查：错误码 + 说明 + 处置建议 + 状态（供界面错误态文案与审计）。"""
    d = _as_dict(draft)
    code = str(d.get("degraded_reason") or "")
    info = describe(code) if code else {"layer": "", "desc": "", "action": ""}
    return {
        "row_key": str(d.get("row_key", "")),
        "code": code,
        "desc": info["desc"],
        "action": info["action"],
        "layer": info["layer"],
        "state": str(d.get("state", "")),
        "display_text": f"模型不可用，本批待人工归类：{info['desc']}；{info['action']}",
    }


def write_back_candidates(drafts: list) -> list[dict]:
    """写回候选：降级行一律排除（can_write_back 恒 False），其余交状态机裁决。"""
    out: list[dict] = []
    for draft in drafts or []:
        d = _as_dict(draft)
        if is_degraded(d):
            continue
        if can_write_back(str(d.get("state", "")), degraded=is_degraded(d)):
            out.append(d)
    return out


def degradation_summary(unexpected_rows: int | list, drafts: list) -> dict[str, Any]:
    """批次摘要：越界计数原样透传（不因模型挂了而减少），降级行数与原因显式列出。"""
    count = int(unexpected_rows) if isinstance(unexpected_rows, int) else len(unexpected_rows)
    degraded = [d for d in (drafts or []) if is_degraded(d)]
    return {
        "unexpected_rows": count,          # 问题不会因为模型挂了就消失
        "unexpected_count": count,
        "degraded_rows": len(degraded),
        "writeback_candidates": write_back_candidates(drafts),  # 恒空
        "outbound_requests": 0,           # 降级行一律不写回：出站请求数 = 0
        "degraded_reasons": [degraded_reason(d) for d in degraded],
    }


def record_degradation(store: Any, batch_ref: str, draft: Any) -> dict[str, Any]:
    """把降级原因写入审计流水（供证据包与界面错误态查询）。"""
    d = _as_dict(draft)
    reason = degraded_reason(d)
    event = AuditLog(store).write(
        "attribution_degraded", batch_ref,
        row_key=reason["row_key"], state=reason["state"], code=reason["code"],
        reason=reason["desc"], action=reason["action"],
        unexpected_count_included=True, writeback_allowed=False,
    )
    return {"audit": event, "reason": reason}


__all__ = [
    "degrade_row", "is_degraded", "degraded_count", "degraded_reason",
    "write_back_candidates", "degradation_summary", "record_degradation",
]
