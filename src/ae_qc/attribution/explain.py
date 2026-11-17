"""归因草稿生成：四类判定 + 建议目标编码 + 置信度。

模型产出永不推进状态、人工确认是唯一放行口：无论模型返回什么（包括恶意写入
state=confirmed），草稿 state 恒为 attributed_pending；「确属新事件」只产出
「建议目录新增一类」，绝不自动改目录。模型不可用时由确定性规则基线兜底。
"""
from __future__ import annotations

import json
from typing import Any

from ..models import AttributionDraft
from .model_gateway import validate_schema

CAUSE_TYPES = ("spelling_drift", "standard_change", "new_event", "missing_info")

# 草稿输出固定 schema（draft_text / cause_type / suggested_code / confidence / missing_info[]）
EXPLAIN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["draft_text", "cause_type", "suggested_code", "confidence", "missing_info"],
    "properties": {
        "draft_text": {"type": "string"},
        "cause_type": {"enum": list(CAUSE_TYPES)},
        "suggested_code": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "missing_info": {"type": "array", "items": {"type": "string"}},
    },
}

_CHECK_COLUMNS = ("event_type_text", "event_type_code", "severity_level", "harm_desc", "root_cause_text")


def _build_messages(row: dict, context: dict) -> list[dict]:
    """出站只带脱敏业务字段，不携带患者可识别明细。"""
    safe = {col: str(row.get(col, "") or "") for col in
            ("event_type_text", "event_type_code", "severity_level", "severity_class", "category_family")}
    return [
        {"role": "system", "content":
            "你是院内不良事件归因草稿助手，只输出固定 JSON。四选一判定 cause_type；"
            "草稿仅为建议，不得判定已整改、不得推进任何状态、不得给诊疗建议。"},
        {"role": "user", "content": json.dumps({
            "row": safe, "cause_types": list(CAUSE_TYPES),
            "context": {k: v for k, v in context.items() if not str(k).startswith("_")},
            "instruction": "结合等级 / 类别族 / 越界情形给出归因草稿，仅输出 JSON 对象。",
        }, ensure_ascii=False, default=str)},
    ]


def _rule_baseline(row: dict, context: dict) -> dict[str, Any]:
    """模型不可用（或未注入网关）时的确定性基线：只按缺列与显式上下文判定。"""
    missing = [col for col in _CHECK_COLUMNS
               if row.get(col) is None or str(row.get(col)).strip() == ""]
    cause = str(context.get("cause_type", "")).strip()
    if cause not in CAUSE_TYPES:
        cause = "missing_info" if missing else "spelling_drift"
    text = str(context.get("draft_text", "")).strip()
    if not text:
        if cause == "missing_info":
            text = (f"该行缺少关键信息（{'、'.join(missing) or '事件经过 / 后果描述'}），"
                    "需补问后再判读；本条仅为草稿，须人工确认。")
        elif cause == "new_event":
            text = "该事件文本疑似未收录于院内目录，建议目录新增一类（仅建议，须人工审核）。"
        elif cause == "standard_change":
            text = "疑似同一事件换了口径或说法，建议核对目录版本与现行口径；本条仅为草稿，须人工确认。"
        else:
            text = "疑似拼写漂移（写错了编码或事件名），建议核对原值与目录编码；本条仅为草稿，须人工确认。"
    return {
        "draft_text": text,
        "cause_type": cause,
        "suggested_code": str(context.get("suggested_code", "") or ""),
        "confidence": float(context.get("confidence", 0.0) or 0.0),
        "missing_info": [str(x) for x in (context.get("missing_info") or missing)],
    }


def _to_draft(row_key: str, data: dict, source: str, degraded_reason: str) -> AttributionDraft:
    cause = str(data.get("cause_type", "missing_info"))
    if cause not in CAUSE_TYPES:
        cause = "missing_info"
    missing = [str(x) for x in (data.get("missing_info") or []) if str(x).strip()]
    text = str(data.get("draft_text", "") or "")
    if cause == "new_event":
        # 「确属新事件」只产出建议，目录变更须走人工审核（任务 35–36）
        text += "【建议目录新增一类；仅建议，不自动改目录，目录变更须走人工审核】"
        if not missing:
            missing = ["目录新增依据（原文出处 / 人工审核意见）"]
    if cause == "missing_info" and not missing:
        # 缺信息时 missing_info 必须非空，说明具体缺哪个信息
        missing = ["缺少可供判读的关键信息（事件经过 / 后果描述 / 等级或初步原因）"]
    # 模型产出永不推进状态、人工确认是唯一放行口：
    # 显式丢弃模型可能返回的 state / confirmed 字段，状态恒定写 attributed_pending
    return AttributionDraft(
        row_key=row_key,
        draft_text=text,
        cause_type=cause,
        suggested_code=str(data.get("suggested_code", "") or ""),
        confidence=float(data.get("confidence") or 0.0),
        missing_info=missing,
        state="attributed_pending",
        source=source,
        degraded_reason=degraded_reason,
    )


def build_draft(row: dict, gateway: Any = None, context: dict | None = None) -> AttributionDraft:
    """生成一行归因草稿；返回 AttributionDraft（state 恒 attributed_pending）。"""
    row = row if isinstance(row, dict) else {}
    ctx = dict(context or {})
    row_key = str(row.get("_row_key", row.get("row_key", "")))
    data: dict | None = None
    degraded_reason = ""
    source = "model"

    if gateway is not None:
        try:
            resp = gateway.structured_call(_build_messages(row, ctx), EXPLAIN_SCHEMA,
                                           request_id=f"explain-{row_key}")
            if isinstance(resp, dict) and resp.get("ok") and not resp.get("unavailable"):
                candidate = resp.get("data")
                if validate_schema(candidate, EXPLAIN_SCHEMA):
                    data = candidate
                else:
                    degraded_reason = "MODEL_SCHEMA_INVALID"
            else:
                degraded_reason = str((resp or {}).get("code") or "MODEL_UNAVAILABLE")
        except Exception as exc:
            degraded_reason = f"MODEL_UNAVAILABLE: {exc}"

    if data is None:
        # 规则基线兜底：模型不可达不抛栈，仍产出草稿（状态依旧待人工确认）
        data = _rule_baseline(row, ctx)
        source = "degraded" if degraded_reason else "manual"

    return _to_draft(row_key, data, source, degraded_reason)


__all__ = ["build_draft", "EXPLAIN_SCHEMA", "CAUSE_TYPES"]
