"""任务 52：CLI 五子命令 --help、统一退出码 0/2/3/4、batch run 产出清单。

凭据一律 monkeypatch 注入（本地占位值），模型端点探测 monkeypatch 为本地替身，
绝不连真实端点；batch run / findings export 在 tmp_path 里跑，不碰用户默认台账。
"""
from __future__ import annotations

import csv
import json

import pytest

from ae_qc import cli, doctor
from ae_qc.config import REQUIRED_ENV


def _set_full_env(monkeypatch) -> None:
    values = {
        "AE_BASE_URL": "http://ae.test", "AE_API_TOKEN": "tok",
        "QC_BASE_URL": "http://qc.test", "QC_TOKEN": "tok",
        "MODEL_BASE_URL": "http://model.test/v1", "MODEL_API_KEY": "tok",
        "MODEL_NAME": "model-x", "MODEL_TIMEOUT": "1", "AE_RULE_SET": "",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _clear_env(monkeypatch) -> None:
    for name in REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)


# ---- 5 个子命令 --help 均可用 ----
@pytest.mark.parametrize("argv", [
    ["rule-set", "validate", "--help"],
    ["batch", "run", "--help"],
    ["findings", "export", "--help"],
    ["replay", "--help"],
    ["doctor", "--help"],
    ["--help"],
    ["rule-set", "--help"],
    ["batch", "--help"],
    ["findings", "--help"],
])
def test_subcommand_help_returns_zero(argv, capsys):
    rc = cli.main(argv)
    assert rc == 0, f"{argv} --help 应返回 0"
    out = capsys.readouterr().out
    assert "usage" in out.lower(), "应打印帮助文本"


# ---- 退出码 2：参数错 ----
@pytest.mark.parametrize("argv", [
    [],
    ["nope"],
    ["batch"],
    ["rule-set"],
    ["findings"],
    ["replay", "--version"],
])
def test_exit_2_on_bad_args(argv):
    assert cli.main(argv) == 2, f"{argv} 应返回参数错 2"


# ---- 退出码 0：成功 ----
def test_exit_0_ruleset_validate_without_env(monkeypatch, capsys):
    """rule-set validate 是本地校验，不依赖凭据即可成功。"""
    _clear_env(monkeypatch)
    assert cli.main(["rule-set", "validate"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "rule-set validate"
    assert payload["version"].startswith("v2026")
    assert set(payload["diff"]) == {"added", "removed", "modified"}


def test_exit_0_doctor_all_ready(monkeypatch, capsys):
    _set_full_env(monkeypatch)
    monkeypatch.setattr(doctor, "check_model_endpoint",
                        lambda url, **kw: {"reachable": True, "code": "", "detail": "ok"})
    assert cli.main(["doctor"]) == 0
    assert "可用" in capsys.readouterr().out


# ---- 退出码 3：配置缺（启动即报错并点名）----
def test_exit_3_missing_env_names_variables(monkeypatch, capsys, tmp_path):
    _clear_env(monkeypatch)
    assert cli.main(["doctor"]) == 3
    out = capsys.readouterr().out
    for name in ("AE_BASE_URL", "AE_API_TOKEN", "QC_BASE_URL", "QC_TOKEN",
                 "MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME", "MODEL_TIMEOUT"):
        assert name in out, f"doctor 缺 env 必须点名 {name}"
    # 跑批类子命令同样启动期报错
    assert cli.main(["batch", "run", "--inbox", str(tmp_path / "inbox"),
                     "--out", str(tmp_path / "out"), "--db", str(tmp_path / "l.db")]) == 3


# ---- 退出码 4：依赖端点不可达（不阻断）----
def test_exit_4_endpoint_unreachable(monkeypatch, capsys):
    _set_full_env(monkeypatch)
    monkeypatch.setattr(doctor, "check_model_endpoint",
                        lambda url, **kw: {"reachable": False, "code": "CONN_FAILED",
                                           "detail": "refused"})
    assert cli.main(["doctor"]) == 4
    out = capsys.readouterr().out
    assert "端点不可达" in out
    assert "不阻断" in out


# ---- batch run 在样例数据上产出清单文件，并与 findings export 数字一致 ----
def test_batch_run_produces_unexpected_list_file(monkeypatch, tmp_path, capsys):
    _set_full_env(monkeypatch)
    inbox = tmp_path / "inbox"
    out = tmp_path / "out"
    db = tmp_path / "ledger.sqlite"

    assert cli.main(["batch", "run", "--inbox", str(inbox), "--out", str(out),
                     "--db", str(db), "--rows", "12"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["command"] == "batch run"
    assert summary["checked_rows"] == 12
    assert summary["unexpected_rows"] >= 1

    lists = list(out.glob("越界行清单_*.csv"))
    assert len(lists) == 1, "必须产出越界行清单文件"
    with lists[0].open("r", encoding="utf-8-sig", newline="") as fh:
        table = list(csv.reader(fh))
    assert table[0][0] == "批次号"
    assert len(table) - 1 == summary["unexpected_rows"], "清单数据行数 == 越界行数"

    # 同一台账串联 findings export：导出行数与越界行数一致
    assert cli.main(["findings", "export", "--db", str(db), "--out", str(out)]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["row_count"] == summary["unexpected_rows"]
    assert list(out.glob("整改清单_*.xlsx")), "必须产出整改清单 xlsx"


# ---- replay 能重放历史批次 ----
def test_replay_historical_batch(monkeypatch, tmp_path, capsys):
    _set_full_env(monkeypatch)
    assert cli.main(["replay", "--inbox", str(tmp_path / "inbox"),
                     "--version", "v2026-01", "--rows", "12"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "replay"
    assert payload["version"] == "v2026-01"
    assert payload["results"] >= 1
    assert payload["unexpected_rows"] >= 1
