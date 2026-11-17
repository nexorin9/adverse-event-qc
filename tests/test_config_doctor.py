"""任务 51：配置加载优先级 + doctor 四档体检 + 模型端点只读探测。

凭据一律用 monkeypatch / 显式 env 注入，绝不连真实端点；模型端点探测用
httpx.MockTransport 替身，断言「只读 GET、不带业务请求体、不访问业务路径」。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest

from ae_qc import config, doctor
from ae_qc.errors import ConfigError, describe

# 8 项必填（AE_RULE_SET 允许留空，用当前生效版本）
MANDATORY_ENV = (
    "AE_BASE_URL", "AE_API_TOKEN", "QC_BASE_URL", "QC_TOKEN",
    "MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME", "MODEL_TIMEOUT",
)


def _full_env() -> dict[str, str]:
    return {
        "AE_BASE_URL": "http://ae.test", "AE_API_TOKEN": "tok",
        "QC_BASE_URL": "http://qc.test", "QC_TOKEN": "tok",
        "MODEL_BASE_URL": "http://model.test/v1", "MODEL_API_KEY": "tok",
        "MODEL_NAME": "model-x", "MODEL_TIMEOUT": "1", "AE_RULE_SET": "",
    }


def _probe_ok(url: str) -> dict:
    return {"reachable": True, "code": "", "detail": "200 OK"}


def _probe_bad(url: str) -> dict:
    return {"reachable": False, "code": "CONN_FAILED", "detail": "connection refused"}


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """每个用例前后重置 config 的模块级缓存，避免污染本文件其它用例与其它测试文件。"""
    config.load_config(fresh=True, env={})
    yield
    config.load_config(fresh=True, env={})


# ---- 缺任一必需环境变量：报错并点名变量名 ----
@pytest.mark.parametrize("missing_name", MANDATORY_ENV)
def test_require_env_names_each_missing_env_var(missing_name):
    env = _full_env()
    env.pop(missing_name)
    with pytest.raises(ConfigError) as exc:
        config.require_env(env=env)
    message = str(exc.value)
    assert missing_name in message, f"报错必须点名 {missing_name}"
    assert "不静默降级" in message


def test_ae_rule_set_may_be_empty():
    """AE_RULE_SET 留空是允许情形（默认生效版本），不应报错。"""
    env = _full_env()
    env.pop("AE_RULE_SET")
    got = config.require_env(env=env)
    assert got["AE_RULE_SET"] == ""


def test_require_env_uses_injected_env(monkeypatch):
    """显式注入 env 优先：即便 os.environ 全缺，注入完整 env 也应通过。"""
    for name in config.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    injected = {name: "injected" for name in config.REQUIRED_ENV}
    got = config.require_env(env=injected)
    assert all(got[name] == "injected" for name in config.REQUIRED_ENV)


def test_missing_env_lists_all_required():
    assert set(config.missing_env(env={})) == set(config.REQUIRED_ENV)
    assert config.missing_env(env=_full_env()) == ["AE_RULE_SET"]  # 仅留空项


# ---- config.toml 与环境变量优先级 ----
def test_config_toml_is_baseline():
    """无同名环境变量时取 config.toml 的 [ingest] max_cell_chars = 2000。"""
    cfg = config.load_config(fresh=True, env={})
    assert cfg["ingest"]["max_cell_chars"] == 2000
    assert config.config_value("max_cell_chars") == 2000


def test_env_var_overrides_config_toml(monkeypatch):
    """环境变量 > config.toml：MAX_CELL_CHARS 覆盖 TOML 的 2000。"""
    monkeypatch.setenv("MAX_CELL_CHARS", "4096")
    cfg = config.load_config(fresh=True)  # env=None → os.environ
    assert cfg["ingest"]["max_cell_chars"] == "4096"
    assert cfg["_env"]["MAX_CELL_CHARS"] == "4096"
    assert config.config_value("max_cell_chars") == "4096"
    # 移除环境变量后回落到 TOML 基线，进一步证明优先级
    monkeypatch.delenv("MAX_CELL_CHARS")
    cfg2 = config.load_config(fresh=True)
    assert cfg2["ingest"]["max_cell_chars"] == 2000
    assert config.config_value("max_cell_chars") == 2000


# ---- doctor 四档输出 ----
def test_doctor_all_available():
    report = doctor.run_doctor(env=_full_env(), model_probe=_probe_ok)
    assert report["exit_code"] == 0
    assert report["blocking"] is False
    assert report["overall"] == doctor.STATUS_OK
    assert report["counts"][doctor.STATUS_OK] == len(report["checks"])
    for bucket in (doctor.STATUS_CONFIG, doctor.STATUS_ASSET, doctor.STATUS_ENDPOINT):
        assert report["counts"][bucket] == 0


def test_doctor_missing_config_names_variable_and_exit_3():
    env = _full_env()
    env.pop("QC_TOKEN")
    report = doctor.run_doctor(env=env, model_probe=_probe_ok)
    assert report["exit_code"] == 3
    assert report["counts"][doctor.STATUS_CONFIG] >= 1
    assert report["blocking"] is True
    assert "QC_TOKEN" in report["missing_env"]
    text = doctor.render_doctor(report)
    assert "缺配置" in text
    assert "QC_TOKEN" in text  # 点名变量名


def test_doctor_asset_missing_exit_3():
    """注入空 catalogs 目录：5 个资产全部缺失 → 资产缺失档 → 退出码 3。"""
    with tempfile.TemporaryDirectory(prefix="ae_qc_doctor_empty_") as d:
        report = doctor.run_doctor(env=_full_env(), catalogs_dir=Path(d),
                                   model_probe=_probe_ok)
    assert report["exit_code"] == 3
    assert report["counts"][doctor.STATUS_ASSET] == len(doctor.ASSET_FILES)
    assert report["blocking"] is True
    assert "资产缺失" in doctor.render_doctor(report)


def test_doctor_endpoint_unreachable_is_non_blocking_and_exit_4():
    """端点不可达不阻断：仍有其它「可用」项，退出码 4 而非 3。"""
    report = doctor.run_doctor(env=_full_env(), model_probe=_probe_bad)
    assert report["exit_code"] == 4
    assert report["blocking"] is False
    assert report["counts"][doctor.STATUS_ENDPOINT] >= 1
    assert report["counts"][doctor.STATUS_OK] > 0, "不可达不得中断其余检查"
    text = doctor.render_doctor(report)
    assert "端点不可达" in text
    assert "不阻断" in text


def test_doctor_every_item_carries_describe_advice():
    """每项带处置建议，失败项处置建议复用 errors.describe(code)['action']。"""
    report = doctor.run_doctor(env=_full_env(), model_probe=_probe_bad)
    assert report["checks"], "体检项不得为空"
    for item in report["checks"]:
        assert item["status"] in doctor.BUCKETS
        assert item["advice"], f"{item['name']} 缺处置建议"
        assert item["desc"] == describe(item["code"])["desc"]
        if not item["ok"]:
            assert item["advice"] == describe(item["code"])["action"]


def test_doctor_render_groups_all_four_buckets():
    text = doctor.render_doctor(doctor.run_doctor(env=_full_env(), model_probe=_probe_ok))
    for bucket in doctor.BUCKETS:
        assert f"[{bucket}]" in text


# ---- 模型端点检查：只读、不发业务请求 ----
def test_model_probe_is_read_only_no_business_request():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "ok"})

    result = doctor.check_model_endpoint("http://model.test/v1",
                                         transport=httpx.MockTransport(handler))
    assert result["reachable"] is True
    assert len(seen) == 1
    req = seen[0]
    assert req.method == "GET", "连通性探测应为只读 GET"
    assert req.content in (b"", None), "不得携带任何业务请求体"
    assert req.url.path not in ("/chat/completions", "/v1/chat/completions",
                                "/api/v1/quality-findings"), "不得访问业务路径"


def test_model_probe_unreachable_returns_structured_signal():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    result = doctor.check_model_endpoint("http://127.0.0.1:1",
                                         transport=httpx.MockTransport(handler))
    assert result["reachable"] is False
    assert result["code"] == "CONN_FAILED"


def test_model_probe_timeout_signal():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout")

    result = doctor.check_model_endpoint("http://model.test",
                                         transport=httpx.MockTransport(handler))
    assert result["reachable"] is False
    assert result["code"] == "TIMEOUT"
