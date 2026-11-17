"""任务 52：端到端全链路一致性回归。

直接复用 scripts/e2e_run.run_e2e（与 e2e_check.sh 同一编排真源），在 tmp_path
工作目录里跑完整链路，逐段断言数字一致；env 用 monkeypatch 注入，写回走脚本内
自启停的本地 http.server 替身，绝不连真实端点。
"""
from __future__ import annotations

import hashlib

import pytest

from ae_qc.config import REQUIRED_ENV
from ae_qc.ledger.idempotency import idempotency_key
from scripts.e2e_run import run_e2e


def _set_env(monkeypatch) -> None:
    values = {
        "AE_BASE_URL": "http://127.0.0.1:1", "AE_API_TOKEN": "e2e-local",
        "QC_BASE_URL": "http://127.0.0.1:1", "QC_TOKEN": "e2e-local",
        "MODEL_BASE_URL": "http://127.0.0.1:1", "MODEL_API_KEY": "e2e-local",
        "MODEL_NAME": "e2e-local-model", "MODEL_TIMEOUT": "1", "AE_RULE_SET": "",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_e2e_all_stage_numbers_consistent(monkeypatch, tmp_path):
    _set_env(monkeypatch)
    n = run_e2e(work_dir=str(tmp_path))

    assert n["consistent"] is True
    assert n["generated_rows"] == n["valid_rows"] == 12, "生成行数 == 读入有效行数"
    assert n["batches"] == 1
    assert n["executed_rules"] >= 1

    # 链路核心：越界=台账=草稿=确认=写回=导出，逐段一致
    chain = [n["unexpected_rows"], n["ledger_evidence_rows"], n["attribution_drafts"],
             n["confirmed"], n["writeback_requests"], n["export_rows"]]
    assert chain[0] >= 1, "样例数据必须检出越界行"
    assert len(set(chain)) == 1, f"链路数字不一致：{chain}"
    assert n["chain"] == chain

    # 未确认时出站请求数 = 0；幂等键逐条可查
    assert n["outbound_before_confirm"] == 0
    assert n["idempotency_keys_found"] == n["writeback_requests"]
    # 本地替身已关闭，无常驻后台进程
    assert n["server_stopped"] is True
    assert n["batch_ref"] == "D01-2026-01"
    assert n["rule_set_version"] == "v2026-01"


def test_e2e_can_run_twice_no_lingering_server(monkeypatch, tmp_path):
    """连续跑两次均一致，佐证本地替身每次都彻底关闭、不占端口不残留。"""
    _set_env(monkeypatch)
    first = run_e2e(work_dir=str(tmp_path / "run1"))
    second = run_e2e(work_dir=str(tmp_path / "run2"))
    assert first["consistent"] is True and second["consistent"] is True
    assert first["server_stopped"] is True and second["server_stopped"] is True
    assert set(first["chain"]) == set(second["chain"])


def test_idempotency_key_formula_is_queryable_sha256():
    """幂等键 = sha256(batch_ref|rule_id|row_key)，64 位 hex，可稳定重算。"""
    key = idempotency_key("D01-2026-01", "AE-SEV-LEVEL", "ROW-1")
    expected = hashlib.sha256(b"D01-2026-01|AE-SEV-LEVEL|ROW-1").hexdigest()
    assert key == expected
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)
