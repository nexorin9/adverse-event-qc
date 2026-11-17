"""任务 48：加载 / 空 / 错误三态文案函数均存在且非空；错误态含处置建议；
空态明确写「本批未发现越界」并含通过率与规则数；待写队列红条提示含条数。
"""
from __future__ import annotations

import pytest

from ae_qc.desktop import states


def test_three_state_text_functions_exist_and_are_non_empty():
    texts = [states.loading_text(), states.empty_text(1.0, 12),
             states.error_text("platform_unreachable")]
    for text in texts:
        assert isinstance(text, str) and text.strip()
    assert len(set(texts)) == 3                     # 三态文案互不相同


def test_loading_text_mentions_progress_and_cancel():
    text = states.loading_text()
    assert "跑批" in text and "取消" in text


def test_empty_text_says_no_violation_and_has_pass_rate_and_rule_count():
    text = states.empty_text(0.96, 18)
    assert "本批未发现越界" in text
    assert "通过率" in text and "96.0%" in text
    assert "规则" in text and "18" in text
    # 100% 且 0 条规则也如实给出，不留空白
    assert "100.0%" in states.empty_text(1.0, 0) and "0" in states.empty_text(1.0, 0)


def test_every_error_kind_has_actionable_advice():
    for kind in states.ERROR_KINDS:
        text = states.error_text(kind)
        assert text.strip()
        assert "处置建议" in text
        detail = states.error_detail(kind)
        assert detail["title"] and detail["message"] and detail["action"]
        assert detail["kind"] == kind
    # 错误码别名与未登记类型都返回带处置建议的文案
    assert "处置建议" in states.error_text("CONN_FAILED")
    assert "处置建议" in states.error_text("RULE_ASSET_MISSING")
    assert "处置建议" in states.error_text("unknown_kind")


def test_error_texts_differ_by_kind():
    texts = {kind: states.error_text(kind) for kind in states.ERROR_KINDS}
    assert len(set(texts.values())) == 3
    assert "平台" in texts["platform_unreachable"]
    assert "跳过" in texts["rule_skipped"]
    assert "降级" in texts["attribution_degraded"]


def test_pending_writeback_banner_shows_count():
    banner = states.pending_writeback_banner(3)
    assert "3" in banner and "待写" in banner and "红条" in banner
    assert "0" in states.pending_writeback_banner(0)


def test_state_text_dispatch():
    assert states.state_text("loading") == states.loading_text()
    assert states.state_text("empty", pass_rate=1.0, rule_count=7) == states.empty_text(1.0, 7)
    assert states.state_text("error", kind="rule_skipped") == states.error_text("rule_skipped")
    with pytest.raises(ValueError):
        states.state_text("bogus")
