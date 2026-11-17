"""制度原文抽取 → 期望条目候选 / 别名候选（人工审核后并入同一套件）。

模型产出永不推进状态、人工确认是唯一放行口：抽取产物一律是候选项，必须带原文
段落出处（basis 指向 §段落号），无出处一律丢弃；审核通过后才进入正式套件并升版，
审核动作记入审计。候选并入的是既有 RuleSet 对象，不新建平行资产目录。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from ..engine.ruleset import RULESET_DIR
from ..ledger.audit import AuditLog
from ..ledger.store import now_iso
from ..models import RuleEntry, RuleSet
from .model_gateway import validate_schema

# 抽取响应的固定 schema：每条候选必须带 basis（段落出处）
POLICY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["rule_candidates", "alias_candidates"],
    "properties": {
        "rule_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "kind", "column", "value_set", "basis"],
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string"},
                    "column": {"type": "string"},
                    "value_set": {"type": "array", "items": {"type": "string"}},
                    "basis": {"type": "string"},
                    "severity": {"type": "string"},
                },
            },
        },
        "alias_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["alias_text", "code", "basis"],
                "properties": {
                    "alias_text": {"type": "string"},
                    "code": {"type": "string"},
                    "basis": {"type": "string"},
                },
            },
        },
    },
}

_COLUMN_HINTS = (
    ("发生时间", "occur_time"), ("上报时间", "report_time"), ("时间", "occur_time"),
    ("等级", "severity_level"), ("类别", "event_type_code"), ("编码", "event_type_code"),
    ("科室", "dept_code"), ("标识", "incident_id"), ("编号", "incident_id"),
)


def split_paragraphs(text: str) -> list[dict[str, Any]]:
    """把制度原文切成带段落号（§行号）的段落，供 basis 引用与复核。"""
    out: list[dict[str, Any]] = []
    for lineno, line in enumerate(str(text or "").splitlines(), start=1):
        stripped = line.strip()
        if stripped:
            out.append({"para_id": f"§{lineno}", "index": lineno, "text": stripped})
    return out


def _basis_valid(basis: Any, para_ids: set[str]) -> bool:
    """basis 必须指向真实存在的具体段落号，无出处一律判无效。"""
    match = re.search(r"§\s*(\d+)", str(basis or ""))
    return bool(match) and f"§{int(match.group(1))}" in para_ids


def _attach_para(candidate: dict, paragraphs: list[dict]) -> dict:
    out = dict(candidate)
    match = re.search(r"§\s*(\d+)", str(out.get("basis", "")))
    if match:
        para_id = f"§{int(match.group(1))}"
        out["paragraph_id"] = para_id
        out["basis_text"] = next((p["text"] for p in paragraphs if p["para_id"] == para_id), "")
    return out


def _rule_baseline_extract(paragraphs: list[dict]) -> dict[str, list[dict]]:
    """无模型时的确定性基线：从原文段落用关键词抽候选（仍必须带段落出处）。"""
    rules: list[dict] = []
    aliases: list[dict] = []
    for para in paragraphs:
        text = str(para["text"])
        basis = f"{para['para_id']}（原文：{text}）"
        match = re.search(r"([^\s，。；：]{2,20})(?:又称|简称|亦称|别名)[：:]?([^\s，。；：]{2,20})", text)
        if match:
            aliases.append({"alias_text": match.group(2).strip(), "code": "",
                            "basis": basis})
        if any(token in text for token in ("不得为空", "必须填", "应填", "须填", "限于", "只允许")):
            column = next((col for hint, col in _COLUMN_HINTS if hint in text), "")
            if column:
                rules.append({"id": f"POLICY-{para['index']}", "kind": "not_null",
                              "column": column, "value_set": [], "basis": basis,
                              "severity": "error"})
    return {"rule_candidates": rules, "alias_candidates": aliases}


def extract_from_policy(text: str, gateway: Any = None) -> dict:
    """从制度原文抽取候选；返回 {rule_candidates, alias_candidates, dropped, ...}。"""
    paragraphs = split_paragraphs(text)
    para_ids = {p["para_id"] for p in paragraphs}
    raw: dict[str, list[dict]] = {"rule_candidates": [], "alias_candidates": []}
    model_used = False

    if gateway is not None:
        messages = [
            {"role": "system", "content":
                "你是院内制度口径抽取助手，只输出固定 JSON。每条候选必须带 basis，"
                "且 basis 必须引用原文段落号（格式 §段落号）；无出处的条目不要输出。"},
            {"role": "user", "content": json.dumps({
                "paragraphs": [{"id": p["para_id"], "text": p["text"]} for p in paragraphs],
                "instruction": "抽取期望条目候选与别名候选，各自带 basis 指向具体段落号。",
            }, ensure_ascii=False)},
        ]
        try:
            resp = gateway.structured_call(messages, POLICY_SCHEMA, request_id="policy-extract")
            if isinstance(resp, dict) and resp.get("ok") and not resp.get("unavailable"):
                data = resp.get("data")
                if validate_schema(data, POLICY_SCHEMA):
                    raw = {
                        "rule_candidates": [dict(c) for c in data.get("rule_candidates", [])],
                        "alias_candidates": [dict(c) for c in data.get("alias_candidates", [])],
                    }
                    model_used = True
        except Exception:
            pass

    if not model_used:
        raw = _rule_baseline_extract(paragraphs)

    # 无出处候选一律丢弃（basis 必须指向真实段落号）
    rules = [_attach_para(c, paragraphs) for c in raw["rule_candidates"]
             if _basis_valid(c.get("basis"), para_ids)]
    aliases = [_attach_para(c, paragraphs) for c in raw["alias_candidates"]
               if _basis_valid(c.get("basis"), para_ids)]
    return {
        "rule_candidates": rules,
        "alias_candidates": aliases,
        "paragraph_count": len(paragraphs),
        "dropped": {
            "rule": len(raw["rule_candidates"]) - len(rules),
            "alias": len(raw["alias_candidates"]) - len(aliases),
        },
        "source": "model" if model_used else "rule_baseline",
    }


def _bump_version(version: str) -> str:
    base = str(version or "v2026-01")
    match = re.search(r"-r(\d+)$", base)
    if match:
        return f"{base[:match.start()]}-r{int(match.group(1)) + 1}"
    return f"{base}-r1"


def merge_candidates(ruleset_obj: RuleSet, candidates: dict) -> RuleSet:
    """把候选并入既有 RuleSet 对象（保留原条目 / meta），升版；不新建平行目录。"""
    merged = ruleset_obj.model_copy(deep=True)
    entries = list(merged.entries)
    existing_ids = {e.id for e in entries}
    added: list[str] = []
    skipped: list[dict] = []

    for raw in (candidates or {}).get("rule_candidates", []) or []:
        try:
            entry = RuleEntry(
                id=str(raw.get("id") or f"POLICY-{len(entries) + 1}"),
                kind=str(raw.get("kind") or "not_null"),
                column=str(raw.get("column") or "") or None,
                value_set=[str(v) for v in (raw.get("value_set") or [])],
                basis=str(raw.get("basis") or ""),
                severity=str(raw.get("severity") or "error"),
                params=dict(raw.get("params") or {}),
            )
        except Exception as exc:
            skipped.append({"id": raw.get("id"), "reason": str(exc)})
            continue
        if entry.id in existing_ids:
            skipped.append({"id": entry.id, "reason": "id 已存在，跳过（不覆盖既有条目）"})
            continue
        entries.append(entry)
        existing_ids.add(entry.id)
        added.append(entry.id)

    merged.entries = entries
    merged.version = _bump_version(ruleset_obj.version)
    meta = dict(merged.meta or {})
    meta["merged_candidates"] = {
        "added": added,
        "skipped": skipped,
        "alias_candidates": [a.get("alias_text") for a in
                             (candidates or {}).get("alias_candidates", []) or []],
        "at": now_iso(),
    }
    merged.meta = meta
    return merged


def export_review_checklist(candidates: dict, path: str | Path) -> dict:
    """导出人工审核清单（每条带段落出处原文与待审核状态）。"""
    items: list[dict] = []
    for kind, key in (("rule", "rule_candidates"), ("alias", "alias_candidates")):
        for candidate in (candidates or {}).get(key, []) or []:
            items.append({
                "type": kind,
                "id": candidate.get("id") or candidate.get("alias_text"),
                "basis": candidate.get("basis", ""),
                "paragraph_id": candidate.get("paragraph_id", ""),
                "basis_text": candidate.get("basis_text", ""),
                "payload": candidate,
                "review_status": "pending",
                "approver": "",
                "reviewed_at": "",
            })
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"items": items, "count": len(items)},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(target), "count": len(items), "items": items}


def approve_candidates(ruleset_obj: RuleSet, candidates: dict, approver: str, *,
                       rulesets_dir: str | Path | None = None, store: Any = None) -> dict:
    """审核通过 → 并入同一套件 → 落盘新版本 → 记审计（不新建平行资产目录）。

    rulesets_dir 建议显式传入（测试用 tmp_path 副本），避免触碰项目内正式套件。
    """
    merged = merge_candidates(ruleset_obj, candidates)
    target_dir = Path(rulesets_dir) if rulesets_dir else RULESET_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{merged.version}.yaml"
    payload = {
        "version": merged.version,
        "meta": merged.meta,
        "entries": [e.model_dump(mode="json", exclude_none=True) for e in merged.entries],
    }
    target.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                      encoding="utf-8")
    event = None
    if store is not None:
        event = AuditLog(store).write(
            "policy_candidates_approved", None,
            approver=str(approver), version=merged.version,
            added=merged.meta["merged_candidates"]["added"],
            alias_candidates=merged.meta["merged_candidates"]["alias_candidates"],
        )
    return {
        "ruleset": merged, "version": merged.version, "path": str(target),
        "added_entries": merged.meta["merged_candidates"]["added"],
        "audit": event, "approved_by": str(approver),
    }


__all__ = [
    "extract_from_policy", "merge_candidates", "export_review_checklist",
    "approve_candidates", "split_paragraphs", "POLICY_SCHEMA",
]
