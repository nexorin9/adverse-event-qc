"""任务 18：跨批次重复上报识别（指纹跨批次索引）。

覆盖：跨期重复命中并给历史定位；换措辞的同一事件仍命中；真正不同的事件不误判；
无历史库给出可识别的「无历史可比」说明；提示级 info 不计入错误越界数；重复索引不增行。
"""
from __future__ import annotations

from ae_qc.engine.cross_batch_duplicate import CrossBatchIndex, event_fingerprint


def _row(no: int, **overrides) -> dict:
    base = {
        "incident_id": "EXAMPLE-0001",
        "dept_code": "D01",
        "event_type_text": "跌倒",
        "event_type_code": "AE-CAT-08",
        "occur_time": "2026-01-02 10:00:00",
        "report_time": "2026-01-02 11:00:00",
        "harm_desc": "未累及患者",
        "root_cause_text": "地面湿滑",
        "reporter_role": "示例安全员",
        "severity_level": "A",
        "_row_no": no,
        "_dept_code": "D01",
        "_period": "2026-01",
        "_row_key": f"D01|2026-01|row{no}|{no}",
    }
    base.update(overrides)
    return base


def test_cross_period_duplicate_gives_history_location(tmp_path):
    idx = CrossBatchIndex(root=tmp_path / "idx")
    assert idx.index("D01-2026-01", [_row(3)]) == 1

    # 下一期同一事件：换了自由文本措辞，稳定标识不变
    current = [_row(9, harm_desc="患者跌倒后诉髋部疼痛，已拍片",
                    event_type_text="患者跌倒致骨折", root_cause_text="地面有水",
                    _period="2026-02")]
    findings = idx.check("D01-2026-02", current)
    assert len(findings) == 1
    f = findings[0]
    assert f["history_batch_ref"] == "D01-2026-01"
    assert f["history_row_no"] == 3
    assert f["history"][0]["history_batch_ref"] == "D01-2026-01"
    assert f["severity"] == "info"
    assert f["counts_as_violation"] is False
    assert f["counts_as_error"] is False
    assert f.get("unexpected_count", 0) == 0  # 提示级不计入错误越界数
    assert f["row_no"] == 9


def test_event_fingerprint_ignores_wording():
    a = _row(1)
    b = _row(2, harm_desc="完全不同的后果描述", event_type_text="完全不同的自由文本",
             root_cause_text="完全不同的原因", reporter_role="另一岗位",
             report_time="2026-01-02 23:59:59", severity_level="E")
    # 措辞 / 上报时间 / 等级都不是事件身份
    assert event_fingerprint(a) == event_fingerprint(b)
    # 身份字段变了才是另一件事
    c = _row(1, incident_id="EXAMPLE-0002")
    assert event_fingerprint(c) != event_fingerprint(a)


def test_different_event_not_false_positive(tmp_path):
    idx = CrossBatchIndex(root=tmp_path / "idx2")
    idx.index("D01-2026-01", [_row(3)])
    current = [
        _row(9, incident_id="EXAMPLE-0002", _period="2026-02"),           # 编号不同
        _row(10, occur_time="2026-01-09 10:00:00", _period="2026-02"),     # 发生时间不同
        _row(11, dept_code="D02", _dept_code="D02", _period="2026-02"),    # 科室不同
    ]
    assert idx.check("D01-2026-02", current) == []


def test_no_history_returns_explicit_note_not_silent_pass(tmp_path):
    idx = CrossBatchIndex(root=tmp_path / "empty")
    findings = idx.check("D01-2026-01", [_row(1)])
    assert isinstance(findings, list) and len(findings) == 1  # 不是空列表（不静默当作通过）
    f = findings[0]
    assert f["no_history"] is True
    assert "无历史可比" in f["message"]
    assert any("无历史" in n for n in f["notes"])
    assert len(f["notes"]) >= 2  # 带处置建议
    assert f["severity"] == "info" and f["counts_as_violation"] is False
    assert f["hits"] == 0


def test_index_is_idempotent_and_self_batch_excluded(tmp_path):
    idx = CrossBatchIndex(root=tmp_path / "idx3")
    rows = [_row(3)]
    assert idx.index("B1", rows) == 1
    assert idx.index("B1", rows) == 1  # 同批次重复索引按覆盖处理，不增行
    assert idx.batches() == ["B1"]
    # 索引里只有自身批次时，仍是「无历史可比」
    assert idx.check("B1", rows)[0]["no_history"] is True

    # 重复索引不导致命中翻倍
    findings = idx.check("B2", [_row(9, _period="2026-02")])
    assert len(findings) == 1
    assert len(findings[0]["history"]) == 1
