"""任务 31：语义归并（目录内成功 / 目录外拒绝 / 无法归类说明 / 逐行对齐）。"""
from __future__ import annotations

import pytest

from ae_qc.attribution.merge import MERGE_SCHEMA, UNCLASSIFIABLE_LABEL, merge_event_names


def _row(no: int, text: str) -> dict:
    return {
        "incident_id": f"EXAMPLE-{no:04d}", "dept_code": "D01",
        "event_type_text": text, "event_type_code": "",
        "severity_level": "A", "severity_class": "Ⅳ",
        "_row_key": f"D01|2026-01|merge{no}|{no}", "_row_no": no,
        "_batch_ref": "D01-2026-01",
    }


def _candidate(code: str, **overrides) -> dict:
    data = {"code": code, "confidence": 0.88, "reason": "语义归并到目录编码",
            "unclassifiable": False, "missing_info": []}
    data.update(overrides)
    return data


class _MergeStub:
    """测试层协议替身：按队列返回结构化响应，可注入单行异常与不可用。"""

    def __init__(self, responses=None, *, unavailable=False, fail_at=None):
        self.responses = list(responses or [])
        self.unavailable = unavailable
        self.fail_at = fail_at
        self.call_count = 0
        self.schemas = []

    def structured_call(self, messages, schema, **kw):
        self.call_count += 1
        self.schemas.append(schema)
        if self.fail_at == self.call_count:
            raise RuntimeError("injected single-row failure")
        if self.unavailable:
            return {"ok": False, "data": None, "unavailable": True,
                    "code": "CONN_FAILED", "meta": {}}
        data = self.responses.pop(0) if self.responses else _candidate("")
        return {"ok": True, "data": data, "unavailable": False, "code": "", "meta": {}}


def test_in_catalog_merge_success(model_stub, categories):
    """目录内归并成功：模型候选在目录内 → merged + 编码；未命中行才调模型。"""
    model_stub.responses = [_candidate("AE-CAT-08")]
    rows = [_row(1, "未登记的自由文本事件名")]
    result = merge_event_names(rows, model_stub, categories)

    assert len(result) == 1
    hit = result[0]
    assert hit["row_key"] == rows[0]["_row_key"]
    assert hit["status"] == "merged"
    assert hit["decision_code"] == "AE-CAT-08"
    assert hit["source"] == "model"
    assert hit["confidence"] == pytest.approx(0.88)
    assert hit["needs_human"] is False
    assert model_stub.call_count == 1
    assert model_stub.calls[0]["schema"] == MERGE_SCHEMA


def test_out_of_catalog_candidate_rejected(categories):
    """目录外候选被拒：落「无法归类·待人工指定」，不取最近类，并说明缺哪个信息。"""
    stub = _MergeStub([_candidate("AE-CAT-99", reason="模型幻觉出目录外编码")])
    result = merge_event_names([_row(2, "某自由文本")], stub, categories)

    hit = result[0]
    assert hit["status"] == "unclassifiable"
    assert hit["label"] == UNCLASSIFIABLE_LABEL
    assert hit["decision_code"] is None
    assert hit["source"] == "manual"
    assert hit["needs_human"] is True
    assert hit["error_code"] == "MODEL_CANDIDATE_NOT_IN_CATALOG"
    assert hit["candidate_code"] == "AE-CAT-99"
    assert hit["missing_info"]
    assert any("目录" in info or "人工指定" in info for info in hit["missing_info"])
    assert "AE-CAT-99" in hit["reason"]


def test_unclassifiable_gives_missing_info(categories):
    """模型明确判无法归类：missing_info 原样保留且非空。"""
    stub = _MergeStub([_candidate("", unclassifiable=True, confidence=0.0,
                                  reason="分类信息不足", missing_info=["事件经过", "后果描述"])])
    result = merge_event_names([_row(3, "信息不足事件")], stub, categories)

    hit = result[0]
    assert hit["status"] == "unclassifiable"
    assert hit["missing_info"] == ["事件经过", "后果描述"]
    assert hit["missing_info"]


def test_rows_aligned_by_row_key_and_single_row_failure_isolated(categories):
    """逐行按 row_key 对齐不错位；中间一行抛异常不拖垮整组。"""
    stub = _MergeStub(
        [_candidate("AE-CAT-08"), _candidate("AE-CAT-02")],
        fail_at=2,  # 第 2 次模型调用（第 2 个未命中行）注入失败
    )
    rows = [_row(1, "甲事件"), _row(2, "乙事件"), _row(3, "丙事件")]
    result = merge_event_names(rows, stub, categories)

    assert [r["row_key"] for r in result] == [r["_row_key"] for r in rows]
    assert result[0]["status"] == "merged"
    assert result[0]["decision_code"] == "AE-CAT-08"
    assert result[1]["status"] == "degraded"
    assert result[1]["unavailable"] is True
    assert result[2]["status"] == "merged"
    assert result[2]["decision_code"] == "AE-CAT-02"


def test_alias_hit_skips_model(categories):
    """别名表精确命中的行不调模型（确定性基线优先）。"""
    stub = _MergeStub([_candidate("AE-CAT-02")])
    rows = [_row(1, "跌倒"), _row(2, "某未命中文本")]
    result = merge_event_names(rows, stub, categories, aliases={"跌倒": "AE-CAT-08"})

    assert result[0]["status"] == "alias_hit"
    assert result[0]["source"] == "alias"
    assert result[0]["decision_code"] == "AE-CAT-08"
    assert result[1]["status"] == "merged"
    assert stub.call_count == 1  # 只有 1 行未命中，只调 1 次模型


def test_model_unavailable_degrades_single_row(categories):
    """网关 unavailable → 该行降级且带原因码，其余行照常处理。"""
    stub = _MergeStub(unavailable=True)
    result = merge_event_names([_row(4, "自由文本")], stub, categories)
    assert result[0]["status"] == "degraded"
    assert result[0]["unavailable"] is True
    assert result[0]["error_code"] == "CONN_FAILED"
    assert result[0]["needs_human"] is True
    assert stub.call_count == 1
