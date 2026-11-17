"""全量越界明细落盘与回查（截断只影响展示，不影响可查）。

业务口径 ↔ 源机制对照
----------------------
* 源机制 ``unexpected_count`` 是全量越界计数，``partial_unexpected_list`` 是展示
  样本；本任务保证「界面/整改清单显示共 N 条」中的 N 等于全量明细条数，避免
  「清单只显示 20 条，数字却写着 47 条」对不上。
* 展示层的截断（``row_locator.truncate_samples``）只作用于展示项；本模块
  ``DetailStore.write`` 收到的是 ``locate`` 展开的**全量**行级证据，按
  ``批次 / 规则`` 组织落盘，``query`` / ``count`` 始终回查全量。
* 落盘前按 ``models.FORBIDDEN_TOKENS`` 脱敏：不落姓名 / 住院号 / 诊断明细，
  也不落考核类字段，保留原值与归一后值两份以便追溯。

默认落在系统临时目录（不进项目发布目录），可由 ``root`` 显式指定。
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

from ..models import FORBIDDEN_TOKENS


def _default_root() -> Path:
    return Path(tempfile.gettempdir()) / "ae_qc_detail_store"


def _safe(name: Any) -> str:
    """把批次号 / 规则 id 安全化成目录/文件名片段（去掉路径分隔符等）。"""
    text = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name).strip())
    return text or "default"


def _scrub(obj: Any) -> Any:
    """递归剔除键命中 FORBIDDEN_TOKENS 的字段（患者可识别 + 考核类字段）。"""
    tokens = [t.lower() for t in FORBIDDEN_TOKENS]
    if isinstance(obj, dict):
        return {
            k: _scrub(v)
            for k, v in obj.items()
            if not any(tok in str(k).lower() for tok in tokens)
        }
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


class DetailStore:
    """全量行级明细存储：按 ``批次/规则`` 组织 JSON 文件。

    - ``write(batch_ref, rule_id, evidences)``：全量重写该 (批次, 规则) 的明细；
      重复写同一键不增行、不翻倍，全量条数由 evidences 决定。
    - ``query(batch_ref, rule_id, dept_code, row_no)``：按条件回查全量明细。
    - ``count(batch_ref, rule_id)``：该键下全量明细条数。
    """

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else _default_root()

    def _batch_dir(self, batch_ref: Any) -> Path:
        return self.root / _safe(batch_ref)

    def _rule_file(self, batch_ref: Any, rule_id: Any) -> Path:
        return self._batch_dir(batch_ref) / f"{_safe(rule_id)}.json"

    def write(self, batch_ref: str, rule_id: str, evidences: list[dict]) -> int:
        """全量落盘；返回写入条数。落盘前脱敏并补齐回查所需的定位字段。"""
        records: list[dict[str, Any]] = []
        for e in evidences or []:
            rec = _scrub(dict(e))
            rec.setdefault("rule_id", rule_id)
            if not rec.get("rule_id"):
                rec["rule_id"] = rule_id
            rec.setdefault("batch_ref", batch_ref)
            if not rec.get("batch_ref"):
                rec["batch_ref"] = batch_ref
            rec.setdefault("dept_code", "")
            rec.setdefault("row_no", 0)
            records.append(rec)

        target = self._rule_file(batch_ref, rule_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {"batch_ref": str(batch_ref), "rule_id": str(rule_id), "items": records}
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return len(records)

    def _candidate_files(self, batch_ref: Any, rule_id: Any) -> list[Path]:
        if batch_ref is not None:
            base = self._batch_dir(batch_ref)
            if rule_id is not None:
                f = self._rule_file(batch_ref, rule_id)
                return [f] if f.exists() else []
            return sorted(base.glob("*.json")) if base.exists() else []
        # 未指定批次：按规则名过滤扫全部批次目录
        pattern = f"{_safe(rule_id)}.json" if rule_id is not None else "*.json"
        return sorted(self.root.glob(f"*/{pattern}")) if self.root.exists() else []

    def query(
        self,
        batch_ref: str | None = None,
        rule_id: str | None = None,
        dept_code: str | None = None,
        row_no: int | None = None,
    ) -> list[dict]:
        """按批次 / 规则 / 科室 / 行号回查全量明细；多个条件按 AND 过滤。"""
        out: list[dict[str, Any]] = []
        for path in self._candidate_files(batch_ref, rule_id):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for rec in payload.get("items", []):
                if batch_ref is not None and str(rec.get("batch_ref", "")) != str(batch_ref):
                    continue
                if rule_id is not None and str(rec.get("rule_id", "")) != str(rule_id):
                    continue
                if dept_code is not None and str(rec.get("dept_code", "")) != str(dept_code):
                    continue
                if row_no is not None:
                    try:
                        if int(rec.get("row_no", -1)) != int(row_no):
                            continue
                    except (TypeError, ValueError):
                        continue
                out.append(rec)
        return out

    def count(self, batch_ref: str, rule_id: str | None = None) -> int:
        """该批次（及可选规则）下的全量明细条数，不受展示截断影响。"""
        return len(self.query(batch_ref=batch_ref, rule_id=rule_id))


__all__ = ["DetailStore"]
