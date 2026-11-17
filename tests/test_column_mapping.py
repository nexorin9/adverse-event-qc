"""入站 B 列映射专项测试：正常映射、待确认清单、多列挂起、必填缺失判不可用。"""
from __future__ import annotations

from ae_qc.ingest.column_mapping import load_mapping, map_headers

STD_ZH = [
    "事件编号", "科室编码", "事件名称", "事件类别编码", "严重程度等级", "事件类别",
    "发生时间", "上报时间", "上报人角色", "后果描述", "初步原因",
]


def test_load_mapping_default_handles_bom_and_multi_candidate():
    """默认映射表可加载（带 BOM）；「事件时间」保留一对多候选。"""
    mapping = load_mapping()
    assert mapping["事件编号"] == ["incident_id"]
    assert mapping["发生时间"] == ["occur_time"]
    assert mapping["事件时间"] == ["occur_time", "report_time"]
    assert len(mapping["事件时间"]) == 2


def test_map_headers_normal_all_standard_columns():
    """标准中文表头全部一对一映射，usable=True，无待确认/挂起/缺列。"""
    out = map_headers(STD_ZH)
    assert out["usable"] is True
    assert out["missing_required"] == []
    assert out["pending"] == []
    assert out["ambiguous"] == {}
    assert out["mapped"]["发生时间"] == "occur_time"
    assert out["mapped"]["上报时间"] == "report_time"
    assert set(out["mapped"].values()) >= {
        "incident_id", "dept_code", "occur_time", "report_time",
    }


def test_unmapped_column_goes_to_pending_not_guessed():
    """未映射列进待确认清单，含表头名与出现次数，绝不猜测映射。"""
    out = map_headers(STD_ZH + ["错列名_未登记备注"])
    assert out["usable"] is True  # 必填列仍齐
    assert "错列名_未登记备注" not in out["mapped"]
    pending = [p for p in out["pending"] if p["header"] == "错列名_未登记备注"]
    assert len(pending) == 1
    assert pending[0]["count"] == 1
    assert pending[0]["candidates"] == []
    assert "不猜" in pending[0]["reason"]


def test_duplicate_unmapped_header_count():
    """重复出现的未映射表头按出现次数累计，不猜测。"""
    out = map_headers(STD_ZH + ["错列名_X", "错列名_X"])
    pending = [p for p in out["pending"] if p["header"] == "错列名_X"]
    assert len(pending) == 1
    assert pending[0]["count"] == 2


def test_ambiguous_header_suspended_with_parallel_candidates():
    """一表头映射到多列 → ambiguous 挂起并列候选，该表头不进 mapped。"""
    out = map_headers(["事件编号", "科室编码", "发生时间", "上报时间", "事件时间"])
    assert out["ambiguous"]["事件时间"] == ["occur_time", "report_time"]
    assert "事件时间" not in out["mapped"]
    pending = [p for p in out["pending"] if p["header"] == "事件时间"]
    assert len(pending) == 1
    assert pending[0]["ambiguous"] is True
    assert set(pending[0]["candidates"]) == {"occur_time", "report_time"}


def test_missing_required_columns_make_table_unusable():
    """必填标准列缺失 → usable=False 并逐个点名缺失列。"""
    out = map_headers(["事件编号", "科室编码", "事件名称"])
    assert out["usable"] is False
    assert "occur_time" in out["missing_required"]
    assert "report_time" in out["missing_required"]
    assert "dept_code" not in out["missing_required"]


def test_ambiguous_only_header_breaks_required_columns():
    """只有「事件时间」而没有明确发生/上报时间表头时，两必填时间列都判缺失。"""
    out = map_headers(["事件编号", "科室编码", "事件时间"])
    assert out["ambiguous"]["事件时间"] == ["occur_time", "report_time"]
    assert out["usable"] is False
    assert set(out["missing_required"]) == {"occur_time", "report_time"}


def test_custom_mapping_injected():
    """支持注入自定义映射（可编辑资产），不依赖默认文件。"""
    custom = {
        "院内事件号": ["incident_id"],
        "科室": ["dept_code"],
        "发生": ["occur_time"],
        "上报": ["report_time"],
    }
    out = map_headers(["院内事件号", "科室", "发生", "上报"], mapping=custom)
    assert out["usable"] is True
    assert out["mapped"]["院内事件号"] == "incident_id"
    assert out["pending"] == []


def test_empty_header_not_counted_as_pending():
    """空表头不猜测、不计入待确认列。"""
    out = map_headers(["事件编号", "科室编码", "发生时间", "上报时间", ""])
    assert all(p["header"] != "" for p in out["pending"])
    assert out["usable"] is True
