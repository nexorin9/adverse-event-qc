"""任务 33：降级路径（pending_manual_classification / 越界照常计数 / 不写回 / 原因可查）。"""
from __future__ import annotations

import httpx

from ae_qc.attribution import degradation as deg
from ae_qc.attribution.explain import build_draft
from ae_qc.attribution.model_gateway import ModelGateway
from ae_qc.ledger.audit import AuditLog
from ae_qc.ledger.queries import LedgerQueries
from ae_qc.ledger.state_machine import DEGRADED_STATE, can_write_back
from ae_qc.ledger.store import Store
from ae_qc.models import AttributionDraft


def _unreachable_gateway():
    """本地协议替身：持续 5xx，重试后网关返回结构化 unavailable 标记。"""
    def handler(request):
        return httpx.Response(503, json={"error": "service unavailable"})

    return ModelGateway(base_url="http://model.local.test/v1", api_key="k", model="m",
                        transport=httpx.MockTransport(handler))


def test_model_unreachable_sets_pending_manual_classification():
    """真实网关不可达 → 业务层据此把该行归因置 pending_manual_classification。"""
    gw = _unreachable_gateway()
    resp = gw.structured_call([{"role": "user", "content": "u"}],
                              {"type": "object", "required": ["x"]})
    gw.close()
    assert resp["unavailable"] is True
    assert resp["code"] == "HTTP_5XX"

    draft = deg.degrade_row({"_row_key": "D01|2026-01|fp|7", "_row_no": 7}, resp["code"])
    assert draft.state == "pending_manual_classification"
    assert draft.state == DEGRADED_STATE
    assert draft.source == "degraded"
    assert deg.is_degraded(draft) is True
    assert draft.degraded_reason == "HTTP_5XX"
    assert "模型不可用" in draft.draft_text
    assert can_write_back(draft.state, degraded=deg.is_degraded(draft)) is False


def test_unexpected_count_unchanged_after_degradation(tmp_db):
    """越界计数照常计入批次统计，不因模型挂了而减少。"""
    store = Store(tmp_db)
    queries = LedgerQueries(store)
    queries.save_batch("D01-2026-01", dept_code="D01", period="2026-01")
    queries.save_result("D01-2026-01", {
        "rule_id": "AE-SEV-LEVEL", "column": "severity_level", "status": "executed",
        "success": False, "unexpected_count": 4, "total_count": 4,
    })
    for i in range(4):
        queries.save_evidence("D01-2026-01", "AE-SEV-LEVEL",
                              {"row_key": f"k{i}", "row_no": i + 1, "value": "Z"})
    before = queries.batch_stats("D01-2026-01")
    assert before["unexpected_rows"] == 4
    assert before["unexpected_count"] == 4

    drafts = [deg.degrade_row({"_row_key": "k0"}, "MODEL_UNAVAILABLE"),
              deg.degrade_row({"_row_key": "k1"}, "MODEL_SCHEMA_INVALID")]
    summary = deg.degradation_summary(4, drafts)

    after = queries.batch_stats("D01-2026-01")
    assert after == before
    assert after["unexpected_rows"] == 4
    assert after["unexpected_count"] == 4
    assert summary["unexpected_rows"] == 4
    assert summary["unexpected_count"] == 4
    assert summary["degraded_rows"] == 2
    store.close()


def test_no_writeback_and_zero_outbound_requests():
    """降级行一律不进入写回候选；出站请求数 = 0（只有 confirmed 且未降级才放行）。"""
    drafts = [deg.degrade_row({"_row_key": f"k{i}"}, reason)
              for i, reason in enumerate(("CONN_FAILED", "TIMEOUT", "MODEL_SCHEMA_INVALID"))]
    assert deg.write_back_candidates(drafts) == []
    sent = []
    for candidate in deg.write_back_candidates(drafts):  # 出站发送器只遍历候选
        sent.append(candidate)
    assert sent == []
    summary = deg.degradation_summary(7, drafts)
    assert summary["outbound_requests"] == 0
    assert summary["writeback_candidates"] == []
    assert can_write_back(DEGRADED_STATE) is False
    assert can_write_back("confirmed", degraded=True) is False
    # 反证：未降级且已确认才允许出站
    assert can_write_back("confirmed", degraded=False) is True


def test_degraded_reason_queryable(tmp_db):
    """降级原因可查：审计流水 + 批次摘要 + 界面错误态文案。"""
    store = Store(tmp_db)
    draft = deg.degrade_row({"_row_key": "k9"}, "MODEL_TIMEOUT")
    record = deg.record_degradation(store, "D01-2026-01", draft)
    assert record["reason"]["code"] == "MODEL_TIMEOUT"
    assert record["reason"]["desc"] and record["reason"]["action"]

    events = AuditLog(store).export("D01-2026-01")
    degraded_events = [e for e in events if e["event_type"] == "attribution_degraded"]
    assert len(degraded_events) == 1
    payload = degraded_events[0]["payload"]
    assert payload["code"] == "MODEL_TIMEOUT"
    assert payload["writeback_allowed"] is False
    assert payload["unexpected_count_included"] is True

    reason = deg.degraded_reason(draft)
    assert "模型不可用" in reason["display_text"]
    assert reason["state"] == DEGRADED_STATE
    summary = deg.degradation_summary(3, [draft])
    assert summary["degraded_reasons"][0]["code"] == "MODEL_TIMEOUT"
    assert summary["degraded_reasons"][0]["action"]
    store.close()


def test_degraded_count_and_non_degraded_drafts():
    drafts = [
        deg.degrade_row({"_row_key": "a"}, "CONN_FAILED"),
        deg.degrade_row({"_row_key": "b"}, "NOT_JSON"),
        AttributionDraft(row_key="c", state="attributed_pending", source="model"),
    ]
    assert deg.degraded_count(drafts) == 2
    assert deg.is_degraded(drafts[2]) is False
    # 正常模型草稿（explain）不是降级行
    normal = build_draft({"_row_key": "d"})
    assert deg.is_degraded(normal) is False
