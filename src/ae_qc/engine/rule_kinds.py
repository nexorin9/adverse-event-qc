"""规则族注册表与 7 种执行器。

统一接口：evaluate(entry, rows, context) -> ExpectedResult；新增一族只需 @register
装饰，不改执行器。每族的失败样本都定位到行并保留原值、归一后值与原始行快照。
"""
from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Any, Callable

from ..errors import RuleError
from ..models import STANDARD_COLUMNS, RuleEntry, ExpectedResult
from .result import build_result

# Ⅰ↔I、Ⅱ↔E/F/G/H、Ⅲ↔B/C/D、Ⅳ↔A（已核官方附件口径）
DEFAULT_SEVERITY_CLASS_MAP = {
    "I": "Ⅰ", "E": "Ⅱ", "F": "Ⅱ", "G": "Ⅱ", "H": "Ⅱ",
    "B": "Ⅲ", "C": "Ⅲ", "D": "Ⅲ", "A": "Ⅳ",
}

DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y/%m/%d",
    "%Y年%m月%d日",
)

RULE_KINDS_IMPL: dict[str, Callable[[RuleEntry, list[dict], dict], ExpectedResult]] = {}


def register(kind: str):
    def _decorator(fn):
        RULE_KINDS_IMPL[kind] = fn
        return fn
    return _decorator


def available_kinds() -> list[str]:
    return sorted(RULE_KINDS_IMPL)


def columns_for(entry: RuleEntry) -> list[str]:
    """该规则适用的列（用于「规则 × 可用列」执行计划）。"""
    declared = entry.params.get("columns")
    if isinstance(declared, list) and declared:
        return list(declared)
    if entry.kind == "time_order":
        return [entry.params.get("left", "occur_time"), entry.params.get("right", "report_time")]
    if entry.kind == "cross_column_derived":
        return [entry.params.get("from_col", "severity_level"), entry.params.get("derives", "severity_class")]
    return [entry.column or ""]


def normalize_value(value: Any, policy: Any = None) -> Any:
    """按参数归一：trim / casefold / 全角半角（width） / 日期多格式（date）。"""
    if value is None:
        return None
    text = str(value)
    policies = [policy] if isinstance(policy, str) else (policy or [])
    for p in policies:
        if p == "trim":
            text = text.strip()
        elif p == "casefold":
            text = text.casefold()
        elif p in ("width", "fullwidth"):
            text = unicodedata.normalize("NFKC", text)
        elif p in ("date", "iso_datetime"):
            parsed = parse_datetime(text)
            if parsed is not None:
                return parsed.isoformat(sep=" ")
    return text


