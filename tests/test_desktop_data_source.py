"""任务 50：界面取数与台账查询结果一致；desktop/ 无底层存储直连；
引擎层（ingest/engine/attribution/ledger/reporting/writeback）零 GUI 依赖。
"""
from __future__ import annotations

import re
from pathlib import Path

from ae_qc.desktop import data_source as ds_mod
from ae_qc.desktop.data_source import DesktopDataSource
from ae_qc.ledger.queries import LedgerQueries
from ae_qc.ledger.store import Store


def _seed(tmp_db):
    q = LedgerQueries(Store(tmp_db))
    q.save_batch("D01-2026-01", dept_code="D01", period="2026-01", source="file",
                 pass_rate=0.8, valid_rows=5)
    q.save_result("D01-2026-01", {"rule_id": "AE-SEV", "column": "severity_level",
                                  "status": "executed", "success": False,
                                  "unexpected_count": 2, "total_count": 2,
                                  "truncated_count": 0})
    for no in (2, 5):
        q.save_evidence("D01-2026-01", "AE-SEV",
                        {"row_key": f"ROW{no}", "row_no": no, "dept_code": "D01",
                         "value": "Z"})
    q.save_draft("D01-2026-01", {"row_key": "ROW2", "state": "attributed_pending",
                                  "source": "model", "draft_text": "草稿"})
    return q


def test_ui_numbers_match_ledger_queries(tmp_db):
    q = _seed(tmp_db)
    ui = DesktopDataSource(q)
    assert ui.list_batches() == q.list_batches()
    assert ui.unexpected_rows("D01-2026-01") == q.unexpected_rows("D01-2026-01")
    assert ui.unexpected_rows(rule_id="AE-SEV") == q.unexpected_rows(rule_id="AE-SEV")
    assert len(ui.unexpected_rows(dept_code="D99")) == 0
    assert ui.pending_count() == q.pending_count() == 1
    assert ui.pending_attributions()[0]["row_key"] == "ROW2"
    assert ui.pending_drafts() == ui.pending_attributions()
    assert ui.pending_writeback_count() == q.pending_writeback_count() == 0
    assert ui.batch_stats("D01-2026-01") == q.batch_stats("D01-2026-01")
    assert ui.status_numbers() == {"pending_count": 1, "pending_writeback_count": 0}


def test_actions_go_through_state_machine_and_feedback_loop(tmp_db):
    q = _seed(tmp_db)
    ui = DesktopDataSource(q)
    transition = ui.confirm_finding("F-1", "attributed_pending", "质控员", "D01-2026-01")
    assert transition["to"] == "confirmed" and transition["legal"] is True
    assert q.store.query("SELECT event_type FROM audit_events "
                         "WHERE event_type='state_transition'")
    candidate = ui.reject_attribution({"event_type_text": "跌倒", "_row_key": "ROW2"},
                                      "AE-CAT-08", "质控员", "D01-2026-01")
    assert candidate["action"] == "reject" and candidate["status"] == "rejected"
    assert len(q.store.query("SELECT * FROM alias_candidates WHERE action='reject'")) == 1


def test_desktop_has_no_sqlite3_or_direct_sql():
    desktop_dir = Path(ds_mod.__file__).resolve().parent
    files = sorted(desktop_dir.glob("*.py")) + sorted((desktop_dir / "views").glob("*.py"))
    assert len(files) >= 9
    # 精确匹配真实 SQL 语句，避免误伤 on_select / selection 等标识符
    sql_pattern = re.compile(
        r"\b(?:SELECT\s+.+?\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|"
        r"DELETE\s+FROM|CREATE\s+TABLE)\b", re.IGNORECASE)
    for f in files:
        src = f.read_text(encoding="utf-8")
        assert "sqlite" not in src.lower(), f"{f.name} 含标准库存储模块字样"
        assert not sql_pattern.search(src), f"{f.name} 含直接 SQL 语句"
        assert ".connect(" not in src and '.execute("' not in src
        # 顶层不得直接 import 标准库存储模块
        assert not re.search(r"^import sqlite3", src, flags=re.MULTILINE)


def test_engine_layers_have_no_tkinter_import():
    ae_qc_dir = Path(ds_mod.__file__).resolve().parent.parent
    layers = ("engine", "ingest", "attribution", "ledger", "reporting", "writeback")
    checked = 0
    for layer in layers:
        for f in sorted((ae_qc_dir / layer).glob("*.py")):
            text = f.read_text(encoding="utf-8")
            assert "tkinter" not in text, f"{layer}/{f.name} 出现 GUI 依赖"
            assert not re.search(r"^\s*(import tkinter|from tkinter)", text,
                                 flags=re.MULTILINE)
            checked += 1
    assert checked >= 20


def test_build_app_refresh_uses_desktop_data_source(tmp_db):
    from ae_qc.desktop.app import build_app
    q = _seed(tmp_db)
    ui = DesktopDataSource(q)
    app = build_app(root=None, data_source=ui)
    result = app["refresh"]()
    assert result["batches"] == 1
    assert result["rows"] == 2
    assert result["pending_count"] == 1
    assert app["data_source"] is ui
    assert app["left"].bars and app["center"].table.inserted
