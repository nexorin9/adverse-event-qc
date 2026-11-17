"""台账只读查询层：界面与导出共用，避免各处自己拼 SQL 造成口径不一致。

归档批次只读；行级明细按配置周期清理但审计与批次汇总长期保留；读出层在
结果返回前裁掉患者可识别字段（姓名 / 住院号 / 诊断明细）。
"""
from __future__ import annotations

from typing import Any

from .audit import scrub
from .store import Store, dumps, loads, now_iso


class LedgerQueries:
    def __init__(self, store: Store):
        self.store = store
        self.store.initialize()

    # ---- 写入辅助（写入前检查归档只读；界面不直连 SQLite） ----
    def save_batch(self, batch_ref: str, *, dept_code: str = "", period: str = "",
                   source: str = "", rule_set_version: str = "", pass_rate: float = 0.0,
                   valid_rows: int = 0, summary: dict | None = None,
                   state: str = "collected") -> None:
        self.assert_writable(batch_ref)
        self.store.execute(
            "INSERT OR REPLACE INTO batches(batch_ref,dept_code,period,source,rule_set_version,"
            "pass_rate,valid_rows,state,summary_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (batch_ref, dept_code, period, source, rule_set_version, pass_rate,
             valid_rows, state, dumps(summary or {}), now_iso()),
        )

    def save_result(self, batch_ref: str, result: dict[str, Any]) -> None:
        self.assert_writable(batch_ref)
        self.store.execute(
            "INSERT INTO expected_results(batch_ref,rule_id,column,status,success,unexpected_count,"
            "total_count,truncated_count,rule_set_version,basis,result_json,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch_ref, result.get("rule_id", ""), result.get("column", ""),
             result.get("status", "executed"), 1 if result.get("success") else 0,
             int(result.get("unexpected_count", 0)), int(result.get("total_count", 0)),
             int(result.get("truncated_count", 0)), result.get("rule_set_version", ""),
             result.get("basis", ""), dumps(result), now_iso()),
        )

    def save_evidence(self, batch_ref: str, rule_id: str, evidence: dict[str, Any]) -> None:
        self.assert_writable(batch_ref)
        self.store.execute(
            "INSERT INTO row_evidence(batch_ref,rule_id,row_key,row_no,dept_code,evidence_json,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (batch_ref, rule_id, evidence.get("row_key", ""), int(evidence.get("row_no", 0) or 0),
             evidence.get("dept_code", ""), dumps(evidence), now_iso()),
        )

    def save_draft(self, batch_ref: str, draft: dict[str, Any]) -> None:
        self.assert_writable(batch_ref)
        self.store.execute(
            "INSERT OR REPLACE INTO attribution_drafts(row_key,batch_ref,draft_json,state,source,updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (draft.get("row_key", ""), batch_ref, dumps(draft),
             draft.get("state", "attributed_pending"), draft.get("source", "model"), now_iso()),
        )

    # ---- 只读查询 ----
    def list_batches(self) -> list[dict[str, Any]]:
        rows = self.store.query("SELECT * FROM batches ORDER BY period DESC, batch_ref")
        return [{"batch_ref": r["batch_ref"], "dept_code": r["dept_code"], "period": r["period"],
                 "source": r["source"], "rule_set_version": r["rule_set_version"],
                 "pass_rate": float(r["pass_rate"] or 0), "valid_rows": int(r["valid_rows"] or 0),
                 "state": r["state"], "summary": loads(r["summary_json"], {})} for r in rows]

    def unexpected_rows(self, batch_ref: str | None = None, rule_id: str | None = None,
                        dept_code: str | None = None, row_no: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM row_evidence WHERE 1=1"
        params: list[Any] = []
        for col, value in (("batch_ref", batch_ref), ("rule_id", rule_id), ("dept_code", dept_code),
                           ("row_no", row_no)):
            if value is not None:
                sql += f" AND {col}=?"
                params.append(value)
        rows = self.store.query(sql + " ORDER BY batch_ref, rule_id, row_no", params)
        return [scrub({**loads(r["evidence_json"], {}), "batch_ref": r["batch_ref"],
                       "rule_id": r["rule_id"]}) for r in rows]

    def pending_attributions(self) -> list[dict[str, Any]]:
        rows = self.store.query(
            "SELECT * FROM attribution_drafts WHERE state IN ('attributed_pending',"
            "'pending_manual_classification') ORDER BY row_key")
        return [{"row_key": r["row_key"], "batch_ref": r["batch_ref"],
                 "state": r["state"], "source": r["source"],
                 "draft": scrub(loads(r["draft_json"], {}))} for r in rows]

    def pending_count(self) -> int:
        return len(self.pending_attributions())

    def pending_writeback_count(self) -> int:
        return int(self.store.query("SELECT COUNT(*) AS n FROM pending_writeback")[0]["n"])

    def batch_stats(self, batch_ref: str) -> dict[str, Any]:
        results = self.store.query("SELECT * FROM expected_results WHERE batch_ref=?", (batch_ref,))
        evidence = self.store.query("SELECT * FROM row_evidence WHERE batch_ref=?", (batch_ref,))
        batches = self.store.query("SELECT * FROM batches WHERE batch_ref=?", (batch_ref,))
        executed = [r for r in results if r["status"] == "executed"]
        passed = sum(1 for r in executed if r["success"])
        return {
            "batch_ref": batch_ref,
            "rule_count": len(results),
            "executed_rules": len(executed),
            "skipped_rules": sum(1 for r in results if r["status"] == "skipped"),
            "passed_rules": passed,
            "pass_rate": round(passed / len(executed), 4) if executed else 0.0,
            "unexpected_rows": len(evidence),
            "unexpected_count": sum(int(r["unexpected_count"] or 0) for r in executed),
            "state": batches[0]["state"] if batches else "",
            "period": batches[0]["period"] if batches else "",
        }

    def archive(self, batch_ref: str) -> dict[str, str]:
        self.store.execute("UPDATE batches SET state='archived' WHERE batch_ref=?", (batch_ref,))
        return {"batch_ref": batch_ref, "state": "archived"}

    def is_archived(self, batch_ref: str) -> bool:
        rows = self.store.query("SELECT state FROM batches WHERE batch_ref=?", (batch_ref,))
        return bool(rows and rows[0]["state"] == "archived")

    def assert_writable(self, batch_ref: str) -> None:
        if self.is_archived(batch_ref):
            raise ValueError(f"批次 {batch_ref} 已归档，只读，不允许写入")

    def purge_details(self, before_date: str) -> dict[str, int]:
        """按保留策略清理行级明细；审计事件与批次汇总长期保留。"""
        before = self.store.query("SELECT COUNT(*) AS n FROM row_evidence WHERE created_at < ?",
                                  (before_date,))[0]["n"]
        self.store.execute("DELETE FROM row_evidence WHERE created_at < ?", (before_date,))
        audit_left = self.store.query("SELECT COUNT(*) AS n FROM audit_events")[0]["n"]
        return {"deleted_details": int(before), "audit_events_kept": int(audit_left)}
