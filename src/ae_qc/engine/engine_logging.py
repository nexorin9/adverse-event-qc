"""引擎结构化日志、错误码引用与每批运行摘要（任务 21）。

业务口径
--------
* 信息科排障岗问「这批为什么少跑了几条规则」，需要的是结构化日志而不是自由文本：
  每行一个 JSON 对象，字段 ``ts/level/batch_ref/rule_id/column/code/message/
  elapsed_ms`` 齐备，可被 ``json.loads`` 解析。
* 错误码直接复用 ``ae_qc.errors.ERROR_CODES``（每码带 layer/desc/action），
  日志写入复用 ``ae_qc.logging_util.log_event``（落 round 根 logs/，写失败不中断业务）。
* 日志会被并行进程追加到同一文件，因此本模块不做「全局日志行数」假设；调用方
  只按自身 ``batch_ref`` 过滤自身事件。
* 每批运行摘要对象（规则数 / 越界行数 / skipped 数 / 耗时 / 套件版本 / 来源构成）
  可被 CLI / 界面 / 证据包复用，数字来自同一批 results。
"""
from __future__ import annotations

from typing import Any

from .. import errors as _errors
from ..logging_util import (
    ERROR_LOG_PATH,
    LOG_FIELDS,
    RunSummary,
    build_run_summary,
    elapsed_ms_since,
    log_event,
    read_events,
    timed,
)

# 错误码表引用：不另造一套，直接复用底座（每码含 layer/desc/action）。
ERROR_CODES = _errors.ERROR_CODES


def code_catalog() -> dict[str, dict[str, str]]:
    """返回错误码表副本，供 doctor / 测试核验每个码的说明与处置建议。"""
    return {code: dict(meta) for code, meta in ERROR_CODES.items()}


def describe_code(code: str) -> dict[str, str]:
    """复用底座 describe：未知码也有显式兜底说明。"""
    return _errors.describe(code)


def _infer_version(results: list[dict]) -> str:
    for r in results:
        version = str(r.get("rule_set_version", "") or "")
        if version:
            return version
    return ""


def _result_elapsed(results: list[dict]) -> float:
    return round(sum(float(r.get("elapsed_ms", 0) or 0) for r in results), 3)


def run_summary(results: list[dict], *, batch_ref: str = "",
                rule_set_version: str = "", source_mix: dict[str, int] | None = None,
                elapsed_ms: float = 0.0) -> dict:
    """每批运行摘要（dict 形态）：规则数 / 越界行数 / skipped 数 / 耗时 /
    套件版本 / 来源构成。数字直接由底座 build_run_summary 计算，不另起口径。
    """
    results = list(results or [])
    version = rule_set_version or _infer_version(results)
    summary = build_run_summary(
        results,
        batch_ref=batch_ref,
        rule_set_version=version,
        source_mix=source_mix,
        elapsed_ms=elapsed_ms,
    )
    return summary.to_dict()


def _code_for(result: dict) -> str:
    """日志 code 字段：skipped 用表内错误码；其余取结果显式携带的合法码。"""
    if str(result.get("status", "executed")) == "skipped":
        return "RULE_EXEC_FAILED"
    explicit = result.get("code") or result.get("error_code")
    if explicit and str(explicit) in ERROR_CODES:
        return str(explicit)
    return ""


def log_run(results: list[dict], batch_ref: str, *,
            rule_set_version: str = "", source_mix: dict[str, int] | None = None,
            elapsed_ms: float | None = None) -> RunSummary:
    """写一批结构化日志并返回 ``RunSummary``。

    - 每条结果一行：skipped 记 ERROR + ``RULE_EXEC_FAILED``，失败记 WARN，
      通过记 INFO；字段含 batch_ref/rule_id/column/code/message/elapsed_ms。
    - 批次级汇总再写一行，便于按 batch_ref 找到本次运行。
    - 耗时默认取各结果 ``elapsed_ms`` 之和（规则累计耗时），可显式覆盖。
    """
    results = list(results or [])
    version = rule_set_version or _infer_version(results)
    total_elapsed = _result_elapsed(results) if elapsed_ms is None else float(elapsed_ms)
    summary = build_run_summary(
        results,
        batch_ref=batch_ref,
        rule_set_version=version,
        source_mix=source_mix,
        elapsed_ms=total_elapsed,
    )

    for r in results:
        status = str(r.get("status", "executed"))
        rule_id = str(r.get("rule_id", "") or "")
        column = str(r.get("column", "") or "")
        if status == "skipped":
            level = "ERROR"
            message = f"规则 {rule_id} 被跳过（单条隔离）：{r.get('skip_reason', '') or r.get('exception_type', '')}"
        elif not bool(r.get("success", True)):
            level = "WARN"
            message = (f"规则 {rule_id} 检出 {int(r.get('unexpected_count', 0) or 0)} "
                       f"条越界（列 {column}）")
        else:
            level = "INFO"
            message = f"规则 {rule_id} 执行通过（列 {column}）"
        log_event(
            level=level,
            code=_code_for(r),
            message=message,
            batch_ref=batch_ref,
            rule_id=rule_id,
            column=column,
            elapsed_ms=float(r.get("elapsed_ms", 0) or 0),
            kind=str(r.get("kind", "") or ""),
            success=bool(r.get("success", False)),
        )

    d = summary.to_dict()
    log_event(
        level="INFO",
        code="",
        message=(f"批次运行摘要：规则数={d['rule_count']}，越界行数={d['unexpected_rows']}，"
                 f"skipped={d['skipped_rules']}，套件版本={d['rule_set_version']}"),
        batch_ref=batch_ref,
        rule_id="",
        column="",
        elapsed_ms=total_elapsed,
        event="run_summary",
        rule_set_version=d["rule_set_version"],
        source_mix=dict(source_mix or {}),
    )
    return summary


__all__ = [
    "ERROR_CODES", "code_catalog", "describe_code", "run_summary", "log_run",
    "RunSummary", "LOG_FIELDS", "ERROR_LOG_PATH", "read_events", "timed", "elapsed_ms_since",
]
