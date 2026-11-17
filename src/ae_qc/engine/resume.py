"""批次级断点续跑与可重入执行（任务 23）。

业务口径
--------
* 500 行以上、几十条规则的批次跑一半崩了，从头再来很浪费。已完成计划条目的结果
  逐条落盘并标记；重跑时跳过已完成条目，从断点继续。
* 同一批次重复执行幂等：已完成的条目不再执行，越界计数不翻倍、结果列表不增行。
* 断点信息（已完成条目 / 剩余条目 / 最后失败原因）可查可清理；清理后可从头重跑。
* ``failure_at`` 用于模拟中断（int = 本次待执行序列的 0-based 位置，str = plan_id）：
  中断返回带 ``interrupted=True`` 的部分结果，不抛栈；再次调用（failure_at=None）
  从断点继续，最终完整结果与一次跑完逐条一致。

状态落在系统临时目录（不进项目发布子目录），可用 ``root`` 显式指定。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable


def _default_root() -> Path:
    return Path(tempfile.gettempdir()) / "ae_qc_resume"


def _safe(name: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(name).strip()) or "default"


def _empty_state(batch_ref: Any) -> dict[str, Any]:
    return {"batch_ref": str(batch_ref), "done": {}, "order": [], "last_failure": None}


def _items(plan: Any) -> list[dict]:
    if isinstance(plan, dict):
        return [dict(it) for it in plan.get("items", [])]
    return [dict(it) for it in (plan or [])]


def _plan_id(item: dict, index: int) -> str:
    return str(item.get("plan_id") or f"P{index:04d}")


def _should_interrupt(failure_at: Any, offset: int, plan_id: str) -> bool:
    if failure_at is None or failure_at is False:
        return False
    if isinstance(failure_at, str):
        return plan_id == failure_at
    if isinstance(failure_at, int) and not isinstance(failure_at, bool):
        return offset == failure_at
    return False


class ResumeState:
    """批次断点状态：按批次一个 JSON 文件，逐条完成即原子落盘。"""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else _default_root()

    def _path(self, batch_ref: Any) -> Path:
        return self.root / f"{_safe(batch_ref)}.json"

    def _load(self, batch_ref: Any) -> dict[str, Any]:
        path = self._path(batch_ref)
        if not path.exists():
            return _empty_state(batch_ref)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _empty_state(batch_ref)
        if not isinstance(payload, dict):
            return _empty_state(batch_ref)
        state = _empty_state(batch_ref)
        done = payload.get("done")
        if isinstance(done, dict):
            state["done"] = dict(done)
        order = payload.get("order")
        if isinstance(order, list):
            state["order"] = [str(x) for x in order]
        state["last_failure"] = payload.get("last_failure")
        return state

    def _save(self, batch_ref: Any, state: dict[str, Any]) -> None:
        path = self._path(batch_ref)
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)  # 原子替换

    def mark_done(self, batch_ref: str, plan_id: str, result: dict) -> dict:
        """标记一条计划项已完成并记录结果；同键重复标记覆盖，不增行不翻倍。"""
        state = self._load(batch_ref)
        pid = str(plan_id)
        is_new = pid not in state["done"]
        state["done"][pid] = result
        if pid not in state["order"]:
            state["order"].append(pid)
        self._save(batch_ref, state)
        return {"plan_id": pid, "is_new": is_new, "done_count": len(state["done"])}

    def remaining(self, batch_ref: str, plan_ids: list[str]) -> list[str]:
        """返回尚未完成的 plan_id（保持传入顺序）；断点信息可查。"""
        done = set(self._load(batch_ref)["done"])
        return [str(pid) for pid in plan_ids if str(pid) not in done]

    def state(self, batch_ref: str, plan_ids: list[str] | None = None) -> dict:
        """断点快照：已完成条目、剩余条目、最后失败原因，可查。"""
        st = self._load(batch_ref)
        done_ids = [p for p in st["order"] if p in st["done"]]
        for p in st["done"]:
            if p not in done_ids:
                done_ids.append(p)
        return {
            "batch_ref": str(batch_ref),
            "done": done_ids,
            "done_count": len(done_ids),
            "remaining": self.remaining(batch_ref, plan_ids) if plan_ids else [],
            "last_failure": st["last_failure"],
        }

    def clear(self, batch_ref: str) -> dict:
        """清理该批断点；清理后从头重跑。"""
        path = self._path(batch_ref)
        existed = path.exists()
        if existed:
            path.unlink()
        return {"batch_ref": str(batch_ref), "cleared": existed}

    def run_resumable(
        self,
        batch_ref: str,
        plan: Any,
        rows: list[dict],
        context: dict,
        execute_fn: Callable[[dict, list[dict], dict], dict],
        failure_at: Any = None,
    ) -> dict:
        """按计划执行，跳过已完成条目，从断点继续。

        ``execute_fn(item, rows, context) -> result_dict``：执行单条计划项的结果函数。
        每条完成立即落盘；中断或异常返回 ``interrupted=True`` 的部分结果，断点保留，
        下一次调用自动续跑。``results`` 按计划顺序汇总**全部已完成**条目。
        """
        items = _items(plan)
        plan_ids = [_plan_id(it, i) for i, it in enumerate(items)]
        st = self._load(batch_ref)
        done: dict[str, Any] = st["done"]

        pending = [(it, _plan_id(it, i)) for i, it in enumerate(items)
                   if _plan_id(it, i) not in done]
        interrupted = False
        executed_now: list[str] = []

        for offset, (item, pid) in enumerate(pending):
            if _should_interrupt(failure_at, offset, pid):
                st["last_failure"] = (
                    f"模拟中断：执行计划项 {pid} 前中断（已完成 {len(done)} 条，"
                    f"剩余 {len(pending) - offset} 条）"
                )
                self._save(batch_ref, st)
                interrupted = True
                break
            try:
                result = execute_fn(item, rows, context)
            except Exception as exc:
                st["last_failure"] = f"计划项 {pid} 执行异常：{type(exc).__name__}: {exc}"
                self._save(batch_ref, st)
                interrupted = True
                break
            done[pid] = result
            if pid not in st["order"]:
                st["order"].append(pid)
            executed_now.append(pid)
            self._save(batch_ref, st)  # 逐条落盘：断点及时可续

        results: list[dict] = []
        skipped: list[dict] = []
        for it, pid in zip(items, plan_ids):
            if pid not in done:
                continue
            r = done[pid]
            if isinstance(r, dict):
                results.append(r)
                if str(r.get("status", "")) == "skipped":
                    skipped.append(r)

        return {
            "batch_ref": str(batch_ref),
            "results": results,
            "skipped": skipped,
            "interrupted": interrupted,
            "executed_now": executed_now,
            "remaining": self.remaining(batch_ref, plan_ids),
            "summary": {
                "total_items": len(items),
                "done": len(results),
                "executed_now": len(executed_now),
                "from_checkpoint": len(done) - len(executed_now),
                "interrupted": interrupted,
                "last_failure": st["last_failure"],
            },
        }


__all__ = ["ResumeState"]
