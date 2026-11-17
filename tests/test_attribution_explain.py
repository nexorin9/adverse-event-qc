"""任务 32：归因草稿（四类判定 / 缺信息 missing_info / 状态恒 attributed_pending）。"""
from __future__ import annotations

import pytest

from ae_qc.attribution.explain import CAUSE_TYPES, EXPLAIN_SCHEMA, build_draft
from ae_qc.attribution.model_gateway import validate_schema
from ae_qc.ledger.queries import LedgerQueries
from ae_qc.ledger.store import Store


def _row(**overrides) -> dict:
    base = {
        "incident_id": "EXAMPLE-0001", "dept_code": "D01",
        "event_type_text": "未登记的自由文本事件名", "event_type_code": "",
        "severity_level": "Z", "severity_class": "Ⅳ", "harm_desc": "未累及患者",
        "root_cause_text": "地面湿滑",
        "_row_key": "D01|2026-01|explain1|1", "_row_no": 1,
        "_batch_ref": "D01-2026-01",
    }
    base.update(overrides)
    return base


def _data(cause: str, **overrides) -> dict:
    data = {"draft_text": f"归因草稿：{cause}", "cause_type": cause,
            "suggested_code": "AE-CAT-08", "confidence": 0.72, "missing_info": []}
    data.update(overrides)
    return data


class _ExplainStub:
    def __init__(self, data=None, *, unavailable=False):
        self.data = data
        self.unavailable = unavailable
        self.call_count = 0

    def structured_call(self, messages, schema, **kw):
        self.call_count += 1
        if self.unavailable:
            return {"ok": False, "data": None, "unavailable": True,
                    "code": "MODEL_UNAVAILABLE", "meta": {}}
        return {"ok": True, "data": self.data, "unavailable": False, "code": "", "meta": {}}


@pytest.mark.parametrize("cause", list(CAUSE_TYPES))
def test_four_cause_types(model_stub, cause):
    """四类判定各一例，且草稿状态恒为 attributed_pending。"""
    model_stub.responses = [_data(cause)]
    draft = build_draft(_row(), model_stub)

    assert draft.cause_type == cause
    assert draft.state == "attributed_pending"
    assert draft.source == "model"
    assert draft.row_key == _row()["_row_key"]
    assert draft.confidence == pytest.approx(0.72)
    assert model_stub.call_count == 1


def test_draft_output_fixed_schema():
    """固定 schema（5 字段），产出的草稿 dict 能通过 schema 校验。"""
    assert set(EXPLAIN_SCHEMA["required"]) == {
        "draft_text", "cause_type", "suggested_code", "confidence", "missing_info",
    }
    draft = build_draft(_row(), _ExplainStub(_data("spelling_drift")))
    assert validate_schema(draft.to_dict(), EXPLAIN_SCHEMA) is True
    assert set(EXPLAIN_SCHEMA["required"]).issubset(draft.to_dict())


def test_missing_info_always_non_empty():
    """缺信息时 missing_info 非空：模型给空列表也要补出具体缺哪个信息。"""
    for cause in ("missing_info", "new_event"):
        draft = build_draft(_row(), _ExplainStub(_data(cause, missing_info=[])))
        assert draft.missing_info
        assert all(str(x).strip() for x in draft.missing_info)

    # 非缺信息类不应被强行塞入缺失信息
    normal = build_draft(_row(), _ExplainStub(_data("spelling_drift", missing_info=[])))
    assert normal.missing_info == []


def test_state_never_confirmed_even_if_model_writes_confirmed():
    """模型产出永不推进状态、人工确认是唯一放行口：模型写 confirmed 也被丢弃。"""
    malicious = _data("standard_change", state="confirmed", status="confirmed")
    draft = build_draft(_row(), _ExplainStub(malicious))
    assert draft.state == "attributed_pending"
    assert "confirmed" not in draft.to_dict().get("state", "")
    assert draft.source == "model"


def test_no_gateway_and_unavailable_gateway_still_attributed_pending():
    """无模型（规则基线）与模型不可达时，状态依旧恒为 attributed_pending。"""
    baseline = build_draft(_row())
    assert baseline.state == "attributed_pending"
    assert baseline.source == "manual"

    unavailable = build_draft(_row(), _ExplainStub(unavailable=True))
    assert unavailable.state == "attributed_pending"
    assert unavailable.source == "degraded"
    assert unavailable.degraded_reason == "MODEL_UNAVAILABLE"


def test_new_event_only_suggests_catalog_addition():
    """「确属新事件」只产出建议，不自动改目录，目录变更须走人工审核。"""
    draft = build_draft(_row(), _ExplainStub(_data("new_event")))
    assert draft.cause_type == "new_event"
    assert "建议" in draft.draft_text
    assert "不自动改目录" in draft.draft_text
    assert "人工审核" in draft.draft_text
    assert draft.state == "attributed_pending"


def test_draft_persisted_state_is_attributed_pending(tmp_db):
    """草稿落库状态恒 attributed_pending（写进台账再读回验证）。"""
    store = Store(tmp_db)
    queries = LedgerQueries(store)
    draft = build_draft(_row(), _ExplainStub(_data("spelling_drift")))
    queries.save_draft("D01-2026-01", draft.to_dict())

    pending = queries.pending_attributions()
    assert len(pending) == 1
    assert pending[0]["row_key"] == draft.row_key
    assert pending[0]["state"] == "attributed_pending"
    assert pending[0]["draft"]["cause_type"] == "spelling_drift"
    store.close()
