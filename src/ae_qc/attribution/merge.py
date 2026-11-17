"""语义归并：自由文本事件名 → 院内目录编码（模型增量，规则守门）。

模型产出永不推进状态、人工确认是唯一放行口。本模块只产出「候选归并结果」：
① 别名表精确命中（engine.rule_kinds.alias_lookup）是确定性基线，不调模型；
② 未命中行的模型输出先过固定 JSON schema，再过目录成员性复核；
③ 目录外候选一律落「无法归类·待人工指定」并说明缺哪个信息，绝不取最近类；
④ 按文本长度分组组织请求，逐行按 row_key 对齐回填，单行失败不拖垮整组。
"""
from __future__ import annotations

import json
from typing import Any

from ..engine.rule_kinds import alias_lookup
from ..errors import describe
from .model_gateway import validate_schema

# 归并请求的固定响应 schema（候选编码 + 置信度 + 理由 + 是否无法归类 + 缺失信息）
MERGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["code", "confidence", "reason", "unclassifiable", "missing_info"],
    "additionalProperties": False,
    "properties": {
        "code": {"type": "string"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "unclassifiable": {"type": "boolean"},
        "missing_info": {"type": "array", "items": {"type": "string"}},
    },
}

UNCLASSIFIABLE_LABEL = "无法归类·待人工指定"
LENGTH_BUCKET = 20  # 按文本长度分组：每 20 字符一个桶
_DEFAULT_MISSING = ["事件经过 / 后果描述 / 可归属的院内目录编码依据"]


def _category_codes(categories: Any) -> set[str]:
    """从目录资产取出合法编码集合（兼容 dict / 字符串两种元素）。"""
    codes: set[str] = set()
    for item in categories or []:
        if isinstance(item, dict):
            for key in ("code", "category_code", "event_type_code"):
                if item.get(key):
                    codes.add(str(item[key]).strip())
        elif isinstance(item, str) and item.strip():
            codes.add(item.strip())
    return codes


def _build_messages(row: dict, codes: set[str]) -> list[dict]:
    """出站请求只带脱敏的业务字段与目录编码，绝不携带患者可识别明细或原文长文本。"""
    safe = {
        "event_type_text": str(row.get("event_type_text", "") or ""),
        "severity_level": str(row.get("severity_level", "") or ""),
        "severity_class": str(row.get("severity_class", "") or ""),
        "category_family": str(row.get("category_family", "") or ""),
    }
    return [
        {"role": "system", "content":
            "你是院内不良事件目录归并助手，只输出固定 JSON。候选编码必须来自给定目录；"
            "无法判读时 unclassifiable=true 并列出缺失信息。不得输出目录外编码，不得给诊疗建议。"},
        {"role": "user", "content": json.dumps({
            "catalog_codes": sorted(codes),
            "row": safe,
            "instruction": "把自由文本事件名语义归并到目录编码，仅输出 JSON 对象。",
        }, ensure_ascii=False)},
    ]


def _unclassifiable(reason: str, missing_info: list[str] | None = None,
                    *, candidate_code: str = "", error_code: str = "") -> dict[str, Any]:
    return {
        "decision_code": None,
        "status": "unclassifiable",
        "label": UNCLASSIFIABLE_LABEL,
        "source": "manual",
        "confidence": 0.0,
        "reason": reason,
        "missing_info": [str(x) for x in (missing_info or []) if str(x).strip()] or list(_DEFAULT_MISSING),
        "needs_human": True,
        "candidate_code": candidate_code,
        "unavailable": False,
        "error_code": error_code,
    }


def _degraded(code: str, detail: str = "") -> dict[str, Any]:
    info = describe(code)
    return {
        "decision_code": None,
        "status": "degraded",
        "label": "模型不可用·待人工指定",
        "source": "degraded",
        "confidence": 0.0,
        "reason": f"{info['desc']}；处置建议：{info['action']}{('；' + detail) if detail else ''}",
        "missing_info": ["模型归并结论不可用，需人工指定目录编码"],
        "needs_human": True,
        "candidate_code": "",
        "unavailable": True,
        "error_code": code,
    }


