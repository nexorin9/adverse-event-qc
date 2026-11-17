"""目录增量监听 + 平台定时轮询（任务 49）。

「桌面常驻」不是「跑一次就退出的脚本」：DirectoryWatcher 常驻监听投递目录，
发现新文件只触发一次跑批（seen 去重）；平台按可配间隔拉增量；状态（批次 /
待确认数 / 待写队列）经 ``ledger.queries`` 落台账，重启后可恢复——本模块
不直连底层存储、不出现直接查询。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable


class DirectoryWatcher:
    """投递目录增量发现 + 平台定时轮询。

    Parameters
    ----------
    inbox : 投递目录（路径不存在时按空目录处理，不抛栈）。
    callback : 发现新文件时调用一次（收新文件路径列表），即「触发一次跑批」。
    interval : 平台定时轮询间隔（秒），可配、可改。
    platform_callback : 到点的平台增量拉取回调。
    queries : LedgerQueries，状态落台账的唯一通道。
    clock : 注入的单调钟（返回 float），便于测试断言轮询间隔。
    """

    def __init__(self, inbox: str | Path, callback: Callable[[list[str]], Any] | None,
                 interval: float = 30.0, *, platform_callback: Callable[[], Any] | None = None,
                 queries: Any = None, clock: Callable[[], float] | None = None,
                 recursive: bool = True, suffix: tuple[str, ...] | None = None):
        self.inbox = Path(inbox)
        self.callback = callback
        self.interval = float(interval)
        self.platform_callback = platform_callback
        self.queries = queries
        self.clock = clock or time.monotonic
        self.recursive = recursive
        self.suffix = tuple(s.lower() for s in suffix) if suffix else None

        self._seen: set[str] = set()
        self.trigger_count = 0            # 目录增量触发跑批次数
        self.platform_poll_count = 0      # 平台轮询触发次数
        self.last_platform_poll: float | None = None
        self.batch_records: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []

    # ---- 目录增量 ----
    def _scan(self) -> list[str]:
        if not self.inbox.exists() or not self.inbox.is_dir():
            return []
        iterator = self.inbox.rglob("*") if self.recursive else self.inbox.glob("*")
        found: list[str] = []
        for path in sorted(iterator):
            if not path.is_file():
                continue
            if self.suffix and path.suffix.lower() not in self.suffix:
                continue
            found.append(str(path.resolve()))
        return found

    def poll_once(self) -> dict[str, Any]:
        """增量发现新文件：一次 poll 内多个新文件合并为「一次跑批」，不重复触发。"""
        files = self._scan()
        new_files = [f for f in files if f not in self._seen]
        for f in new_files:
            self._seen.add(f)
        result: dict[str, Any] = {
            "new_files": new_files, "triggered": False,
            "total_seen": len(self._seen), "trigger_count": self.trigger_count,
        }
        if new_files:
            out = self.callback(new_files) if callable(self.callback) else None
            self.trigger_count += 1
            result["triggered"] = True
            result["trigger_count"] = self.trigger_count
            result["callback_result"] = out
            for f in new_files:
                self.batch_records.append(self.record_state(
                    f"inbox-{Path(f).stem}", {"file": Path(f).name,
                                              "trigger_count": self.trigger_count}))
        # 平台定时轮询（与目录触发分开计数，互不影响）
        result["platform"] = self.platform_poll_once()
        return result

    # ---- 平台定时轮询（间隔可配） ----
    def set_interval(self, seconds: float) -> float:
        self.interval = float(seconds)
        return self.interval

    def platform_poll_once(self) -> dict[str, Any]:
        """按 interval 判断到点与否；未到点不触发，到点触发一次平台拉取。"""
        now = float(self.clock())
        due = (self.last_platform_poll is None
               or (now - self.last_platform_poll) >= self.interval)
        result: dict[str, Any] = {"due": due, "now": now,
                                  "interval": self.interval, "triggered": False}
        if due:
            self.last_platform_poll = now
            self.platform_poll_count += 1
            out = self.platform_callback() if callable(self.platform_callback) else None
            result.update({"triggered": True, "platform_poll_count": self.platform_poll_count,
                           "callback_result": out})
        return result

    # ---- 状态落台账（经 ledger.queries；不直连底层存储） ----
    def record_state(self, key: str, value: dict[str, Any]) -> dict[str, Any]:
        """把一条运行状态经 queries.save_batch 落到台账（summary 承载状态）。"""
        record: dict[str, Any] = {"key": key, "value": value, "persisted": False}
        if self.queries is None:
            return record
        batch_ref = f"watch-state-{key}"
        try:
            self.queries.save_batch(batch_ref, state="collected",
                                    summary={"watcher": "incremental", "key": key, **value})
            record["persisted"] = True
            record["batch_ref"] = batch_ref
        except Exception as exc:
            record["error"] = type(exc).__name__
        return record

    def pending_count(self) -> int:
        return self.queries.pending_count() if self.queries is not None else 0

    def restore_view(self) -> dict[str, Any]:
        """重启后从台账恢复「上次视图」：批次 / 待确认数 / 待写队列数。"""
        if self.queries is None:
            return {"batches": [], "pending_count": 0, "pending_writeback_count": 0}
        return {"batches": self.queries.list_batches(),
                "pending_count": self.queries.pending_count(),
                "pending_writeback_count": self.queries.pending_writeback_count()}


__all__ = ["DirectoryWatcher"]
