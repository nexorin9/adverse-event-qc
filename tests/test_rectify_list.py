"""任务 41：整改清单 Excel（行数 / basis / 科室分组 / 脱敏）。"""
from __future__ import annotations

import pytest
from openpyxl import load_workbook

from ae_qc.reporting.headers import EXPORT_HEADERS, assert_no_forbidden
from ae_qc.reporting.rectify_list import export_rectify_list

BASIS = "《医疗质量安全不良事件分级分类标准》附件2（已核原文）"


def _rows():
    rows = []
    for n in range(1, 4):
        rows.append(dict(dept_code="D01", row_no=n, column="severity_level",
                         present_value="Z", expected_value="A–I", basis=BASIS,
                         rule_id="AE-SEV-LEVEL", batch_ref="D01-2026-01"))
    rows.append(dict(dept_code="D02", row_no=8, column="severity_level",
                     present_value="Z", expected_value="A–I", basis=BASIS,
                     rule_id="AE-SEV-LEVEL", batch_ref="D02-2026-01"))
    return rows


def test_export_row_count_equals_all_unexpected_rows(tmp_path):
    """导出行数 == 全量越界行数（不截断、不另算）。"""
    rows = _rows()
    out = export_rectify_list(rows, tmp_path / "rectify.xlsx")
    assert out["row_count"] == len(rows) == 4
    wb = load_workbook(out["path"])
    ws = wb["整改清单"]
    assert [c.value for c in ws[1]] == list(EXPORT_HEADERS)
    assert ws.max_row - 1 == len(rows)
    assert out["headers"] == list(EXPORT_HEADERS)


def test_basis_column_non_empty(tmp_path):
    """依据列取规则条目 basis，逐行非空。"""
    out = export_rectify_list(_rows(), tmp_path / "rectify.xlsx")
    ws = load_workbook(out["path"]).active
    basis_idx = list(EXPORT_HEADERS).index("依据") + 1
    for r in range(2, ws.max_row + 1):
        value = ws.cell(row=r, column=basis_idx).value
        assert value
        assert BASIS in str(value)


def test_by_dept_grouping(tmp_path):
    """按科室分 sheet 可用，各 sheet 行数之和仍 == 全量越界行数。"""
    out = export_rectify_list(_rows(), tmp_path / "rectify_dept.xlsx", by_dept=True)
    assert set(out["sheets"]) == {"D01", "D02"}
    assert out["by_dept"] is True
    wb = load_workbook(out["path"])
    total = 0
    for sheet in wb.sheetnames:
        ws = wb[sheet]
        assert [c.value for c in ws[1]] == list(EXPORT_HEADERS)
        total += ws.max_row - 1
        for r in range(2, ws.max_row + 1):
            assert ws.cell(row=r, column=1).value == sheet
    assert total == len(_rows())


def test_no_patient_identifiable_fields(tmp_path):
    """导出脱敏：表头与全部单元格不含患者可识别字段与考核类字段。"""
    out = export_rectify_list(_rows(), tmp_path / "rectify.xlsx")
    assert_no_forbidden(EXPORT_HEADERS)
    wb = load_workbook(out["path"])
    blob = " ".join(str(c.value) for ws in wb for row in ws.iter_rows()
                    for c in row if c.value is not None)
    for token in ("姓名", "住院号", "诊断明细", "patient_name", "score",
                  "rank", "penalty", "绩效", "排名", "扣分"):
        assert token not in blob


@pytest.mark.parametrize("bad_field", ["score", "患者姓名", "住院号", "诊断明细"])
def test_injected_forbidden_field_rejected(tmp_path, bad_field):
    """注入禁用字段／患者可识别字段即被拒。"""
    rows = _rows()
    rows[0][bad_field] = "x"
    with pytest.raises(ValueError):
        export_rectify_list(rows, tmp_path / "bad.xlsx")
