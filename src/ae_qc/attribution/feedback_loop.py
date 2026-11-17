"""人工确认/驳回反馈回流 → 别名候选 → 审核生效（口径沉淀闭环）。

模型产出永不推进状态、人工确认是唯一放行口：确认 / 驳回只写别名候选表，
别名生效必须再经一次显式人工审核（review）；生效别名命中后由规则层直接判定，
不再触发模型调用。历史批次结果只读不改，别名升版不影响已落库结果。

注意：审核写入的目标目录是构造时传入的 catalogs_dir；测试必须用 tmp_path 的
catalogs 副本，绝不触碰项目内正式 catalogs/aliases.csv。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT
from ..engine.rule_kinds import alias_lookup
from ..ledger.audit import AuditLog
from ..ledger.store import Store, now_iso

ALIASES_FILE = "aliases.csv"
VERSION_FILE = "aliases_version.json"
_ALIAS_FIELDS = ("alias_text", "category_code", "note")


def _read_aliases(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(fh)]


def _write_aliases(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(_ALIAS_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: str(row.get(k, "") or "") for k in _ALIAS_FIELDS})


def _version_info(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(info, dict):
                return info
        except (OSError, ValueError):
            pass
    return {"version": 0, "history": []}


def _bump_version(path: Path, event: dict) -> dict[str, Any]:
    info = _version_info(path)
    info["version"] = int(info.get("version", 0)) + 1
    info.setdefault("history", []).append({"version": info["version"], "at": now_iso(), **event})
    info["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return info


class FeedbackLoop:
    """确认 / 驳回 → 候选表；review 审核 → aliases.csv 生效并升版。"""

    def __init__(self, catalogs_dir: str | Path | None = None, store: Any = None):
        self.catalogs_dir = Path(catalogs_dir) if catalogs_dir else Path(PROJECT_ROOT) / "catalogs"
        self.aliases_path = self.catalogs_dir / ALIASES_FILE
        self.version_path = self.catalogs_dir / VERSION_FILE
        self.store = store or Store()
        self.store.initialize()
        self.audit = AuditLog(self.store)

    # ---- 反馈回流：只写候选，不直接生效 ----
    def confirm(self, row: dict, code: str, reviewer: str, batch_ref: str = "") -> dict:
        return self._record(row, code, reviewer, batch_ref, "confirm")

    def reject(self, row: dict, code: str, reviewer: str, batch_ref: str = "",
               reason: str = "") -> dict:
        return self._record(row, code, reviewer, batch_ref, "reject", reason)

    def _record(self, row: dict, code: str, reviewer: str, batch_ref: str,
                action: str, reason: str = "") -> dict[str, Any]:
        row = row if isinstance(row, dict) else {}
        alias_text = str(row.get("alias_text") or row.get("event_type_text")
                         or row.get("text") or "").strip()
        row_key = str(row.get("_row_key") or row.get("row_key") or "")
        suggested = str(code or "").strip()
        status = "pending" if action == "confirm" else "rejected"
        candidate_id = self.store.execute(
            "INSERT INTO alias_candidates(alias_text,suggested_code,reviewer,review_time,"
            "batch_ref,row_key,action,status) VALUES (?,?,?,?,?,?,?,?)",
            (alias_text, suggested, str(reviewer or ""), now_iso(), str(batch_ref or ""),
             row_key, action, status),
        )
        event = self.audit.write(f"alias_{action}", batch_ref, candidate_id=candidate_id,
                                 alias_text=alias_text, suggested_code=suggested,
                                 row_key=row_key, reviewer=str(reviewer or ""), reason=reason)
        return {
            "candidate_id": candidate_id, "alias_text": alias_text,
            "suggested_code": suggested, "row_key": row_key,
            "reviewer": str(reviewer or ""), "batch_ref": str(batch_ref or ""),
            "action": action, "status": status, "active": False, "audit": event,
        }

    def candidates(self, action: str | None = None, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM alias_candidates WHERE 1=1"
        params: list[Any] = []
        if action:
            sql += " AND action=?"
            params.append(action)
        if status:
            sql += " AND status=?"
            params.append(status)
        return [dict(r) for r in self.store.query(sql + " ORDER BY id", params)]

    def rejections(self) -> list[dict]:
        """驳回记录可查（供后续评测模型建议准确率）。"""
        return self.candidates(action="reject")

    # ---- 显式审核：唯一让别名生效的放行口 ----
    def review(self, candidate_id: int, approver: str) -> dict[str, Any]:
        rows = self.store.query("SELECT * FROM alias_candidates WHERE id=?", (candidate_id,))
        if not rows:
            return {"ok": False, "code": "CANDIDATE_NOT_FOUND", "candidate_id": candidate_id}
        cand = dict(rows[0])
        if cand.get("action") != "confirm" or cand.get("status") in ("approved", "rejected"):
            return {"ok": False, "code": "CANDIDATE_NOT_APPROVABLE", "candidate_id": candidate_id,
                    "reason": "驳回候选或已审核候选不生效"}
        alias_text = str(cand.get("alias_text") or "").strip()
        code = str(cand.get("suggested_code") or "").strip()
        if not alias_text or not code:
            return {"ok": False, "code": "CANDIDATE_INCOMPLETE", "candidate_id": candidate_id,
                    "reason": "候选缺少别名文本或目标编码"}

        existing = [r for r in _read_aliases(self.aliases_path)
                    if str(r.get("alias_text", "")).strip() != alias_text]
        existing.append({"alias_text": alias_text, "category_code": code,
                         "note": f"人工审核生效·{approver}·{now_iso()}"})
        _write_aliases(self.aliases_path, existing)
        info = _bump_version(self.version_path, {"alias_text": alias_text,
                                                  "category_code": code,
                                                  "approver": str(approver)})
        self.store.execute(
            "UPDATE alias_candidates SET status='approved', reviewer=?, review_time=? WHERE id=?",
            (f"{cand.get('reviewer', '')}|approved:{approver}", now_iso(), candidate_id),
        )
        event = self.audit.write("alias_review_approved", cand.get("batch_ref", ""),
                                 candidate_id=candidate_id, alias_text=alias_text,
                                 suggested_code=code, approver=str(approver),
                                 version=info["version"])
        return {
            "ok": True, "candidate_id": candidate_id, "alias_text": alias_text,
            "category_code": code, "approver": str(approver),
            "version": info["version"], "active": True, "audit": event,
        }

    # ---- 生效别名查询：命中即由规则层判定，不调模型 ----
    def active_aliases(self) -> dict[str, str]:
        return {str(r["alias_text"]).strip(): str(r["category_code"]).strip()
                for r in _read_aliases(self.aliases_path) if r.get("alias_text")}

    def is_active(self, alias: str) -> bool:
        key = str(alias or "").strip()
        return bool(key) and key in self.active_aliases()

    def lookup(self, text: str) -> str | None:
        """别名表精确匹配（确定性基线）：命中返回编码，调用方据此跳过模型。"""
        return alias_lookup(text, self.active_aliases())

    def version(self) -> int:
        return int(_version_info(self.version_path).get("version", 0))


__all__ = ["FeedbackLoop"]
