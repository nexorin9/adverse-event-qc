"""归因批量编排：分组批量 + 限流 + 逐行按 row_key 回填 + 断点续跑。

模型产出永不推进状态、人工确认是唯一放行口：本编排只把模型结果写成
attributed_pending 草稿；失败行写成降级草稿并显式标记进重试队列，绝不整批
回滚、绝不静默跳过。批次摘要列出失败行数，实例自身保存断点供续跑。
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from ..errors import ERROR_CODES, describe
from ..ledger.queries import LedgerQueries
from ..ledger.store import Store
from .degradation import degrade_row
from .model_gateway import validate_schema

# 批量响应的固定 schema：逐行结果必须带 row_key，供按行键回填（不用下标）
BATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["row_key", "draft_text", "cause_type"],
                "properties": {
                    "row_key": {"type": "string"},
                    "draft_text": {"type": "string"},
                    "cause_type": {"type": "string"},
                    "suggested_code": {"type": "string"},
                    "confidence": {"type": "number"},
                    "missing_info": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

LENGTH_BUCKET = 40  # 按文本长度分组：每 40 字符一个桶


class AttributionOrchestrator:
    """按规则族与文本长度分组批量归因；store 为 None 时用台账默认库。

    限流由 ThreadPoolExecutor(max_workers=max_concurrency) 实现：超出的批次排队
    等待槽位，不会并发爆发。实例保存 self._done / self.retry_queue 作为断点。
    """

    def __init__(self, gateway: Any, store: Any = None, batch_size: int = 50,
                 max_concurrency: int = 4):
        self.gateway = gateway
        self.store = store or Store()
        self.batch_size = max(1, int(batch_size))
        self.max_concurrency = max(1, int(max_concurrency))
        self._done: dict[str, dict] = {}       # row_key -> 已落库草稿（续跑直接复用）
        self.retry_queue: list[dict] = []      # 失败行（原始输入 dict）
        self._lock = threading.Lock()
        self._queries: LedgerQueries | None = None

    @property
    def queries(self) -> LedgerQueries:
        if self._queries is None:
            self._queries = LedgerQueries(self.store)
        return self._queries

    def _group(self, rows: list[dict]) -> dict[tuple[str, int], list[dict]]:
        groups: dict[tuple[str, int], list[dict]] = {}
        for row in rows:
            family = str(row.get("rule_family", row.get("kind", "unknown")))
            text = str(row.get("text", row.get("event_type_text", "")) or "")
            groups.setdefault((family, len(text) // LENGTH_BUCKET), []).append(row)
        return {key: groups[key] for key in sorted(groups, key=lambda k: (str(k[0]), k[1]))}

    def _batches(self, groups: dict[tuple[str, int], list[dict]]) -> list[dict]:
        out: list[dict] = []
        for key, rows in groups.items():
            for start in range(0, len(rows), self.batch_size):
                out.append({"group": key, "rows": rows[start:start + self.batch_size]})
        return out

    def _call_batch(self, batch: dict) -> dict:
        """一次批量请求；返回 {batch, by_key, error}，永不抛栈中断其它批次。"""
        rows = batch["rows"]
        messages = [
            {"role": "system", "content":
                "你是院内不良事件归因草稿助手，批量处理多行；每行输出必须带原 row_key，"
                "仅输出固定 JSON。草稿仅为建议，不得推进任何状态。"},
            {"role": "user", "content": json.dumps({
                "rows": [{"row_key": str(r.get("row_key", "")),
                          "text": str(r.get("text", r.get("event_type_text", "")) or ""),
                          "rule_family": str(r.get("rule_family", ""))} for r in rows],
                "instruction": "逐行给出归因草稿，results 数组按 row_key 标识，不要求顺序。",
            }, ensure_ascii=False)},
        ]
        try:
            resp = self.gateway.structured_call(messages, BATCH_SCHEMA,
                                                request_id=f"batch-{batch['group'][0]}-{batch['group'][1]}")
        except Exception as exc:
            return {"batch": batch, "by_key": {}, "error": f"BATCH_FAILED: {exc}"}
        if not isinstance(resp, dict) or resp.get("unavailable") or not resp.get("ok"):
            return {"batch": batch, "by_key": {},
                    "error": str((resp or {}).get("code") or "MODEL_UNAVAILABLE")}
        data = resp.get("data")
        if not validate_schema(data, BATCH_SCHEMA):
            return {"batch": batch, "by_key": {}, "error": "MODEL_SCHEMA_INVALID"}
        by_key: dict[str, dict] = {}
        for item in data["results"]:
            if isinstance(item, dict) and item.get("row_key"):
                by_key[str(item["row_key"])] = item
        return {"batch": batch, "by_key": by_key, "error": None}

    def run(self, drafts_input: list[dict]) -> dict:
        """跑一批归因草稿；返回 {drafts, failed, summary}（drafts 按输入顺序不错位）。"""
        rows = [dict(r) for r in (drafts_input or []) if isinstance(r, dict)]
        ordered_keys = [str(r.get("row_key", "")) for r in rows]

        # 合并上次遗留的重试行，按 row_key 去重；已完成的行不再重复调用模型
        seen = set(ordered_keys)
        carry = [dict(r) for r in self.retry_queue if str(r.get("row_key", "")) not in seen]
        all_rows = rows + carry
        pending = [r for r in all_rows if str(r.get("row_key", "")) not in self._done]
        reused_rows = len(all_rows) - len(pending)

        groups = self._group(pending)
        batches = self._batches(groups)
        peak = [0]
        inflight = [0]

        def worker(batch: dict) -> dict:
            with self._lock:
                inflight[0] += 1
                peak[0] = max(peak[0], inflight[0])
            try:
                return self._call_batch(batch)
            finally:
                with self._lock:
                    inflight[0] -= 1

        outcomes: list[dict] = []
        if batches:
            with ThreadPoolExecutor(max_workers=self.max_concurrency) as pool:
                future_map = {pool.submit(worker, b): b for b in batches}
                for future in as_completed(future_map):
                    batch = future_map[future]
                    try:
                        outcomes.append(future.result())
                    except Exception as exc:
                        outcomes.append({"batch": batch, "by_key": {},
                                         "error": f"BATCH_FAILED: {exc}"})

        # 逐行按 row_key 回填（不用下标，也不依赖批次完成顺序）
        parsed: dict[str, dict] = {}
        batch_errors: dict[str, str] = {}
        for outcome in outcomes:
            for row in outcome["batch"]["rows"]:
                key = str(row.get("row_key", ""))
                if outcome["error"]:
                    batch_errors[key] = outcome["error"]
                else:
                    item = outcome["by_key"].get(key)
                    if item is None:
                        batch_errors[key] = "MODEL_SCHEMA_INVALID"  # 未按 row_key 回填即失败
                    else:
                        parsed[key] = item

        failed: list[dict] = []
        new_done: dict[str, dict] = {}
        degraded_by_key: dict[str, dict] = {}
        new_retry: list[dict] = []

        for row in pending:
            key = str(row.get("row_key", ""))
            batch_ref = str(row.get("batch_ref", "") or "")
            if key in batch_errors:
                code = batch_errors[key]
                # 自定义失败串（如 BATCH_FAILED: ...）不在错误码表内，统一归到 MODEL_UNAVAILABLE
                reason_code = code if code in ERROR_CODES else "MODEL_UNAVAILABLE"
                draft = degrade_row({"_row_key": key}, reason_code).to_dict()
                failed.append({"row_key": key, "batch_ref": batch_ref,
                               "error_code": code, "reason": describe(reason_code).get("action", "")})
                degraded_by_key[key] = draft
                new_retry.append(row)
            else:
                item = parsed[key]
                # 模型产出永不推进状态、人工确认是唯一放行口：state 恒 attributed_pending
                draft = {
                    "row_key": key,
                    "draft_text": str(item.get("draft_text", "") or ""),
                    "cause_type": str(item.get("cause_type", "missing_info") or "missing_info"),
                    "suggested_code": str(item.get("suggested_code", "") or ""),
                    "confidence": float(item.get("confidence") or 0.0),
                    "missing_info": [str(x) for x in (item.get("missing_info") or [])],
                    "state": "attributed_pending",
                    "source": "model",
                    "degraded_reason": "",
                }
                new_done[key] = draft
            # 成功行正常落库；失败行落降级草稿（便于界面显示错误态），归档只读不拖垮整组
            try:
                self.queries.save_draft(batch_ref, draft)
            except Exception:
                pass

        with self._lock:
            self._done.update(new_done)
            keep = {str(r.get("row_key", "")) for r in new_retry}
            self.retry_queue = [r for r in self.retry_queue
                                if str(r.get("row_key", "")) not in new_done
                                and str(r.get("row_key", "")) not in keep]
            self.retry_queue.extend(new_retry)

        # 按输入顺序组装，逐行按 row_key 对齐不错位
        drafts: list[dict] = []
        for key in ordered_keys:
            if key in self._done:
                drafts.append(dict(self._done[key]))
            elif key in degraded_by_key:
                drafts.append(dict(degraded_by_key[key]))
            else:
                drafts.append({"row_key": key, "state": "pending_manual_classification",
                               "source": "degraded", "degraded_reason": "NOT_PROCESSED"})

        summary = {
            "total_rows": len(rows),
            "groups": len(groups),
            "group_keys": [list(k) for k in groups],
            "batches": len(batches),
            "batch_size": self.batch_size,
            "max_concurrency": self.max_concurrency,
            "max_in_flight": peak[0],
            "queued_batches": max(0, len(batches) - self.max_concurrency),
            "succeeded_rows": len(new_done),
            "failed_rows": len(failed),
            "retry_queue_size": len(self.retry_queue),
            "reused_rows": reused_rows,
        }
        return {"drafts": drafts, "failed": failed, "summary": summary}


__all__ = ["AttributionOrchestrator", "BATCH_SCHEMA"]
