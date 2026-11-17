"""七类脏数据形态专项断言（任务 8）：每类各有独立断言，防止真实数据来时静默吞掉。

样例由 gen_sample_batches 合成生成到 tmp_path，标识一律占位，读表走真实 xlsx_reader。
"""
from __future__ import annotations

import pytest

from ae_qc.engine.rule_kinds import evaluate
from ae_qc.ingest.xlsx_reader import read_xlsx
from ae_qc.models import RuleEntry
from scripts.gen_sample_batches import generate_batches


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    root = tmp_path_factory.mktemp("dirty_inbox")
    stats = generate_batches(inbox_dir=root)
    res = read_xlsx(root / "D01" / "2026-01.xlsx")
    return {"stats": stats, "res": res, "root": root}


def test_generation_reaches_500_rows(sample):
    """10 科室 × 3 期 ≥500 行，30 张表全部落盘。"""
    assert sample["stats"]["files"] == 30
    assert sample["stats"]["rows"] >= 500
    assert len(list(sample["root"].glob("*/*.xlsx"))) == 30


def test_rare_char_not_garbled(sample):
    """生僻字（CJK 扩展区，非 BMP）读入后逐字符一致，不乱码不丢字。"""
    rows = sample["res"]["rows"]
    rare_rows = [r for r in rows if "𠮷" in str(r.get("event_type_text", ""))]
    assert len(rare_rows) == 1
    assert rare_rows[0]["event_type_text"] == "现场𠮷字标记"
    assert rare_rows[0]["_encoding_fixed"] == []  # 未被误判成乱码


def test_gbk_mojibake_restored(sample):
    """GBK/UTF-8 混编：GBK 乱码单元格按编码嗅探回退还原成中文。"""
    rows = sample["res"]["rows"]
    fixed = [r for r in rows if "event_type_text" in r.get("_encoding_fixed", [])]
    assert len(fixed) == 1
    assert fixed[0]["event_type_text"] == "跌倒"
    # 同表内其它行是正常 UTF-8 中文，证明是「混编」而非整表乱码
    normal = [r for r in rows if not r.get("_encoding_fixed")
              and str(r.get("event_type_text", "")).startswith(("跌倒", "给药", "输液", "标本",
                                                                "设备", "压疮", "转运", "药品"))]
    assert normal


def test_empty_value_tolerated_and_row_kept(sample):
    """空值可容忍：非必填列的 None/空字符串行仍留在有效行集，不被静默丢弃。"""
    rows = sample["res"]["rows"]
    empty_rows = [r for r in rows
                  if r.get("event_type_code") in (None, "") or r.get("harm_desc") in (None, "")]
    assert len(empty_rows) >= 2
    assert len(rows) == 20  # 20 条明细全在，只跳过了合计行与全空行


def test_total_row_identified(sample):
    """合计行被识别并计数，不进入校验行集。"""
    skipped = sample["res"]["skipped"]
    assert skipped["total_rows"] == 1
    assert all("合计" not in str(r.get("incident_id", "")) for r in sample["res"]["rows"])


def test_empty_row_identified(sample):
    """全空行被识别并计数。"""
    assert sample["res"]["skipped"]["empty_rows"] == 1
    assert sample["res"]["skipped"]["total"] == (
        sample["res"]["skipped"]["total_rows"] + sample["res"]["skipped"]["empty_rows"]
    )


def test_wrong_header_in_pending(sample):
    """错列名进待确认清单（含来源文件与出现次数），不猜测映射。"""
    pending = sample["res"]["pending_columns"]
    assert any("错列名" in p["header"] for p in pending)
    item = [p for p in pending if "错列名" in p["header"]][0]
    assert item["count"] == 1
    assert item["source_file"] == "2026-01.xlsx"
    assert sample["res"]["skipped"]["bad_columns"] == 1


def test_suspect_duplicate_marked_not_merged(sample):
    """疑似重复行被标记且互指行号，但不自动合并。"""
    marked = [r for r in sample["res"]["rows"] if r.get("_suspect_duplicate")]
    assert len(marked) == 2
    assert marked[0]["_duplicate_with"] == marked[1]["_row_no"]
    assert marked[1]["_duplicate_with"] == marked[0]["_row_no"]
    assert len(sample["res"]["rows"]) == 20  # 两行都还在


def test_time_inversion_caught_by_rule(sample):
    """跨期时间倒挂可被 time_order 规则捕获并定位到行。"""
    entry = RuleEntry(
        id="AE-TIME-ORDER-DIRTY",
        kind="time_order",
        column="occur_time",
        basis="院内不良事件报告制度·发生时间不得晚于上报时间",
        params={"left": "occur_time", "right": "report_time"},
    )
    result = evaluate(entry, sample["res"]["rows"], {})
    assert result.unexpected_count >= 1
    assert result.unexpected_samples[0]["row_no"] > 0


def test_long_cell_truncated_and_recorded(sample):
    """超长单元格被截断到配置上限并记录原长度。"""
    truncated = sample["res"]["truncated_cells"]
    assert len(truncated) == 1
    assert truncated[0]["original_length"] > truncated[0]["kept_length"]
    assert truncated[0]["column"] == "harm_desc"
