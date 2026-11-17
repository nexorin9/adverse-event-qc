"""任务 47：确认/驳回按钮存在；点击走状态机 / 反馈回流接口；撤回二次确认；
降级行显示原因与重试入口；desktop 下无底层存储直连。

无显示环境：面板用 StubFactory 的 fake widget，对话框用注入的 fake dialog。
"""
from __future__ import annotations

import re
from pathlib import Path

from ae_qc.attribution.degradation import degrade_row
from ae_qc.attribution.feedback_loop import FeedbackLoop
from ae_qc.desktop.views import confirm_panel as cp_mod
from ae_qc.desktop.views.confirm_panel import ConfirmPanel
from ae_qc.ledger import state_machine
from ae_qc.ledger.queries import LedgerQueries
from ae_qc.ledger.store import Store


def _queries(tmp_db):
    q = LedgerQueries(Store(tmp_db))
    q.save_batch("D01-2026-01", dept_code="D01", period="2026-01")
    return q


def _draft(**kw):
    draft = {"row_key": "ROW2", "draft_text": "疑似拼写漂移，建议核对目录编码",
             "cause_type": "spelling_drift", "suggested_code": "AE-CAT-08",
             "confidence": 0.86, "missing_info": [], "state": "attributed_pending",
             "source": "model", "degraded_reason": "", "batch_ref": "D01-2026-01"}
    draft.update(kw)
    return draft


def test_confirm_and_reject_buttons_exist_and_show_fields(tmp_db):
    q = _queries(tmp_db)
    panel = ConfirmPanel(store=q.store,
                         feedback_loop=FeedbackLoop(catalogs_dir=Path(tmp_db).parent,
                                                    store=q.store))
    assert set(panel.buttons) == {"confirm", "reject", "withdraw", "retry"}
    for name, button in panel.buttons.items():
        assert hasattr(button, "configure") and hasattr(button, "invoke"), name
    shown = panel.show(_draft(), basis="《分级分类标准》附件2")
    assert shown["fields"]["草稿"].startswith("疑似拼写漂移")
    assert shown["fields"]["置信度"] == "0.86"
    assert shown["fields"]["建议编码"] == "AE-CAT-08"
    assert shown["fields"]["依据"] == "《分级分类标准》附件2"
    assert shown["fields"]["状态"] == "attributed_pending"
    assert shown["degraded"] is False


def test_confirm_click_goes_through_state_machine(tmp_db, monkeypatch):
    q = _queries(tmp_db)
    calls = []
    original = state_machine.transition

    def spy(store, finding_id, from_state, to_state, actor, reason="", *, batch_ref=""):
        calls.append({"finding_id": finding_id, "from": from_state, "to": to_state,
                      "actor": actor, "batch_ref": batch_ref})
        return original(store, finding_id, from_state, to_state, actor, reason,
                        batch_ref=batch_ref)

    monkeypatch.setattr(state_machine, "transition", spy)
    panel = ConfirmPanel(store=q.store, dialog=lambda *a: True)
    panel.show(_draft(), finding_id="F-1", from_state="attributed_pending",
               batch_ref="D01-2026-01")
    result = panel.confirm_button.invoke()
    assert calls and calls[0]["to"] == "confirmed"
    assert calls[0]["from"] == "attributed_pending"
    assert calls[0]["batch_ref"] == "D01-2026-01"
    assert result["ok"] is True and result["to_state"] == "confirmed"
    assert panel.last_action["action"] == "confirm"
    # 状态机真的写了审计流水（不是空转）
    events = q.store.query(
        "SELECT event_type FROM audit_events WHERE event_type='state_transition'")
    assert len(events) == 1


def test_reject_click_goes_through_feedback_loop(tmp_db):
    q = _queries(tmp_db)
    fl = FeedbackLoop(catalogs_dir=Path(tmp_db).parent, store=q.store)
    panel = ConfirmPanel(store=q.store, feedback_loop=fl)
    panel.show(_draft(), finding_id="F-1")
    panel.set_row({"event_type_text": "跌倒", "_row_key": "ROW2"})
    result = panel.reject_button.invoke()
    assert result["ok"] is True
    assert panel.last_action["action"] == "reject"
    candidates = fl.candidates(action="reject")
    assert len(candidates) == 1
    assert candidates[0]["suggested_code"] == "AE-CAT-08"
    assert candidates[0]["row_key"] == "ROW2"


def test_withdraw_requires_second_confirmation(tmp_db, monkeypatch):
    q = _queries(tmp_db)
    asks = []

    def deny(title, message):
        asks.append((title, message))
        return False

    panel = ConfirmPanel(store=q.store, dialog=deny)
    panel.show(_draft(state="confirmed"), finding_id="F-1", from_state="confirmed")
    result = panel.withdraw_button.invoke()
    assert asks and "二次确认" in asks[0][0]
    assert result["withdrawn"] is False and result["ok"] is False
    assert panel.last_action["action"] == "withdraw"

    withdraw_calls = []
    original = state_machine.withdraw

    def spy(store, finding_id, actor, reason="", *, batch_ref=""):
        withdraw_calls.append(finding_id)
        return original(store, finding_id, actor, reason, batch_ref=batch_ref)

    monkeypatch.setattr(state_machine, "withdraw", spy)
    panel2 = ConfirmPanel(store=q.store, dialog=lambda *a: True)
    panel2.show(_draft(state="confirmed"), finding_id="F-2", from_state="confirmed")
    result2 = panel2.withdraw_button.invoke()
    assert withdraw_calls == ["F-2"]
    assert result2["withdrawn"] is True
    assert q.store.query("SELECT event_type FROM audit_events WHERE event_type='withdraw'")


def test_degraded_row_shows_reason_and_retry_entry():
    draft = degrade_row({"_row_key": "R9"}, "MODEL_UNAVAILABLE").to_dict()
    retried = []
    panel = ConfirmPanel(retry_callback=lambda d, r: retried.append(d["row_key"]))
    shown = panel.show(draft)
    assert shown["degraded"] is True
    assert shown["degraded_reason"] and "模型不可用" in shown["degraded_reason"]
    assert "模型不可用" in panel.labels["降级原因"].cget("text")
    assert panel.confirm_button.cget("state") == "disabled"   # 降级行不许确认
    assert panel.retry_button.cget("state") == "normal"       # 重试入口可用
    rejected = panel.confirm_button.invoke()
    assert rejected["ok"] is False and rejected["code"] == "WRITEBACK_DEGRADED"
    retried_result = panel.retry_button.invoke()
    assert retried_result["ok"] is True and retried == ["R9"]


def test_desktop_has_no_direct_store_connection():
    desktop_dir = Path(cp_mod.__file__).resolve().parent.parent
    files = sorted(desktop_dir.glob("*.py")) + sorted((desktop_dir / "views").glob("*.py"))
    assert len(files) >= 9
    # 精确匹配真实 SQL 语句，避免误伤 on_select / selection 等标识符
    sql_pattern = re.compile(
        r"\b(?:SELECT\s+.+?\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|"
        r"DELETE\s+FROM|CREATE\s+TABLE)\b", re.IGNORECASE)
    for f in files:
        src = f.read_text(encoding="utf-8")
        assert "sqlite" not in src.lower(), f"{f.name} 含标准库存储模块字样"
        assert not sql_pattern.search(src), f"{f.name} 含直接 SQL 语句"
        assert ".connect(" not in src and '.execute("' not in src
