"""本机 SQLite 台账：schema、版本化迁移、连接管理与 WAL。

台账落在用户数据目录（默认 ~/.ae_qc/ledger.sqlite），不进项目发布目录；
换人接手靠台账，不靠某个人的 Excel。读出层不返回患者可识别明细。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

MIGRATIONS: list[tuple[int, str]] = [
    (1, """
        CREATE TABLE IF NOT EXISTS batches (
            batch_ref TEXT PRIMARY KEY, dept_code TEXT, period TEXT, source TEXT,
            rule_set_version TEXT, pass_rate REAL DEFAULT 0, valid_rows INTEGER DEFAULT 0,
            state TEXT DEFAULT 'collected', summary_json TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS expected_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT, batch_ref TEXT, rule_id TEXT, column TEXT,
            status TEXT, success INTEGER, unexpected_count INTEGER DEFAULT 0,
            total_count INTEGER DEFAULT 0, truncated_count INTEGER DEFAULT 0,
            rule_set_version TEXT, basis TEXT, result_json TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS row_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT, batch_ref TEXT, rule_id TEXT, row_key TEXT,
            row_no INTEGER, dept_code TEXT, evidence_json TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS attribution_drafts (
            row_key TEXT PRIMARY KEY, batch_ref TEXT, draft_json TEXT, state TEXT,
            source TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS findings (
            finding_id TEXT PRIMARY KEY, batch_ref TEXT, dept_code TEXT, period TEXT,
            rule_id TEXT, rule_set_version TEXT, row_key TEXT, status TEXT,
            confirmed_by TEXT, confirmed_at TEXT, body_json TEXT, idempotency_key TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            key TEXT PRIMARY KEY, batch_ref TEXT, rule_id TEXT, row_key TEXT,
            payload_json TEXT, first_result_json TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, event_type TEXT, batch_ref TEXT,
            actor TEXT, payload_json TEXT
        );
        CREATE TABLE IF NOT EXISTS rule_set_versions (
            version TEXT PRIMARY KEY, basis_snapshot_json TEXT, effective_from TEXT, created_at TEXT
        );
    """),
    (2, """
        CREATE TABLE IF NOT EXISTS alias_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, alias_text TEXT, suggested_code TEXT,
            reviewer TEXT, review_time TEXT, batch_ref TEXT, row_key TEXT,
            action TEXT, status TEXT DEFAULT 'pending'
        );
        CREATE TABLE IF NOT EXISTS pending_writeback (
            key TEXT PRIMARY KEY, finding_id TEXT, body_json TEXT, attempts INTEGER DEFAULT 0,
            enqueued_at TEXT
        );
        CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY, value_json TEXT, updated_at TEXT
        );
    """),
]


def default_db_path() -> str:
    """台账默认位置：用户数据目录（不进项目子目录）。"""
    root = Path.home() / ".ae_qc"
    root.mkdir(parents=True, exist_ok=True)
    return str(root / "ledger.sqlite")


class Store:
    """轻量连接管理；所有读写经此一处，界面层不得直连 sqlite3。"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = str(db_path) if db_path else default_db_path()
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def initialize(self) -> int:
        """按版本顺序应用迁移，返回当前 schema 版本。"""
        conn = self.connect()
        conn.execute("CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)")
        row = conn.execute("SELECT MAX(version) AS v FROM schema_meta").fetchone()
        current = int(row["v"] or 0)
        for version, sql in MIGRATIONS:
            if version > current:
                conn.executescript(sql)
                conn.execute("INSERT INTO schema_meta(version) VALUES (?)", (version,))
                current = version
        conn.commit()
        return current

    @property
    def version(self) -> int:
        conn = self.connect()
        row = conn.execute("SELECT MAX(version) AS v FROM schema_meta").fetchone()
        return int(row["v"] or 0)

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        conn = self.connect()
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return cur.lastrowid or 0

    def query(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        conn = self.connect()
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
