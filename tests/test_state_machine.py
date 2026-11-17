"""任务 26：合法迁移 / 非法迁移拒绝 / 撤回在案 / 写回许可查询。"""
from __future__ import annotations

import pytest

from ae_qc.ledger.state_machine import (
    STATES, TRANSITIONS, can_write_back, state_info, transition, withdraw,
)
from ae_qc.ledger.store import Store


def _store_with_finding(tmp_db, state="collected"):
    store = Store(tmp_db)
    store.initialize()
    store.execute(
        "INSERT INTO findings(finding_id,batch_ref,dept_code,period,rule_id,status,created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        ("EX-F-1", "D01-2026-01", "D01", "2026-01", "AE-SEV-LEVEL", state, "2026-01-01T00:00:00"),
    )
    return store


def test_seven_states_walk_in_order(tmp_db):
    store = _store_with_finding(tmp_db)
    chain = list(STATES)
    for frm, to in zip(chain, chain[1:]):
        result = transition(store, "EX-F-1", frm, to, "示例质控员", batch_ref="D01-2026-01")
        assert result["legal"] is True and result["to"] == to
    row = store.query("SELECT status FROM findings WHERE finding_id='EX-F-1'")[0]
    assert row["status"] == "archived"


def test_illegal_transition_rejected_and_audited(tmp_db):
    store = _store_with_finding(tmp_db, "archived")
    before = store.query("SELECT COUNT(*) AS n FROM audit_events")[0]["n"]
    with pytest.raises(ValueError) as exc:
        transition(store, "EX-F-1", "archived", "confirmed", "示例质控员",
                   batch_ref="D01-2026-01")
    assert "非法状态迁移" in str(exc.value)
    events = store.query("SELECT * FROM audit_events ORDER BY id")
    assert len(events) == before + 1
    assert events[-1]["event_type"] == "state_transition_rejected"
    payload = events[-1]["payload_json"]
    assert "archived" in payload and "confirmed" in payload and "示例质控员" in payload


def test_withdraw_is_recorded(tmp_db):
    store = _store_with_finding(tmp_db, "confirmed")
    result = withdraw(store, "EX-F-1", "示例质控员", "科室补充了分级说明",
                      batch_ref="D01-2026-01")
    assert result["withdrawn"] is True and result["to"] == "attributed_pending"
    events = store.query("SELECT * FROM audit_events WHERE event_type='withdraw'")
    assert len(events) == 1
    assert "科室补充了分级说明" in events[0]["payload_json"]
    assert store.query("SELECT status FROM findings WHERE finding_id='EX-F-1'")[0]["status"] == "attributed_pending"


@pytest.mark.parametrize("state,degraded,expected", [
    ("confirmed", False, True),
    ("attributed_pending", False, False),
    ("pending_manual_classification", False, False),
    ("confirmed", True, False),
])
def test_writeback_permission_for_four_states(state, degraded, expected):
    assert can_write_back(state, degraded) is expected


def test_state_info_exposes_transition_table():
    info = state_info()
    assert len(info["states"]) == 7
    assert info["transitions"]["archived"] == []
    assert info["degraded_state"] == "pending_manual_classification"
    assert info["illegal_transition_code"]["layer"] == "台账"
    assert info["illegal_transition_code"]["action"]
