"""任务 36：制度原文抽取（带出处候选 / 无出处丢弃 / 并入既有套件 / 审核升版）。

测试用 tmp_path 的 rulesets 副本，绝不触碰项目内正式 catalogs/rulesets/。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ae_qc.attribution.policy_extract import (
    POLICY_SCHEMA, approve_candidates, export_review_checklist, extract_from_policy,
    merge_candidates,
)
from ae_qc.engine.ruleset import load_ruleset
from ae_qc.ledger.audit import AuditLog
from ae_qc.ledger.store import Store
from ae_qc.models import RuleSet

POLICY_TEXT = "\n".join([
    "第1条 发生时间不得为空，必须按 ISO8601 填报。",
    "第2条 跌倒又称患者跌倒致骨折，统一归入安全管理与意外伤害事件。",
    "第3条 严重程度等级限于 A 至 I。",
    "第4条 事件类别编码应填院内目录编码。",
])

VALID_PAYLOAD = {
    "rule_candidates": [
        {"id": "POL-1", "kind": "not_null", "column": "occur_time", "value_set": [],
         "basis": "§1 发生时间不得为空", "severity": "error"},
        {"id": "POL-2", "kind": "set_membership", "column": "severity_level",
         "value_set": ["A", "I"], "basis": "§3 严重程度等级限于A至I", "severity": "error"},
    ],
    "alias_candidates": [
        {"alias_text": "患者跌倒致骨折", "code": "AE-CAT-08", "basis": "§2 跌倒又称..."},
    ],
}


class _PolicyStub:
    def __init__(self, data):
        self.data = data
        self.call_count = 0
        self.schemas = []

    def structured_call(self, messages, schema, **kw):
        self.call_count += 1
        self.schemas.append(schema)
        return {"ok": True, "data": self.data, "unavailable": False, "code": "", "meta": {}}


@pytest.fixture
def policy_sandbox(tmp_path, project_root):
    official = project_root / "catalogs" / "rulesets"
    official_files = sorted(p.name for p in official.glob("*.yaml"))
    rulesets = tmp_path / "rulesets"
    rulesets.mkdir()
    for path in official.glob("*.yaml"):
        shutil.copy(path, rulesets / path.name)
    return {"rulesets": rulesets, "official": official, "official_files": official_files,
            "tmp": tmp_path}


def test_extract_produces_candidates_with_basis():
    """抽取产出期望条目候选与别名候选，且每条都带指向具体段落号的 basis。"""
    gw = _PolicyStub(VALID_PAYLOAD)
    result = extract_from_policy(POLICY_TEXT, gw)

    assert gw.call_count == 1
    assert gw.schemas[0] == POLICY_SCHEMA
    assert result["source"] == "model"
    assert result["paragraph_count"] == 4
    assert len(result["rule_candidates"]) == 2
    assert len(result["alias_candidates"]) == 1
    assert result["dropped"] == {"rule": 0, "alias": 0}
    for candidate in result["rule_candidates"] + result["alias_candidates"]:
        assert candidate["basis"] and "§" in candidate["basis"]
        assert candidate["paragraph_id"] in {"§1", "§2", "§3", "§4"}
        assert candidate["basis_text"]  # 附原文段落，供人工复核


def test_candidates_without_basis_are_dropped():
    """无出处 / 出处段落不存在的候选一律丢弃，并在 dropped 中计数。"""
    payload = {
        "rule_candidates": [
            {"id": "POL-OK", "kind": "not_null", "column": "occur_time", "value_set": [],
             "basis": "§1 有出处", "severity": "error"},
            {"id": "POL-NO", "kind": "not_null", "column": "report_time", "value_set": [],
             "basis": "", "severity": "error"},
            {"id": "POL-BAD", "kind": "not_null", "column": "dept_code", "value_set": [],
             "basis": "§99 不存在的段落", "severity": "error"},
        ],
        "alias_candidates": [
            {"alias_text": "患者跌倒致骨折", "code": "AE-CAT-08", "basis": "§2 有出处"},
            {"alias_text": "无出处别名", "code": "AE-CAT-01", "basis": ""},
        ],
    }
    result = extract_from_policy(POLICY_TEXT, _PolicyStub(payload))

    assert result["dropped"] == {"rule": 2, "alias": 1}
    assert len(result["rule_candidates"]) == 1
    assert len(result["alias_candidates"]) == 1
    assert result["rule_candidates"][0]["id"] == "POL-OK"
    assert result["alias_candidates"][0]["alias_text"] == "患者跌倒致骨折"
    for candidate in result["rule_candidates"] + result["alias_candidates"]:
        assert candidate["basis"].startswith("§")


def test_merge_into_existing_ruleset_object(policy_sandbox):
    """候选并入既有 RuleSet 对象（保留原条目 + 升版），不新建平行目录/文件。"""
    ruleset = load_ruleset("v2026-01")
    original_ids = [e.id for e in ruleset.entries]
    original_version = ruleset.version
    candidates = {
        "rule_candidates": [
            {"id": "POL-NEW", "kind": "not_null", "column": "occur_time", "value_set": [],
             "basis": "§1 有出处", "severity": "error"},
        ],
        "alias_candidates": [],
    }
    before = sorted(p.name for p in policy_sandbox["rulesets"].iterdir())
    merged = merge_candidates(ruleset, candidates)
    after = sorted(p.name for p in policy_sandbox["rulesets"].iterdir())

    assert before == after  # 纯内存并入，未落任何新文件
    assert isinstance(merged, RuleSet)
    assert merged.version != original_version
    assert len(merged.entries) == len(ruleset.entries) + 1
    assert [e.id for e in merged.entries[:len(ruleset.entries)]] == original_ids
    assert "POL-NEW" in [e.id for e in merged.entries]
    assert merged.meta["merged_candidates"]["added"] == ["POL-NEW"]
    assert any(e.basis.startswith("§1") for e in merged.entries if e.id == "POL-NEW")
    # 原对象未被就地改写
    assert ruleset.version == original_version
    assert len(ruleset.entries) == len(original_ids)


def test_review_checklist_and_approval(policy_sandbox, tmp_db):
    """导出人工审核清单；审核通过进入正式套件并升版、记审计；正式目录不变。"""
    extracted = extract_from_policy(POLICY_TEXT, _PolicyStub(VALID_PAYLOAD))
    checklist = export_review_checklist(extracted, policy_sandbox["tmp"] / "review.json")
    total = len(extracted["rule_candidates"]) + len(extracted["alias_candidates"])
    assert checklist["count"] == total
    assert total == 3
    assert Path(checklist["path"]).exists()
    for item in checklist["items"]:
        assert item["basis"] and item["paragraph_id"] and item["basis_text"]
        assert item["review_status"] == "pending"
        assert item["approver"] == ""

    ruleset = load_ruleset("v2026-01")
    store = Store(tmp_db)
    approved = approve_candidates(ruleset, extracted, "主任",
                                  rulesets_dir=policy_sandbox["rulesets"], store=store)
    assert approved["version"] != ruleset.version
    assert approved["added_entries"] == ["POL-1", "POL-2"]
    assert approved["approved_by"] == "主任"

    loaded = load_ruleset(approved["path"])
    assert loaded.version == approved["version"]
    assert len(loaded.entries) == len(ruleset.entries) + len(approved["added_entries"])
    assert all(rule_id in {e.id for e in loaded.entries} for rule_id in approved["added_entries"])

    # 未新建平行目录：原文件 + 一个新版本文件，且全部是文件
    names = sorted(p.name for p in policy_sandbox["rulesets"].iterdir())
    assert names == sorted(policy_sandbox["official_files"] + [f"{approved['version']}.yaml"])
    assert all(p.is_file() for p in policy_sandbox["rulesets"].iterdir())

    events = AuditLog(store).export()
    audit_events = [e for e in events if e["event_type"] == "policy_candidates_approved"]
    assert len(audit_events) == 1
    assert audit_events[0]["payload"]["approver"] == "主任"
    assert audit_events[0]["payload"]["version"] == approved["version"]

    assert sorted(p.name for p in policy_sandbox["official"].glob("*.yaml")) == policy_sandbox["official_files"]
    store.close()


def test_rule_baseline_without_gateway():
    """无模型时确定性基线仍能抽取带段落出处的候选。"""
    result = extract_from_policy(POLICY_TEXT)
    assert result["source"] == "rule_baseline"
    assert result["rule_candidates"]
    assert result["alias_candidates"]
    assert result["dropped"] == {"rule": 0, "alias": 0}
    for candidate in result["rule_candidates"] + result["alias_candidates"]:
        assert "§" in candidate["basis"]
        assert candidate["paragraph_id"] in {"§1", "§2", "§3", "§4"}
