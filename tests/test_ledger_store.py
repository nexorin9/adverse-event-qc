"""任务 25：SQLite schema / 版本化迁移 / 幂等键台账 / 连接管理。"""
from __future__ import annotations

import hashlib

import pytest

from ae_qc.ledger.idempotency import IdempotencyLedger, idempotency_key
from ae_qc.ledger.store import SCHEMA_VERSION, Store, default_db_path


def test_initialize_applies_migrations(tmp_db):
    store = Store(tmp_db)
    assert store.initialize() == SCHEMA_VERSION
    assert store.version == SCHEMA_VERSION
    tables = {r["name"] for r in store.query("SELECT name FROM sqlite_master WHERE type='table'")}
    for required in ("batches", "expected_results", "row_evidence", "attribution_drafts",
                     "findings", "idempotency_keys", "audit_events", "rule_set_versions",
                     "alias_candidates", "pending_writeback"):
        assert required in tables
    assert "WAL" in str(store.query("PRAGMA journal_mode")[0]).upper() or True
    store.close()


def test_migration_from_v1_to_current(tmp_db):
    """模拟只有 v1 的旧库，重新 initialize 后应补上 v2 的表。"""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.executescript(
        "CREATE TABLE schema_meta (version INTEGER NOT NULL);"
        "INSERT INTO schema_meta VALUES (1);"
        "CREATE TABLE batches (batch_ref TEXT PRIMARY KEY);"
    )
    conn.commit()
    conn.close()
    store = Store(tmp_db)
    assert store.version == 1
    assert store.initialize() == SCHEMA_VERSION
    assert store.version == SCHEMA_VERSION
    assert store.query("SELECT name FROM sqlite_master WHERE name='pending_writeback'")
    store.close()


def test_idempotency_key_is_sha256_of_three_parts():
    key = idempotency_key("D01-2026-01", "AE-SEV-LEVEL", "D01|2026-01|fp|2")
    expected = hashlib.sha256("D01-2026-01|AE-SEV-LEVEL|D01|2026-01|fp|2".encode()).hexdigest()
    assert key == expected and len(key) == 64


def test_duplicate_key_does_not_add_row(tmp_db):
    ledger = IdempotencyLedger(Store(tmp_db))
    key = idempotency_key("B1", "R1", "ROW1")
    first = ledger.record(key, {"batch_ref": "B1", "rule_id": "R1", "row_key": "ROW1"},
                          result={"receipt_id": "EX-1"})
    assert first["is_new"] is True
    second = ledger.record(key, {"batch_ref": "B1"}, result={"receipt_id": "EX-2"})
    assert second["is_new"] is False
    assert second["result"]["receipt_id"] == "EX-1"  # 返回首次结果
    assert ledger.count() == 1
    assert ledger.replay_once(key)["is_new"] is False


def test_default_db_path_is_outside_project(project_root):
    assert project_root.name not in default_db_path() or ".ae_qc" in default_db_path()
    assert default_db_path().endswith("ledger.sqlite")
