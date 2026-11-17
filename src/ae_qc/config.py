"""配置加载：config.toml + .env + 环境变量，优先级 环境变量 > .env > config.toml。

凭据一律从环境变量读取，仓库内不落任何密钥；缺任一必需变量时启动即报错，
逐个点名变量名与用途，不静默降级（信息科排障时最怕「跑起来了但什么都没拉到」）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROUND_ROOT = PROJECT_ROOT.parent

try:  # python-dotenv 是运行依赖；缺失时仍能读真实环境变量
    from dotenv import load_dotenv as _load_dotenv
except ImportError:  # pragma: no cover
    _load_dotenv = None

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

REQUIRED_ENV: dict[str, str] = {
    "AE_BASE_URL": "院内不良事件报告平台根地址（入站 HTTP GET）",
    "AE_API_TOKEN": "入站平台 Bearer 令牌",
    "QC_BASE_URL": "质控管理平台根地址（出站 HTTP POST）",
    "QC_TOKEN": "出站写回 Bearer 令牌",
    "MODEL_BASE_URL": "OpenAI-compatible 推理服务端点",
    "MODEL_API_KEY": "模型访问令牌",
    "MODEL_NAME": "模型名（院内自建推理服务决定）",
    "MODEL_TIMEOUT": "单次模型调用超时秒数",
    "AE_RULE_SET": "本次跑批使用的期望套件版本；留空表示当前生效版本",
}

_CACHE: dict[str, Any] = {}


def load_config(project_root: Path | str | None = None, *, env: dict[str, str] | None = None,
                fresh: bool = False) -> dict[str, Any]:
    """合并三层配置。env 参数为显式注入（测试用），默认 os.environ。"""
    root = Path(project_root) if project_root else PROJECT_ROOT
    key = str(root)
    if fresh:
        _CACHE.pop(key, None)
    if key in _CACHE:
        return dict(_CACHE[key])

    cfg: dict[str, Any] = {}
    toml_path = root / "config.toml"
    if toml_path.exists():
        with toml_path.open("rb") as fh:
            cfg = tomllib.load(fh)

    dotenv_path = root / ".env"
    if _load_dotenv is not None and dotenv_path.exists():
        _load_dotenv(dotenv_path=dotenv_path, override=False)

    effective_env = dict(os.environ if env is None else env)
    # 环境变量覆盖同名 TOML 值（扁平键，如 MAX_CELL_CHARS）；分组配置保持 TOML 原样
    for name, value in effective_env.items():
        flat = name.lower()
        for section in list(cfg.values()):
            if isinstance(section, dict) and flat in section:
                section[flat] = value
    cfg["_env"] = effective_env
    cfg["_project_root"] = str(root)
    _CACHE[key] = cfg
    return dict(cfg)


def config_value(key: str, default: Any = None, section: str | None = None) -> Any:
    """读一个非敏感运行参数：环境变量同名大写优先，其次 TOML 分组键。"""
    cfg = load_config()
    env_name = key.upper()
    if env_name in cfg.get("_env", {}):
        return cfg["_env"][env_name]
    if section:
        return cfg.get(section, {}).get(key, default)
    for value in cfg.values():
        if isinstance(value, dict) and key in value:
            return value[key]
    return default


def get_credential(name: str, env: dict[str, str] | None = None) -> str:
    """取一个凭据；缺失时抛错并点名变量名与用途。"""
    source = dict(os.environ if env is None else env)
    value = source.get(name, "")
    if not value:
        purpose = REQUIRED_ENV.get(name, "对接凭据")
        raise ConfigError(f"缺少必需环境变量 {name}（用途：{purpose}）；请补 .env 或环境变量后重启")
    return value


def missing_env(names: list[str] | None = None, env: dict[str, str] | None = None) -> list[str]:
    source = dict(os.environ if env is None else env)
    return [n for n in (names or list(REQUIRED_ENV)) if not source.get(n, "").strip()]


def require_env(names: list[str] | None = None, env: dict[str, str] | None = None) -> dict[str, str]:
    """启动期校验：缺项一次性逐个点名变量名与用途。AE_RULE_SET 允许留空（默认生效版本）。"""
    source = dict(os.environ if env is None else env)
    targets = list(names or REQUIRED_ENV)
    missing = []
    for name in targets:
        value = source.get(name, "").strip()
        if not value and name != "AE_RULE_SET":
            missing.append(f"  - {name}：{REQUIRED_ENV.get(name, '对接凭据')}")
    if missing:
        raise ConfigError("缺少必需环境变量，启动中止（不静默降级）：\n" + "\n".join(missing))
    return {n: source.get(n, "") for n in targets}


def model_settings(env: dict[str, str] | None = None) -> dict[str, Any]:
    """模型网关配置（timeout 为空时给默认值，不构成静默降级）。"""
    source = dict(os.environ if env is None else env)
    raw = source.get("MODEL_TIMEOUT", "30")
    try:
        timeout = float(raw) if raw else 30.0
    except (TypeError, ValueError):
        timeout = 30.0
    return {
        "base_url": source.get("MODEL_BASE_URL", ""),
        "api_key": source.get("MODEL_API_KEY", ""),
        "model": source.get("MODEL_NAME", ""),
        "timeout": timeout,
    }
