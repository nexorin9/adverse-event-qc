"""数据获取阶梯专项测试（任务 3）：规模、脏数据形态、脱敏、最窄切片、资产与 fixture。

- 生成 ≥500 行脱敏样例；
- 每类脏数据形态专项断言；
- demo_minimal.sh 退出码 0 且清单行数与人工核对一致；
- cases.json 与「每条规则一正一反」边界 fixture 真实可用。
"""
from __future__ import annotations

import csv
import json
import subprocess
from pathlib import Path

import openpyxl

from ae_qc.engine.rule_kinds import evaluate
from ae_qc.ingest.xlsx_reader import read_xlsx
from ae_qc.models import RuleEntry
from scripts.gen_sample_batches import generate_batches

FORBIDDEN_TOKENS = ("住院号", "姓名", "诊断明细", "score", "rank", "penalty",
                    "排名", "扣分", "绩效", "罚分")


def test_generation_scale_and_inbox_layout(tmp_path):
    """10 科室 × 3 期、≥500 行，按 inbox/<dept_code>/<period>.xlsx 输出。"""
    inbox = tmp_path / "inbox"
    stats = generate_batches(inbox_dir=inbox)
    assert stats["files"] == 30
    assert stats["rows"] >= 500
    assert len(stats["depts"]) == 10
    assert len(stats["periods"]) == 3
    for dept in stats["depts"]:
        for period in stats["periods"]:
            path = inbox / dept / f"{period}.xlsx"
            assert path.is_file(), f"缺样例表 {path}"
    assert stats["dirty"]["total_rows"] == 30      # 每表一个合计行
    assert stats["dirty"]["empty_rows"] == 30      # 每表一个全空行


def test_dirty_forms_special_assertions(tmp_path):
    """合计行被识别、空值可容忍、错列名进待确认清单、时间倒挂可被规则捕获。"""
    inbox = tmp_path / "inbox"
    generate_batches(inbox_dir=inbox)
    res = read_xlsx(inbox / "D01" / "2026-01.xlsx")

    # ① 合计行被识别（且与全空行分开计数）
    assert res["skipped"]["total_rows"] == 1
    assert res["skipped"]["empty_rows"] == 1

    # ② 空值可容忍：空值行仍在有效行集，不静默丢行
    assert len(res["rows"]) == 20
    assert any(r.get("event_type_code") in (None, "") for r in res["rows"])

    # ③ 错列名进待确认清单，不猜
    assert any("错列名" in p["header"] for p in res["pending_columns"])
    assert res["skipped"]["bad_columns"] >= 1

    # ④ 跨期时间倒挂可被 time_order 规则捕获
    entry = RuleEntry(
        id="AE-TIME-ORDER-LADDER",
        kind="time_order",
        column="occur_time",
        basis="院内不良事件报告制度·发生时间不得晚于上报时间",
        params={"left": "occur_time", "right": "report_time"},
    )
    result = evaluate(entry, res["rows"], {})
    assert result.unexpected_count >= 1
    assert all("row_no" in s for s in result.unexpected_samples)


def test_desensitized_placeholders_and_no_forbidden_fields(tmp_path):
    """标识一律占位；全表不含患者可识别明细与考核类字段（272 号文）。"""
    inbox = tmp_path / "inbox"
    generate_batches(inbox_dir=inbox)
    incident_ids: list[str] = []
    roles: list[str] = []
    for path in sorted(inbox.glob("*/*.xlsx")):
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        headers = [str(c) if c is not None else "" for c in rows[0]]
        for raw in rows[1:]:
            for cell in raw:
                if isinstance(cell, str):
                    for token in FORBIDDEN_TOKENS:
                        assert token not in cell, f"{path.name} 出现禁用字段 {token}"
        id_idx = headers.index("事件编号")
        role_idx = headers.index("上报人角色")
        for raw in rows[1:]:
            cells = list(raw)
            if cells[id_idx] and str(cells[id_idx]).startswith("<EXAMPLE_LOT>"):
                incident_ids.append(str(cells[id_idx]))
            if cells[role_idx]:
                roles.append(str(cells[role_idx]))
        wb.close()
    assert incident_ids, "没有任何占位标识"
    assert all(i.startswith("<EXAMPLE_LOT>") for i in incident_ids)
    assert all("示例" in r for r in roles)


def test_demo_minimal_sh_exit_zero_and_rows_match_manual_count(project_root):
    """demo_minimal.sh 退出码 0、清单非空，且清单行数与人工核对一致。"""
    sh = project_root / "scripts" / "demo_minimal.sh"
    assert sh.is_file()
    completed = subprocess.run(
        [str(sh)], cwd=str(project_root), capture_output=True, text=True, timeout=240,
    )
    assert completed.returncode == 0, completed.stderr

    lists = sorted((project_root / "out").glob("越界行清单_*.csv"))
    assert lists, "demo 未产出越界行清单"
    list_path = lists[0]
    assert "D01-2026-01" in list_path.name       # 文件名含批次号
    assert "AE-SEV-LEVEL" in list_path.name      # 文件名含规则标识

    with list_path.open("r", encoding="utf-8-sig", newline="") as fh:
        csv_rows = list(csv.reader(fh))
    header, data_rows = csv_rows[0], csv_rows[1:]
    assert header[0] == "批次号"
    assert len(data_rows) > 0

    # 人工核对：直接读原始 xlsx，数出 severity_level 取值越界（非空且不在 A–I）的明细行
    source = project_root / "out" / "demo_inbox" / "D01" / "2026-01.xlsx"
    wb = openpyxl.load_workbook(source, read_only=True, data_only=True)
    ws = wb.active
    manual = 0
    for raw in ws.iter_rows(min_row=2, values_only=True):
        cells = list(raw)
        if all(c is None or (isinstance(c, str) and c.strip() == "") for c in cells):
            continue
        first = cells[0]
        if isinstance(first, str) and any(t in first for t in ("合计", "小计", "总计")):
            continue
        level = cells[4]
        if level not in (None, "") and str(level).strip() not in list("ABCDEFGHI"):
            manual += 1
    wb.close()
    assert manual > 0
    assert len(data_rows) == manual
    assert all(r[4] == "severity_level" for r in data_rows)  # 越界列一致


def test_cases_json_all_entries_have_basis_and_confidence(project_root):
    """案例集按 domain/cases 同构格式，每条带 basis 与 confidence。"""
    payload = json.loads((project_root / "assets" / "case_set" / "cases.json").read_text(encoding="utf-8"))
    assert payload["domain"]
    assert payload["cases"]
    for case in payload["cases"]:
        assert case["id"]
        assert case["basis"].strip()
        assert case["confidence"].strip()
        assert case["expected"]


def test_rule_pairs_fixtures_each_kind_positive_negative(project_root):
    """每条规则一正一反边界 fixture 真实可用：文件名自带规则族名，正反例都跑得通。"""
    pairs_dir = project_root / "tests" / "fixtures" / "rule_pairs"
    files = sorted(pairs_dir.glob("*.json"))
    assert len(files) == 7
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert path.stem == data["kind"], f"{path.name} 文件名未带规则族名"
        entry = RuleEntry.model_validate(data["entry"])
        for side in ("positive", "negative"):
            result = evaluate(entry, data[side]["rows"], dict(data.get("context", {})))
            observed = {
                "success": result.success,
                "unexpected_count": result.unexpected_count,
                "info_findings_count": len(result.info_findings),
            }
            for key, expected in data[side]["expect"].items():
                assert observed[key] == expected, (
                    f"{path.name} {side} {key}: 期望 {expected}，实得 {observed[key]}"
                )
