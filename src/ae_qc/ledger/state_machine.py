"""业务状态机：合法迁移、非法迁移拒绝、撤回留痕、写回许可查询。

状态链 collected→validated→attributed_pending→confirmed→written_back→rectified→archived。
模型产出永不推进状态；「当前是否允许出站写回」由状态机回答，不由调用方自觉判断。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..errors import ERROR_CODES
from .store import Store, dumps, now_iso

STATES = (
    "collected", "validated", "attributed_pending", "confirmed",
    "written_back", "rectified", "archived",
)

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "collected": ("validated",),
    "validated": ("attributed_pending", "collected"),
    "attributed_pending": ("confirmed", "validated"),
    "confirmed": ("written_back", "attributed_pending"),  # 后者＝撤回
    "written_back": ("rectified",),
    "rectified": ("archived",),
    "archived": (),
}

# 降级态不在七态链内：模型不可达时归因待人工，一律不许出站
DEGRADED_STATE = "pending_manual_classification"


def is_legal(from_state: str, to_state: str) -> bool:
    return to_state in TRANSITIONS.get(from_state, ())


def can_write_back(state: str, degraded: bool = False) -> bool:
    """只有 confirmed 且未降级才允许出站；其余一律 False。"""
    return state == "confirmed" and not degraded


def _audit(store: Store, event_type: str, batch_ref: str, actor: str, payload: dict) -> None:
    body = dict(payload)
    body.setdefault("actor", actor or "")
    store.execute(
        "INSERT INTO audit_events(ts,event_type,batch_ref,actor,payload_json) VALUES (?,?,?,?,?)",
        (now_iso(), event_type, batch_ref or "", actor or "", dumps(body)),
    )


def transition(store: Store, finding_id: str, from_state: str, to_state: str,
               actor: str, reason: str = "", *, batch_ref: str = "") -> dict[str, Any]:
    """执行一次状态迁移；非法迁移抛 ValueError 并写审计（含被拒迁移与操作人）。"""
    store.initialize()
    if not is_legal(from_state, to_state):
        _audit(store, "state_transition_rejected", batch_ref, actor,
               {"finding_id": finding_id, "from": from_state, "to": to_state,
                "reason": reason or "非法迁移", "code": "STATE_TRANSITION_ILLEGAL"})
        raise ValueError(f"非法状态迁移：{from_state} → {to_state}（已记审计，操作人 {actor or '未知'}）")

    confirmed_by = actor if to_state == "confirmed" else None
    confirmed_at = datetime.now().isoformat(timespec="seconds") if to_state == "confirmed" else None
    store.execute(
        "UPDATE findings SET status=?, confirmed_by=COALESCE(?,confirmed_by),"
        " confirmed_at=COALESCE(?,confirmed_at) WHERE finding_id=?",
        (to_state, confirmed_by, confirmed_at, finding_id),
    )
    _audit(store, "state_transition", batch_ref, actor,
           {"finding_id": finding_id, "from": from_state, "to": to_state,
            "reason": reason, "confirmed_by": confirmed_by, "confirmed_at": confirmed_at})
    return {"finding_id": finding_id, "from": from_state, "to": to_state,
            "actor": actor, "legal": True, "confirmed_by": confirmed_by,
            "confirmed_at": confirmed_at}


def withdraw(store: Store, finding_id: str, actor: str, reason: str = "",
             *, batch_ref: str = "") -> dict[str, Any]:
    """撤回：confirmed → attributed_pending，留痕（撤回人、时间、原因）。"""
    result = transition(store, finding_id, "confirmed", "attributed_pending", actor,
                        reason or "质控员撤回已确认结论", batch_ref=batch_ref)
    _audit(store, "withdraw", batch_ref, actor,
           {"finding_id": finding_id, "reason": reason, "time": now_iso()})
    result["withdrawn"] = True
    return result


def state_info() -> dict[str, Any]:
    return {"states": list(STATES), "transitions": {k: list(v) for k, v in TRANSITIONS.items()},
            "degraded_state": DEGRADED_STATE,
            "illegal_transition_code": ERROR_CODES["STATE_TRANSITION_ILLEGAL"]}
