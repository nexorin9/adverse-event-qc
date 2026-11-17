"""任务 35：反馈回流（候选非直接生效 / 审核生效 / 驳回可查 / 历史不变）。

严格使用 tmp_path 的 catalogs 副本，绝不触碰项目内正式 catalogs/aliases.csv；
每个测试都断言正式文件字节内容未被改动。
"""
from __future__ import annotations

import shutil

import pytest

from ae_qc.attribution.feedback_loop import FeedbackLoop
from ae_qc.attribution.merge import merge_event_names
from ae_qc.ledger.audit import AuditLog
from ae_qc.ledger.queries import LedgerQueries
from ae_qc.ledger.store import Store


@pytest.fixture
def sandbox(tmp_path, project_root):
    catalogs = tmp_path / "catalogs"
    catalogs.mkdir()
    official = project_root / "catalogs" / "aliases.csv"
    official_bytes = official.read_bytes()
    shutil.copy(official, catalogs / "aliases.csv")
    store = Store(str(tmp_path / "ledger.sqlite"))
    return {
        "catalogs": catalogs,
        "store": store,
        "loop": FeedbackLoop(catalogs_dir=catalogs, store=store),
        "official": official,
        "official_bytes": official_bytes,
    }


def test_confirm_creates_candidate_not_active(sandbox, project_root):
    """确认只写候选表，不直接生效、不升版；正式 aliases.csv 不变。"""
    loop = sandbox["loop"]
    candidate = loop.confirm({"event_type_text": "新自由文本别名", "_row_key": "rk1"},
                             "AE-CAT-08", "质控员", "B1")
    assert candidate["status"] == "pending"
    assert candidate["active"] is False
    assert loop.is_active("新自由文本别名") is False
    assert loop.version() == 0

    rows = loop.candidates(action="confirm", status="pending")
    assert len(rows) == 1
    record = rows[0]
    for field in ("alias_text", "suggested_code", "reviewer", "review_time",
                  "batch_ref", "row_key", "action", "status"):
        assert field in record
    assert record["alias_text"] == "新自由文本别名"
    assert record["suggested_code"] == "AE-CAT-08"
    assert record["reviewer"] == "质控员"
    assert record["review_time"]
    assert record["batch_ref"] == "B1"
    assert record["row_key"] == "rk1"
    assert record["action"] == "confirm"
    assert record["status"] == "pending"

    assert "新自由文本别名" not in (sandbox["catalogs"] / "aliases.csv").read_text(encoding="utf-8-sig")
    assert sandbox["official"].read_bytes() == sandbox["official_bytes"]


def test_review_activates_alias_and_skips_model(sandbox, project_root, categories, model_stub):
    """审核后别名生效并升版；生效别名命中不再调模型；正式 aliases.csv 不变。"""
    loop = sandbox["loop"]
    candidate = loop.confirm({"event_type_text": "新自由文本别名", "_row_key": "rk1"},
                             "AE-CAT-08", "质控员", "B1")
    review = loop.review(candidate["candidate_id"], "主任")

    assert review["ok"] is True
    assert review["version"] == 1
    assert review["active"] is True
    assert loop.is_active("新自由文本别名") is True
    assert loop.lookup("新自由文本别名") == "AE-CAT-08"
    assert loop.active_aliases()["新自由文本别名"] == "AE-CAT-08"
    assert loop.version() == 1
    assert loop.candidates(status="approved")[0]["id"] == candidate["candidate_id"]

    events = AuditLog(sandbox["store"]).export()
    approved = [e for e in events if e["event_type"] == "alias_review_approved"]
    assert len(approved) == 1
    assert approved[0]["payload"]["approver"] == "主任"
    assert approved[0]["payload"]["version"] == 1

    rows = [
        {"_row_key": "h1", "event_type_text": "新自由文本别名"},
        {"_row_key": "h2", "event_type_text": "另一个未命中文本"},
    ]
    merged = merge_event_names(rows, model_stub, categories, aliases=loop.active_aliases())
    assert merged[0]["status"] == "alias_hit"
    assert merged[0]["source"] == "alias"
    assert model_stub.call_count == 1  # 命中行不调模型，只有未命中行调 1 次

    assert sandbox["official"].read_bytes() == sandbox["official_bytes"]


def test_rejection_recorded(sandbox):
    """驳回被记录且可用于评测；驳回候选即便审核也不生效。"""
    loop = sandbox["loop"]
    rejected = loop.reject({"event_type_text": "被驳回别名", "_row_key": "rk2"},
                           "AE-CAT-01", "质控员", "B1", reason="模型建议不准")
    assert rejected["status"] == "rejected"
    assert rejected["active"] is False
    assert loop.is_active("被驳回别名") is False

    rejections = loop.rejections()
    assert len(rejections) == 1
    assert rejections[0]["alias_text"] == "被驳回别名"
    assert rejections[0]["suggested_code"] == "AE-CAT-01"
    assert rejections[0]["action"] == "reject"
    assert rejections[0]["row_key"] == "rk2"

    review = loop.review(rejected["candidate_id"], "主任")
    assert review["ok"] is False
    assert review["code"] == "CANDIDATE_NOT_APPROVABLE"
    assert loop.version() == 0
    events = AuditLog(sandbox["store"]).export()
    assert any(e["event_type"] == "alias_reject" for e in events)


def test_history_batch_results_unchanged(sandbox, project_root):
    """别名升版不影响历史批次结果（批次统计与越界明细前后完全一致）。"""
    loop = sandbox["loop"]
    store = sandbox["store"]
    queries = LedgerQueries(store)
    queries.save_batch("B-HIST", dept_code="D01", period="2026-01", rule_set_version="v2026-01")
    queries.save_result("B-HIST", {
        "rule_id": "AE-SEV-LEVEL", "column": "severity_level", "status": "executed",
        "success": False, "unexpected_count": 3, "total_count": 3,
        "rule_set_version": "v2026-01",
    })
    for i in range(3):
        queries.save_evidence("B-HIST", "AE-SEV-LEVEL",
                              {"row_key": f"h{i}", "row_no": i + 1, "value": "Z"})
    before = queries.batch_stats("B-HIST")
    before_rows = queries.unexpected_rows("B-HIST")
    assert before["unexpected_rows"] == 3

    candidate = loop.confirm({"event_type_text": "历史期别名", "_row_key": "hx"},
                             "AE-CAT-02", "质控员", "B-HIST")
    review = loop.review(candidate["candidate_id"], "主任")
    assert review["ok"] is True
    assert loop.version() == 1

    after = queries.batch_stats("B-HIST")
    after_rows = queries.unexpected_rows("B-HIST")
    assert after == before
    assert after_rows == before_rows
    assert after["unexpected_count"] == 3
    assert after["passed_rules"] == before["passed_rules"]
    assert sandbox["official"].read_bytes() == sandbox["official_bytes"]
    store.close()
