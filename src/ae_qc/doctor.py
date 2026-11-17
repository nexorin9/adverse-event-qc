"""运维体检（doctor）：启动期一次性核对「配置 / 资产 / 台账 / 模型端点」是否就绪。

定位：排障与无人值守跑批的前置体检，**不是**日常业务入口。信息科排障最怕
「跑起来了但什么都没拉到」，因此缺任一必需环境变量时绝不静默降级，逐个点名
变量名与用途（复用 ``config.require_env`` / ``missing_env``）。

四档输出（每项带处置建议，处置建议统一复用 ``errors.describe`` 的错误码表）：
    - 可用：该项检查通过；
    - 缺配置：必需环境变量缺失（阻断跑批，退出码 3）；
    - 资产缺失：目录资产 / 规则套件 / 台账不可用（阻断跑批，退出码 3）；
    - 端点不可达：模型端点连通性失败（**不阻断**：越界照常计数、归因降级人工，
      退出码 4 仅提示无人值守调度器「可降级跑批」）。

模型端点检查是**只读连通性探测**：仅对根路径发一个不带任何请求体的 GET（不访问
/chat/completions 等推理业务路径、不构造业务 payload），任何 HTTP 应答都视为
「端口在监听 / 可达」；连接失败或超时才判不可达。出站 HTTP 一律走 httpx，
trust_env=False 避免被环境代理干扰。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import PROJECT_ROOT, missing_env, require_env
from .engine.ruleset import load_ruleset
from .errors import ConfigError, describe
from .ledger.store import Store

# ---- 四档状态（输出按这四档归类）----
STATUS_OK = "可用"
STATUS_CONFIG = "缺配置"
STATUS_ASSET = "资产缺失"
STATUS_ENDPOINT = "端点不可达"
BUCKETS = (STATUS_OK, STATUS_CONFIG, STATUS_ASSET, STATUS_ENDPOINT)

# doctor 需要核对的目录资产（相对 catalogs/）
ASSET_FILES = (
    "severity_levels.json",
    "event_categories.json",
    "aliases.csv",
    "dept_master.csv",
    "column_mapping.csv",
)

DEFAULT_PROBE_TIMEOUT = 1.5


def check_model_endpoint(base_url: str, *, timeout: float = DEFAULT_PROBE_TIMEOUT,
                         transport: Any = None) -> dict[str, Any]:
    """只读连通性探测：GET 根路径，不带任何业务请求体，也不访问推理/写回业务路径。

    返回 ``{"reachable": bool, "code": str, "detail": str}``：
      - 拿到任意 HTTP 应答（2xx/3xx/4xx/5xx）即判 reachable=True（端口在监听）；
      - httpx 超时判 TIMEOUT；连接失败 / 协议错误判 CONN_FAILED。
    ``transport`` 仅供测试注入 httpx.MockTransport，产品运行不传（走真实 httpx）。
    """
    client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport,
                          trust_env=False)
    try:
        response = client.get("/")  # 纯连通性探测：无 body、无业务路径、无业务 payload
        return {"reachable": True, "code": "",
                "detail": f"收到 HTTP {response.status_code}（端口在监听）"}
    except httpx.TimeoutException:
        return {"reachable": False, "code": "TIMEOUT", "detail": "连接超时"}
    except Exception as exc:  # ConnectError / 非法 URL / 其它传输级错误
        return {"reachable": False, "code": "CONN_FAILED",
                "detail": f"{type(exc).__name__}"}
    finally:
        client.close()


def _item(name: str, status: str, code: str, detail: str, *, ok: bool,
          blocking: bool) -> dict[str, Any]:
    """构造一个体检项；处置建议统一取自错误码表 ``errors.describe``。"""
    meta = describe(code)
    return {
        "name": name,
        "status": status,
        "ok": ok,
        "code": code,                         # 该项失败态对应的错误码
        "desc": meta["desc"],                 # 复用 describe：说明
        "advice": meta["action"] if not ok else "无需处置（当前检查通过）",
        "detail": detail,
        "blocking": blocking,                 # True=阻断跑批；端点不可达为 False
    }


def _check_env(env: dict[str, str] | None) -> tuple[dict[str, Any], list[str]]:
    source = dict(os.environ if env is None else env)
    try:
        require_env(env=env)  # 启动期校验：缺项抛 ConfigError 并逐个点名变量名与用途
    except ConfigError as exc:
        # AE_RULE_SET 允许留空（require_env 语义），从结构化缺失清单里剔除
        missing = [n for n in missing_env(env=env) if n != "AE_RULE_SET"]
        detail = (f"缺失 {len(missing)} 项必需变量：{'、'.join(missing)}。"
                  f"启动校验报错：{str(exc)}")
        return (_item("环境变量（9 项）", STATUS_CONFIG, "CONFIG_MISSING", detail,
                      ok=False, blocking=True), missing)
    note = ("" if str(source.get("AE_RULE_SET", "")).strip()
            else "；AE_RULE_SET 未设置（留空即用当前生效版本，属允许情形）")
    return (_item("环境变量（9 项）", STATUS_OK, "CONFIG_MISSING",
                  f"9 项必需变量齐备{note}", ok=True, blocking=False), [])


def _check_assets(catalogs_dir: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for name in ASSET_FILES:
        path = Path(catalogs_dir) / name
        if path.is_file():
            items.append(_item(f"资产 {name}", STATUS_OK, "RULE_ASSET_MISSING",
                               f"存在：{path}", ok=True, blocking=False))
        else:
            items.append(_item(f"资产 {name}", STATUS_ASSET, "RULE_ASSET_MISSING",
                               f"缺失：{path}", ok=False, blocking=True))
    return items


def _check_rulesets(ruleset_dir: Path) -> dict[str, Any]:
    rdir = Path(ruleset_dir)
    files = sorted(p for p in rdir.glob("*")
                   if p.suffix.lower() in (".yaml", ".yml", ".json"))
    if not files:
        return _item("规则套件", STATUS_ASSET, "RULESET_NOT_FOUND",
                     f"{rdir} 下没有任何套件文件", ok=False, blocking=True)
    loaded: list[str] = []
    for path in files:
        try:
            rs = load_ruleset(path)  # 加载期即校验条目 schema，不静默放行非法条目
            loaded.append(f"{rs.version}({len(rs.entries)}条)")
        except Exception as exc:
            return _item("规则套件", STATUS_ASSET, "RULESET_NOT_FOUND",
                         f"{path.name} 加载失败：{type(exc).__name__}: {exc}",
                         ok=False, blocking=True)
    return _item("规则套件", STATUS_OK, "RULESET_NOT_FOUND",
                 "全部可加载：" + "、".join(loaded), ok=True, blocking=False)


def _check_ledger() -> dict[str, Any]:
    """台账可写：用临时文件 Store 真正 initialize + 写 + 读，不触碰用户默认库。"""
    try:
        with tempfile.TemporaryDirectory(prefix="ae_qc_doctor_") as d:
            store = Store(str(Path(d) / "doctor.sqlite"))
            store.initialize()
            version = store.version
            store.execute("CREATE TABLE IF NOT EXISTS _doctor_probe (x INTEGER)")
            store.execute("INSERT INTO _doctor_probe(x) VALUES (1)")
            count = int(store.query("SELECT COUNT(*) AS n FROM _doctor_probe")[0]["n"])
            store.close()
            if count != 1:
                raise RuntimeError("写入回读不一致")
        return _item("台账可写", STATUS_OK, "CONFIG_MISSING",
                     f"临时文件台账可初始化并读写（schema v{version}）",
                     ok=True, blocking=False)
    except Exception as exc:
        return _item("台账可写", STATUS_ASSET, "CONFIG_MISSING",
                     f"临时台账不可写：{type(exc).__name__}: {exc}",
                     ok=False, blocking=True)


def _check_model(env: dict[str, str] | None,
                 model_probe: Callable[[str], dict[str, Any]] | None) -> dict[str, Any]:
    source = dict(os.environ if env is None else env)
    url = str(source.get("MODEL_BASE_URL", "")).strip()
    if not url:
        # 端点地址本身缺配置：由环境变量档点名，不谎报「可达」
        return _item("模型端点", STATUS_CONFIG, "CONFIG_MISSING",
                     "MODEL_BASE_URL 未配置，无法做连通性探测（仅检查，不发业务请求）",
                     ok=False, blocking=True)
    probe = model_probe(url) if model_probe is not None else check_model_endpoint(url)
    if probe.get("reachable"):
        return _item("模型端点", STATUS_OK, "MODEL_UNAVAILABLE",
                     f"可达：{url}；{probe.get('detail', '')}", ok=True, blocking=False)
    return _item("模型端点", STATUS_ENDPOINT, "MODEL_UNAVAILABLE",
                 f"不可达：{url}；{probe.get('detail', '')}"
                 "（不阻断：越界照常计数，归因降级人工指定，不写回）",
                 ok=False, blocking=False)


def run_doctor(*, env: dict[str, str] | None = None,
               catalogs_dir: str | Path | None = None,
               ruleset_dir: str | Path | None = None,
               model_probe: Callable[[str], dict[str, Any]] | None = None) -> dict[str, Any]:
    """跑一次体检，返回结构化报告（不抛栈、不因端点不可达而中断其余检查）。

    ``env`` / ``catalogs_dir`` / ``ruleset_dir`` / ``model_probe`` 均可显式注入，
    便于测试用 monkeypatch/替身构造四档场景，产品运行不传即读真实环境。
    返回含：checks / counts(四档计数) / overall / blocking / exit_code / missing_env。
    """
    cdir = Path(catalogs_dir) if catalogs_dir else PROJECT_ROOT / "catalogs"
    rdir = Path(ruleset_dir) if ruleset_dir else PROJECT_ROOT / "catalogs" / "rulesets"

    checks: list[dict[str, Any]] = []
    env_item, missing = _check_env(env)
    checks.append(env_item)
    checks.extend(_check_assets(cdir))
    checks.append(_check_rulesets(rdir))
    checks.append(_check_ledger())
    checks.append(_check_model(env, model_probe))

    counts: dict[str, int] = {bucket: 0 for bucket in BUCKETS}
    for check in checks:
        counts[check["status"]] += 1

    blocking = any((not c["ok"]) and c["blocking"] for c in checks)
    # 退出码语义：0 成功 / 2 参数错 / 3 配置缺(含资产缺失) / 4 依赖端点不可达
    if counts[STATUS_CONFIG] or counts[STATUS_ASSET]:
        exit_code = 3
    elif counts[STATUS_ENDPOINT]:
        exit_code = 4
    else:
        exit_code = 0
    overall = next(b for b in (STATUS_CONFIG, STATUS_ASSET, STATUS_ENDPOINT, STATUS_OK)
                   if counts[b])

    return {
        "checks": checks,
        "counts": counts,
        "missing_env": missing,
        "overall": overall,
        "blocking": blocking,
        "exit_code": exit_code,
    }


def render_doctor(report: dict[str, Any]) -> str:
    """把体检报告渲染成按四档归类、带处置建议的人读文本（CLI 打印用）。"""
    lines = ["===== ae-qc 运维体检（doctor）=====",
             f"总判定：{report['overall']}  |  建议退出码：{report['exit_code']}"
             "（0 成功 / 2 参数错 / 3 配置缺 / 4 依赖端点不可达）",
             "是否阻断跑批：" + ("是" if report["blocking"]
                             else "否（端点不可达不阻断，可降级跑批）")]
    for bucket in BUCKETS:
        items = [c for c in report["checks"] if c["status"] == bucket]
        lines.append(f"[{bucket}] {len(items)} 项")
        for c in items:
            lines.append(f"  - {c['name']}：{c['detail']}")
            if not c["ok"]:
                lines.append(f"      处置建议（{c['code']}）：{c['advice']}")
    lines.append("===== 体检结束 =====")
    return "\n".join(lines)


__all__ = [
    "STATUS_OK", "STATUS_CONFIG", "STATUS_ASSET", "STATUS_ENDPOINT", "BUCKETS",
    "ASSET_FILES", "check_model_endpoint", "run_doctor", "render_doctor",
]
