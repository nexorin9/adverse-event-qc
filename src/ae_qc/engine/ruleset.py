"""期望套件：条目 schema 校验、套件加载、版本管理与条目级 diff。

套件是院内一等资产（catalogs/rulesets/*.yaml），升版留 diff、旧版本可复现历史
批次。加载期拒绝非法条目并给出行号，不允许运行期静默通过。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from ..config import PROJECT_ROOT
from ..errors import RuleError
from ..models import RuleEntry, RuleSet

RULESET_DIR = PROJECT_ROOT / "catalogs" / "rulesets"


def _entry_line(path: Path, entry_id: str) -> int:
    """在 YAML 文本里定位条目 id 的行号，用于加载期报错。"""
    try:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if entry_id and entry_id in line and ("id:" in line or "- id:" in line):
                return i
    except OSError:
        pass
    return 0


def _parse_payload(payload: dict[str, Any], path: Path) -> RuleSet:
    version = str(payload.get("version") or path.stem)
    raw_entries = payload.get("entries", [])
    entries: list[RuleEntry] = []
    for idx, raw in enumerate(raw_entries, start=1):
        try:
            entries.append(RuleEntry.model_validate(dict(raw)))
        except Exception as exc:  # pydantic ValidationError / ValueError
            line = _entry_line(path, str(raw.get("id", ""))) or idx
            raise RuleError(
                f"套件 {path.name} 第 {line} 行条目非法：{exc}",
                code="RULE_EMPTY_VALUE_SET" if "value_set" in str(exc) else "RULE_KIND_UNKNOWN",
            ) from exc
    seen: set[str] = set()
    for e in entries:
        if e.id in seen:
            raise RuleError(f"套件 {path.name} 存在重复条目 id={e.id}", code="RULE_KIND_UNKNOWN")
        seen.add(e.id)
    return RuleSet(version=version, entries=entries, meta=payload.get("meta", {}))


def load_ruleset(path_or_version: str | Path) -> RuleSet:
    """按路径或版本号加载套件（支持 .yaml/.yml/.json）。"""
    raw = str(path_or_version)
    candidate = Path(raw)
    if not candidate.is_absolute() and not candidate.exists():
        candidate = RULESET_DIR / raw
        if not candidate.suffix:
            candidate = RULESET_DIR / f"{raw}.yaml"
        if not candidate.exists():
            candidate = RULESET_DIR / f"{raw}.json"
    if not candidate.exists():
        raise RuleError(f"期望套件不存在：{raw}（目录 {RULESET_DIR}）", code="RULESET_NOT_FOUND")
    if candidate.suffix.lower() == ".json":
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    else:
        payload = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise RuleError(f"套件 {candidate.name} 顶层必须是对象", code="RULESET_NOT_FOUND")
    return _parse_payload(payload, candidate)


def list_versions() -> list[str]:
    if not RULESET_DIR.exists():
        return []
    out = []
    for path in sorted(RULESET_DIR.glob("*")):
        if path.suffix.lower() in (".yaml", ".yml", ".json"):
            out.append(path.stem)
    return out


def latest_version() -> str:
    versions = list_versions()
    if not versions:
        raise RuleError("catalogs/rulesets/ 下没有任何套件", code="RULESET_NOT_FOUND")
    return versions[-1]


def load_ruleset_for_period(period: str = "", locked: str | None = None) -> tuple[RuleSet, str]:
    """按批次期间选版本：默认最新生效版本；AE_RULE_SET 可锁定版本。

    版本生效策略写在套件 meta.effective_from（yyyy-mm）：仅取 ≤ period 的版本；
    没有生效标记的视为始终可用。locked 优先于期间推断。
    """
    if locked:
        rs = load_ruleset(locked)
        return rs, rs.version
    eligible: list[tuple[str, str]] = []
    for version in list_versions():
        rs = load_ruleset(version)
        effective_from = str(rs.meta.get("effective_from", ""))
        eligible.append((effective_from or "0000-00", version))
    eligible.sort()
    if period:
        usable = [v for start, v in eligible if start <= period]
        version = usable[-1] if usable else eligible[-1][1]
    else:
        version = eligible[-1][1]
    rs = load_ruleset(version)
    return rs, rs.version


def diff_rulesets(old: RuleSet | str | Path, new: RuleSet | str | Path) -> dict[str, list[str]]:
    """条目级 diff：新增 / 删除 / 修改（修改＝同 id 但任一字段变化）。"""
    a = load_ruleset(old) if isinstance(old, (str, Path)) else old
    b = load_ruleset(new) if isinstance(new, (str, Path)) else new
    amap, bmap = a.entry_map(), b.entry_map()
    added = sorted(set(bmap) - set(amap))
    removed = sorted(set(amap) - set(bmap))
    modified = sorted(
        rule_id for rule_id in set(amap) & set(bmap)
        if amap[rule_id].model_dump() != bmap[rule_id].model_dump()
    )
    return {"added": added, "removed": removed, "modified": modified}


def basis_snapshot(ruleset: RuleSet) -> dict[str, str]:
    """条目 basis 快照：历史结果不依赖当前套件文件。"""
    return {e.id: e.basis for e in ruleset.entries}


def bind_version(result: dict[str, Any], ruleset: RuleSet) -> dict[str, Any]:
    """把版本号与 basis 快照绑到单条期望结果上。"""
    out = dict(result)
    out["rule_set_version"] = ruleset.version
    if not out.get("basis"):
        out["basis"] = basis_snapshot(ruleset).get(out.get("rule_id", ""), "")
    out["basis_snapshot"] = basis_snapshot(ruleset)
    return out