def _missing_for_out_of_catalog(candidate: str, codes: set[str]) -> list[str]:
    return [
        f"候选编码 {candidate!r} 不在院内目录（现有 {len(codes)} 类），缺少可映射到目录的依据",
        "需人工指定应归属的目录编码，或补充事件经过 / 后果描述 / 科室口径说明",
    ]


def merge_event_names(rows: list[dict], gateway: Any, categories: Any,
                      aliases: dict[str, str] | None = None) -> list[dict]:
    """归并一批自由文本事件名；返回与 rows 等长、逐行按 row_key 对齐的结果列表。"""
    source_rows = [r for r in (rows or []) if isinstance(r, dict)]
    alias_map = dict(aliases or {})
    codes = _category_codes(categories)
    slots: list[dict | None] = [None] * len(source_rows)
    pending: list[tuple[int, str, dict]] = []

    # ① 别名表精确命中：确定性基线，直接判定，不调模型
    for idx, row in enumerate(source_rows):
        row_key = str(row.get("_row_key", row.get("row_key", "")))
        text = row.get("event_type_text", row.get("text", ""))
        base = {"row_key": row_key, "event_type_text": text}
        hit = alias_lookup(text, alias_map)
        if hit:
            slots[idx] = {
                **base, "decision_code": hit, "status": "alias_hit", "source": "alias",
                "confidence": 1.0,
                "reason": "别名表精确匹配（规则层确定性归并，不调模型）",
                "missing_info": [], "needs_human": False, "candidate_code": hit,
                "unavailable": False, "error_code": "",
            }
        else:
            pending.append((idx, row_key, row))

    # ② 未命中行按文本长度分组，同组连续请求；单行失败隔离
    groups: dict[int, list[tuple[int, str, dict]]] = {}
    for item in pending:
        text = str(item[2].get("event_type_text", item[2].get("text", "")) or "")
        groups.setdefault(len(text) // LENGTH_BUCKET, []).append(item)

    for bucket in sorted(groups):
        for idx, row_key, row in groups[bucket]:
            text = row.get("event_type_text", row.get("text", ""))
            base = {"row_key": row_key, "event_type_text": text}
            try:
                resp = gateway.structured_call(_build_messages(row, codes), MERGE_SCHEMA,
                                               request_id=f"merge-{row_key}")
                if not isinstance(resp, dict) or resp.get("unavailable") or not resp.get("ok"):
                    slots[idx] = {**base, **_degraded(str((resp or {}).get("code") or "CONN_FAILED"))}
                    continue
                data = resp.get("data")
                if not validate_schema(data, MERGE_SCHEMA):
                    # 网关已验过一次；此处再验一次，防止注入的替身绕过协议
                    slots[idx] = {**base, **_unclassifiable(
                        "模型响应不符合固定 JSON schema，已拒绝（不静默兜底）",
                        ["合规的模型结构化输出"], error_code="MODEL_SCHEMA_INVALID")}
                    continue
                candidate = str(data.get("code", "")).strip()
                if data.get("unclassifiable") or not candidate:
                    slots[idx] = {**base, **_unclassifiable(
                        str(data.get("reason", "") or "模型判定无法归类"),
                        list(data.get("missing_info") or []))}
                    continue
                # ③ 目录成员性复核（规则）：目录外候选一律拒绝，不取最近类
                if candidate not in codes:
                    slots[idx] = {**base, **_unclassifiable(
                        f"模型候选编码 {candidate} 不在院内目录，已拒绝（不取最近类）",
                        _missing_for_out_of_catalog(candidate, codes),
                        candidate_code=candidate, error_code="MODEL_CANDIDATE_NOT_IN_CATALOG")}
                    continue
                slots[idx] = {
                    **base, "decision_code": candidate, "status": "merged", "source": "model",
                    "label": "", "confidence": float(data.get("confidence") or 0.0),
                    "reason": str(data.get("reason", "") or ""), "missing_info": [],
                    "needs_human": False, "candidate_code": candidate,
                    "unavailable": False, "error_code": "",
                }
            except Exception as exc:
                # 单行失败不拖垮整组
                slots[idx] = {**base, **_degraded("MODEL_UNAVAILABLE", str(exc))}

    return [s if s is not None else {"row_key": "", "status": "degraded"} for s in slots]


__all__ = ["merge_event_names", "MERGE_SCHEMA", "UNCLASSIFIABLE_LABEL"]
