"""引擎层结构化日志与每批运行摘要。

日志落 round 根目录 logs/（不写项目子目录），每行一个 JSON 对象；
日志写入失败永不中断业务（排障工具不能成为故障源）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ROUND_ROOT

LOG_DIR = ROUND_ROOT / "logs"
ERROR_LOG_PATH = LOG_DIR / "ae_qc-engine.log"

LOG_FIELDS = ("ts", "level", "batch_ref", "rule_id", "column", "code", "message", "elapsed_ms")


def log_event(level: str = "INFO", code: str = "", message: str = "", **kw: Any) -> dict[str, Any]:
    """写一行结构化日志并返回该行内容（便于测试断言字段齐备）。"""
    event: dict[str, Any] = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "level": level.upper(),
        "batch_ref": kw.pop("batch_ref", ""),
        "rule_id": kw.pop("rule_id", ""),
        "column": kw.pop("column", ""),
        "code": code,
        "message": message,
        "elapsed_ms": float(kw.pop("elapsed_ms", 0.0)),
    }
    event.update(kw)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass  # 日志不可写不阻断业务
    return event


@dataclass
class RunSummary:
    """每批运行摘要：规则数 / 越界行数 / skipped 数 / 耗时 / 套件版本 / 来源构成。"""

    batch_ref: str = ""
    rule_count: int = 0
    unexpected_rows: int = 0
    skipped_rules: int = 0
    elapsed_ms: float = 0.0
    rule_set_version: str = ""
    source_mix: dict[str, int] = field(default_factory=dict)
    started_at: str = ""
    skipped_reasons: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_run_summary(results: list[dict[str, Any]], *, batch_ref: str = "",
                      rule_set_version: str = "", source_mix: dict[str, int] | None = None,
                      elapsed_ms: float = 0.0) -> RunSummary:
    """从一批期望结果算出运行摘要；skipped 显式单列，不计入通过率分母。"""
    executed = [r for r in results if r.get("status", "executed") == "executed"]
    skipped = [r for r in results if r.get("status") == "skipped"]
    reasons: dict[str, int] = {}
    for r in skipped:
        reason = str(r.get("skip_reason", "unknown"))
        reasons[reason] = reasons.get(reason, 0) + 1
    return RunSummary(
        batch_ref=batch_ref,
        rule_count=len(results),
        unexpected_rows=sum(int(r.get("unexpected_count", 0)) for r in executed),
        skipped_rules=len(skipped),
        elapsed_ms=elapsed_ms,
        rule_set_version=rule_set_version,
        source_mix=dict(source_mix or {}),
        started_at=datetime.now().isoformat(timespec="seconds"),
        skipped_reasons=reasons,
    )


def timed() -> float:
    return time.perf_counter()


def elapsed_ms_since(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 3)


def read_events(limit: int | None = None) -> list[dict[str, Any]]:
    """读回日志行（供 doctor / 测试核验）。"""
    if not ERROR_LOG_PATH.exists():
        return []
    lines = ERROR_LOG_PATH.read_text(encoding="utf-8").splitlines()
    if limit is not None:
        lines = lines[-limit:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
