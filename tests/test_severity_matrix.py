"""任务 24：严重程度—类别一致性矩阵 + 别名优先归并与模型仲裁。

覆盖：非法等级类别组合被捕获；合法组合通过；表中没有的组合保守拒绝；半缺不重复
计数；别名命中不触发模型调用（用 conftest 的 GatewayStub 调用计数断言）；
别名与模型冲突时双方证据都在并交人工；一致时不交人工；矩阵可从目录加载。
"""
from __future__ import annotations

from ae_qc.engine import severity_matrix as sm
from ae_qc.engine.rule_kinds import alias_lookup


def _row(no: int, **overrides) -> dict:
    base = {
        "_row_no": no,
        "_row_key": f"k{no}",
        "_dept_code": "D01",
        "_period": "2026-01",
        "severity_level": "A",
        "category_family": "safety",
        "event_type_text": "跌倒",
    }
    base.update(overrides)
    return base


def _arbitrate_with_stub(text, aliases, stub, row=None):
    """测试层仲裁驱动：别名命中直接判定不调模型；未命中才调 GatewayStub。"""
    hit = alias_lookup(text, aliases)
    if hit:
        return sm.arbitrate(hit, None, row or {})
    response = stub.structured_call([{"role": "user", "content": text}], {"type": "object"})
    return sm.arbitrate(None, response.get("data"), row or {})


def test_illegal_combination_is_caught():
    matrix = sm.load_matrix()
    assert matrix
    assert sm.check_combination("A", "transfusion", matrix) is False  # 目录里 allowed=0
    res = sm.evaluate_matrix([_row(1, severity_level="A", category_family="transfusion")], matrix)
    assert res.success is False
    assert res.unexpected_count == 1
    assert res.unexpected_samples[0]["reason"] == "等级—类别族组合越界"


def test_legal_combination_passes():
    matrix = sm.load_matrix()
    assert sm.check_combination("A", "safety", matrix) is True
    assert sm.check_combination("I", "clinical", matrix) is True
    rows = [_row(1), _row(2, severity_level="B", category_family="medication")]
    res = sm.evaluate_matrix(rows, matrix)
    assert res.success is True
    assert res.unexpected_count == 0


def test_unlisted_combination_conservatively_rejected():
    matrix = sm.load_matrix()
    # 表里没有的组合默认不允许（保守，不猜）
    assert sm.check_combination("Z", "unknown_family", matrix) is False


def test_half_missing_row_not_double_counted():
    matrix = sm.load_matrix()
    # 半缺由 cross_column_derived 族负责，不在矩阵里重复计数
    res = sm.evaluate_matrix([_row(1, severity_level="", category_family="safety")], matrix)
    assert res.unexpected_count == 0


def test_alias_hit_does_not_call_model(model_stub):
    aliases = {"跌倒": "AE-CAT-08"}
    d = _arbitrate_with_stub("跌倒", aliases, model_stub, _row(1))
    assert d["source"] == "alias"
    assert d["decision_code"] == "AE-CAT-08"
    assert d["needs_model"] is False
    assert d["needs_human"] is False
    assert model_stub.call_count == 0  # 调用计数断言：命中不触发模型

    d2 = _arbitrate_with_stub("完全陌生的事件名", aliases, model_stub, _row(2))
    assert model_stub.call_count == 1  # 只有未命中项才调模型
    assert d2["needs_model"] is True


def test_model_result_used_when_alias_misses(model_stub):
    model_stub.responses = [{"code": "AE-CAT-08", "confidence": 0.9, "reason": "语义归并"}]
    d = _arbitrate_with_stub("患者跌倒致骨折", {"跌倒": "AE-CAT-08"}, model_stub, _row(1))
    assert d["source"] == "model"
    assert d["decision_code"] == "AE-CAT-08"
    assert d["evidence"][0]["source"] == "model"
    assert model_stub.call_count == 1


def test_alias_and_model_conflict_keeps_both_evidence(model_stub):
    model_stub.responses = [{"code": "AE-CAT-99", "confidence": 0.82, "reason": "模型建议归到 99"}]
    hit = alias_lookup("跌倒", {"跌倒": "AE-CAT-08"})
    response = model_stub.structured_call([{"role": "user", "content": "跌倒"}], {"type": "object"})
    d = sm.arbitrate(hit, response["data"], _row(1))

    assert d["needs_human"] is True
    assert d["decision_code"] is None  # 不静默取其一
    assert d["source"] == "manual"
    assert {e["source"] for e in d["evidence"]} == {"alias", "model"}
    assert {e["code"] for e in d["evidence"]} == {"AE-CAT-08", "AE-CAT-99"}
    assert "冲突" in d["reason"]
    assert model_stub.call_count == 1


def test_alias_and_model_agree_needs_no_human(model_stub):
    model_stub.responses = [{"code": "AE-CAT-08", "confidence": 0.9, "reason": "与别名一致"}]
    hit = alias_lookup("跌倒", {"跌倒": "AE-CAT-08"})
    response = model_stub.structured_call([], {})
    d = sm.arbitrate(hit, response["data"], _row(1))
    assert d["needs_human"] is False
    assert d["source"] == "alias"
    assert len(d["evidence"]) == 2  # 双方证据都保留
    assert d["decision_code"] == "AE-CAT-08"


def test_matrix_loadable_from_catalog(catalogs_dir):
    matrix = sm.load_matrix(catalogs_dir / "severity_matrix.csv")
    assert len(matrix) >= 9 * 5
    assert all({"level", "category_family", "allowed"} <= set(r) for r in matrix)
