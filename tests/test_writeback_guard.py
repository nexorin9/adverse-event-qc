"""任务 38：写回前置校验 + body 白名单（272 号文）；未确认出站请求数 = 0。

用计数式本地替身统计「未确认时出站请求数」，人工确认是唯一放行口。
"""
from __future__ import annotations

import httpx
import pytest

from ae_qc.ledger.state_machine import DEGRADED_STATE, can_write_back
from ae_qc.reporting.headers import FINDING_BODY_FIELDS
from ae_qc.writeback.guard import (
    ALLOWED_META_FIELDS, FINDING_BODY_FIELDS as GUARD_FIELDS,
    build_body, check_writeback,
)
from ae_qc.writeback.qc_client import QCClient

BASE_URL = "http://qc-platform.test"
TOKEN = "test-token"


def _finding(**overrides):
    f = {
        "finding_id": "EX-F-1", "batch_ref": "D01-2026-01", "dept_code": "D01",
        "period": "2026-01", "rule_id": "AE-SEV-LEVEL",
        "rule_set_version": "v2026-01", "observed_value": "Z",
        "unexpected_count": 1,
        "unexpected_samples": [{"row_no": 2, "value": "Z", "expected": "A–I"}],
        "attribution_draft": "拼写漂移", "attribution_confidence": 0.9,
        "status": "confirmed", "confirmed_by": "示例质控员",
        "confirmed_at": "2026-01-10T10:00:00", "row_key": "ROW-1",
    }
    f.update(overrides)
    return f


@pytest.mark.parametrize("state", [
    "collected", "validated", "attributed_pending",
    "written_back", "rectified", "archived", "",
])
def test_non_confirmed_state_not_allowed(state):
    """非 confirmed 状态一律拒绝出站。"""
    verdict = check_writeback(_finding(), state)
    assert verdict["allowed"] is False
    assert any("WRITEBACK_NOT_CONFIRMED" in r for r in verdict["reasons"])
    assert can_write_back(state) is False


def test_confirmed_state_allowed():
    verdict = check_writeback(_finding(), "confirmed")
    assert verdict["allowed"] is True
    assert verdict["reasons"] == []
    assert can_write_back("confirmed") is True


def test_degraded_row_not_allowed():
    """降级行（含 DEGRADED_STATE）一律不出站。"""
    verdict = check_writeback(_finding(), "confirmed", degraded=True)
    assert verdict["allowed"] is False
    assert any("WRITEBACK_DEGRADED" in r for r in verdict["reasons"])
    assert can_write_back("confirmed", True) is False
    assert check_writeback(_finding(), DEGRADED_STATE)["allowed"] is False


@pytest.mark.parametrize("field", [
    "score", "rank", "penalty", "绩效分", "排名", "扣分", "罚分",
])
def test_forbidden_assessment_fields_rejected(field):
    """body 含 score/rank/penalty/绩效/排名/扣分 类字段被拒（构造与前置都拦）。"""
    bad = _finding()
    bad[field] = 99
    with pytest.raises(ValueError) as exc:
        build_body(bad)
    assert "WRITEBACK_FORBIDDEN_FIELD" in str(exc.value)
    verdict = check_writeback(bad, "confirmed")
    assert verdict["allowed"] is False
    assert any("WRITEBACK_FORBIDDEN_FIELD" in r for r in verdict["reasons"])


@pytest.mark.parametrize("field", [
    "patient_name", "姓名", "住院号", "诊断明细", "患者姓名", "inpatient_no",
])
def test_patient_identifiable_fields_rejected(field):
    bad = _finding()
    bad[field] = "张三"
    with pytest.raises(ValueError):
        build_body(bad)
    assert check_writeback(bad, "confirmed")["allowed"] is False


def test_whitelist_fields_complete():
    """白名单字段齐：body 恰好 14 字段，且与 guard/headers 同源。"""
    body = build_body(_finding())
    assert tuple(body) == FINDING_BODY_FIELDS == GUARD_FIELDS
    assert len(body) == 14
    assert "row_key" not in body
    # 白名单外普通字段直接拒绝构造
    extra = _finding()
    extra["extra_col"] = "x"
    with pytest.raises(ValueError) as exc:
        build_body(extra)
    assert "白名单外字段" in str(exc.value)
    # 缺字段同样拒绝
    incomplete = _finding()
    incomplete.pop("confirmed_at")
    with pytest.raises(ValueError):
        build_body(incomplete)


def test_allowed_meta_fields_do_not_enter_body():
    """row_key / degraded 等元字段允许出现在 finding，但不进 body。"""
    f = _finding(degraded=False, attribution_state="attributed_pending")
    body = build_body(f)
    assert set(body) == set(FINDING_BODY_FIELDS)
    for meta in ALLOWED_META_FIELDS:
        assert meta not in body


def test_unconfirmed_outbound_request_count_is_zero():
    """计数式本地替身：未确认／降级时出站请求数 = 0（QCClient 前置校验拦截）。"""
    calls = []

    def counting_handler(request):
        calls.append(request)
        return httpx.Response(201, json={"receipt_id": "EX-R-X"})

    client = QCClient(BASE_URL, TOKEN, transport=httpx.MockTransport(counting_handler))
    for state in ("collected", "attributed_pending", DEGRADED_STATE):
        result = client.post_finding(_finding(status=state, row_key=f"RK-{state}"),
                                     row_key=f"RK-{state}")
        assert result["ok"] is False
    degraded_result = client.post_finding(_finding(degraded=True, row_key="RK-D"),
                                          row_key="RK-D")
    assert degraded_result["ok"] is False
    assert degraded_result["code"] == "WRITEBACK_DEGRADED"
    assert client.request_count == 0
    assert len(calls) == 0