def parse_datetime(value: Any, formats: list[str] | None = None) -> datetime | None:
    """多候选日期/时间格式归一；无法解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in formats or DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def null_kind(value: Any) -> str | None:
    """三种空值口径：None / 空字符串 / 纯空白。"""
    if value is None:
        return "none"
    if isinstance(value, str):
        if value == "":
            return "empty"
        if value.strip() == "":
            return "blank"
    return None


def _limit(ctx: dict) -> int:
    return int(ctx.get("limit", ctx.get("sample_display_limit", 20)))


def _ratio(entry: RuleEntry, ctx: dict) -> float:
    return float(entry.params.get("allowed_unexpected_ratio", ctx.get("allowed_unexpected_ratio", 0.0)) or 0.0)


def _sample(row: dict, value: Any, expected: Any, reason: str, **extra: Any) -> dict[str, Any]:
    item = {
        "row_key": row.get("_row_key", ""),
        "row_no": int(row.get("_row_no", 0) or 0),
        "dept_code": row.get("_dept_code", row.get("dept_code", "")),
        "period": row.get("_period", ""),
        "value": value,
        "expected": expected,
        "reason": reason,
        "normalized_value": value,
        "file_name": row.get("_file_name", ""),
        "sheet_name": row.get("_sheet_name", ""),
        "raw": {k: v for k, v in row.items() if not k.startswith("_")},
    }
    item.update(extra)
    return item


def _finish(entry: RuleEntry, rows: list[dict], ctx: dict, unexpected: list[dict], **kw: Any) -> ExpectedResult:
    version = str(ctx.get("ruleset_version", ""))
    return build_result(
        entry.id, entry.column or "", unexpected,
        limit=_limit(ctx), severity=entry.severity, kind=entry.kind,
        rule_set_version=version, basis=entry.basis,
        allowed_unexpected_ratio=_ratio(entry, ctx), row_count=len(rows), **kw,
    )


@register("set_membership")
def _set_membership(entry: RuleEntry, rows: list[dict], ctx: dict) -> ExpectedResult:
    col = entry.column or ""
    norm = entry.params.get("normalize", ["trim", "width", "casefold"])
    allowed_raw = [str(v) for v in entry.value_set]
    allowed_norm = {str(normalize_value(v, norm)) for v in allowed_raw}
    null_policy = entry.params.get("null_policy", "count_as_unexpected")
    unexpected = []
    for row in rows:
        value = row.get(col)
        if null_kind(value):
            if null_policy == "allow":
                continue
            unexpected.append(_sample(row, value, allowed_raw, "空值不允许（null_policy=count_as_unexpected）"))
            continue
        normalized = normalize_value(value, norm)
        if str(normalized) not in allowed_norm:
            item = _sample(row, value, allowed_raw, "取值不在允许集合内")
            item["normalized_value"] = normalized
            unexpected.append(item)
    return _finish(entry, rows, ctx, unexpected,
                   observed_value={"allowed_set": allowed_raw, "normalized_allowed_set": sorted(allowed_norm)})


@register("not_null")
def _not_null(entry: RuleEntry, rows: list[dict], ctx: dict) -> ExpectedResult:
    col = entry.column or ""
    null_policy = entry.params.get("null_policy", "count_as_unexpected")
    counts = {"none": 0, "empty": 0, "blank": 0}
    unexpected = []
    for row in rows:
        value = row.get(col)
        kind = null_kind(value)
        if kind is None:
            continue
        counts[kind] += 1
        if null_policy == "allow":
            continue
        unexpected.append(_sample(row, value, "非空值", f"空值口径={kind}"))
    return _finish(entry, rows, ctx, unexpected, observed_value={"null_counts": counts})


@register("type_format")
def _type_format(entry: RuleEntry, rows: list[dict], ctx: dict) -> ExpectedResult:
    col = entry.column or ""
    declared = entry.params.get("type", "iso_datetime")
    formats = entry.params.get("formats")
    enum_values = entry.params.get("enum") or entry.params.get("value_set") or []
    null_policy = entry.params.get("null_policy", "count_as_unexpected")
    unexpected = []
    reasons: dict[str, int] = {}
    for row in rows:
        value = row.get(col)
        if is_empty(value):
            if null_policy == "allow":
                continue
            unexpected.append(_sample(row, value, declared, "字段为空，无法校验格式"))
            reasons["empty"] = reasons.get("empty", 0) + 1
            continue
        ok, normalized, reason = True, normalize_value(str(value), ["trim", "width"]), ""
        if declared in ("iso_datetime", "date", "datetime"):
            parsed = parse_datetime(normalized, formats)
            ok = parsed is not None
            normalized = parsed.isoformat(sep=" ") if parsed else normalized
            reason = "日期/时间格式不符合声明格式" if not ok else ""
        elif declared == "int":
            try:
                normalized = int(str(normalized).strip())
            except (TypeError, ValueError):
                ok, reason = False, "不是整数"
        elif declared == "enum":
            ok = str(normalized) in {str(v) for v in enum_values}
            reason = "不在枚举集合内" if not ok else ""
        else:
            ok, reason = False, f"未支持的声明类型 {declared}"
        if not ok:
            unexpected.append(_sample(row, value, declared, reason, normalized_value=normalized))
            reasons[reason] = reasons.get(reason, 0) + 1
    return _finish(entry, rows, ctx, unexpected,
                   observed_value={"declared_type": declared, "failure_reasons": reasons})


@register("time_order")
def _time_order(entry: RuleEntry, rows: list[dict], ctx: dict) -> ExpectedResult:
    left = entry.params.get("left", "occur_time")
    right = entry.params.get("right", "report_time")
    direction = entry.params.get("direction", "lte")
    tolerance = float(entry.params.get("tolerance_seconds", 0) or 0)
    null_policy = entry.params.get("null_policy", "count_as_unexpected")
    equal_pairs, checked = 0, 0
    unexpected = []
    for row in rows:
        lv, rv = row.get(left), row.get(right)
        if is_empty(lv) or is_empty(rv):
            if null_policy != "allow":
                unexpected.append(_sample(row, {"left": lv, "right": rv}, f"{left} ≤ {right}", "时间字段缺失"))
            continue
        ldt, rdt = parse_datetime(lv, entry.params.get("formats")), parse_datetime(rv, entry.params.get("formats"))
        if ldt is None or rdt is None:
            unexpected.append(_sample(row, {"left": lv, "right": rv}, "ISO8601", "时间格式无法解析"))
            continue
        checked += 1
        if ldt == rdt:
            equal_pairs += 1  # 并列时间视为通过，但计入观察值
            continue
        delta = (ldt - rdt).total_seconds()
        violated = delta > tolerance if direction == "lte" else delta < -tolerance
        if violated:
            item = _sample(row, {"left": lv, "right": rv}, f"{left} ≤ {right}", "发生时间晚于上报时间（时间倒挂）")
            item["observed_value"] = {left: lv, right: rv, "delta_seconds": delta}
            unexpected.append(item)
    return _finish(entry, rows, ctx, unexpected,
                   observed_value={"equal_pairs": equal_pairs, "checked_pairs": checked,
                                   "direction": direction, "tolerance_seconds": tolerance})


@register("cross_column_derived")
def _cross_column_derived(entry: RuleEntry, rows: list[dict], ctx: dict) -> ExpectedResult:
    from_col = entry.params.get("from_col", "severity_level")
    derives = entry.params.get("derives", "severity_class")
    mapping = dict(entry.params.get("mapping") or DEFAULT_SEVERITY_CLASS_MAP)
    null_policy = entry.params.get("null_policy", "count_as_unexpected")
    unexpected, both_sides = [], []
    for row in rows:
        source, actual = row.get(from_col), row.get(derives)
        if is_empty(source) or is_empty(actual):
            if null_policy != "allow":
                unexpected.append(_sample(row, {"from": source, "derived": actual}, "两侧齐全", "派生列半缺"))
            continue
        expected = mapping.get(str(source).strip())
        if expected is None:
            unexpected.append(_sample(row, {"from": source, "derived": actual}, "派生映射", "来源值无派生映射"))
            continue
        if str(actual).strip() != str(expected):
            unexpected.append(_sample(row, {"from": source, "derived": actual}, expected,
                                      "跨列派生不一致", normalized_value=expected))
            both_sides.append({from_col: source, derives: actual, "expected": expected})
    return _finish(entry, rows, ctx, unexpected,
                   observed_value={"mismatches": both_sides[: _limit(ctx)], "mapping_size": len(mapping)})


def alias_lookup(text: Any, aliases: dict[str, str], normalize: Any = None) -> str | None:
    """别名表精确匹配（规则层确定性归并）；未命中返回 None，交给模型仲裁。"""
    if text is None:
        return None
    norm = normalize or ["trim", "width", "casefold"]
    key = str(normalize_value(text, norm))
    return aliases.get(key) or aliases.get(str(text).strip())


@register("alias_resolution")
def _alias_resolution(entry: RuleEntry, rows: list[dict], ctx: dict) -> ExpectedResult:
    text_col = entry.params.get("from_col", "event_type_text")
    aliases = dict(entry.params.get("aliases") or ctx.get("aliases") or {})
    hits, unexpected = 0, []
    for row in rows:
        text = row.get(text_col)
        code = alias_lookup(text, aliases)
        if code:
            hits += 1
            row["_alias_hit"] = code  # 供仲裁：命中项不调模型
        else:
            item = _sample(row, text, "别名表命中", "未命中")
            item["normalized_value"] = normalize_value(text, ["trim", "width", "casefold"])
            item["needs_model_merge"] = True
            unexpected.append(item)
    return _finish(entry, rows, ctx, unexpected,
                   observed_value={"alias_hits": hits, "alias_table_size": len(aliases),
                                   "unmatched_note": "未命中（待模型归并），非空白值"})


@register("catalog_coverage")
def _catalog_coverage(entry: RuleEntry, rows: list[dict], ctx: dict) -> ExpectedResult:
    expected = {str(c) for c in entry.params["expected_categories"]}
    applicable = entry.params.get("applicable_depts")
    col = entry.params.get("column", entry.column or "event_type_code")
    scoped = [r for r in rows if not applicable or r.get("_dept_code", r.get("dept_code")) in applicable]
    if applicable and not scoped:
        # 不在适用范围内的科室不产生提示
        return _finish(entry, rows, ctx, [], observed_value={"out_of_scope": True, "applicable_depts": applicable},
                       info_findings=[])
    observed = {str(r.get(col)).strip() for r in scoped if r.get(col) is not None and str(r.get(col)).strip()}
    missing = sorted(expected - observed)
    findings = []
    if missing:
        findings.append({
            "rule_id": entry.id, "kind": "catalog_coverage", "severity": "info",
            "message": "本期间按目录应出现的类目缺失（提示级，不判定违规）",
            "missing_categories": missing, "observed_categories": sorted(observed),
            "dept_codes": sorted({str(r.get("_dept_code", r.get("dept_code", ""))) for r in scoped}),
        })
    # 提示不混入 unexpected_count 的错误口径
    return _finish(entry, rows, ctx, [], observed_value={"missing_count": len(missing),
                                                          "observed_count": len(observed)},
                   info_findings=findings)


def evaluate(entry: RuleEntry, rows: list[dict[str, Any]], context: dict | None = None) -> ExpectedResult:
    """执行一条规则；未知族与注入失败由执行器隔离成 skipped。"""
    ctx = dict(context or {})
    if ctx.get("_force_fail_rule") == entry.id:
        raise RuntimeError("injected rule failure for isolation test")
    if entry.kind not in RULE_KINDS_IMPL:
        raise RuleError(f"未知规则族 kind={entry.kind}", code="RULE_KIND_UNKNOWN")
    return RULE_KINDS_IMPL[entry.kind](entry, list(rows), ctx)


__all__ = [
    "RULE_KINDS_IMPL", "register", "evaluate", "available_kinds", "columns_for",
    "normalize_value", "parse_datetime", "is_empty", "null_kind", "alias_lookup",
    "DEFAULT_SEVERITY_CLASS_MAP", "DATE_FORMATS", "STANDARD_COLUMNS",
]
