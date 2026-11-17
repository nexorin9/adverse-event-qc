"""主窗口三态文案与待写队列红条提示（任务 48）。

加载 / 空 / 错误三态各有明确文案；错误态必须给出可执行的下一步（处置建议）。
空态如实写「本批未发现越界」并给出通过率与检查过的规则数，不留空白面板。
"""
from __future__ import annotations

from typing import Any

from ..errors import describe

ERROR_KINDS = ("platform_unreachable", "rule_skipped", "attribution_degraded")

# 三类错误态：标题 / 现象 / 处置建议（建议取自 errors.describe 的 action，保持一致口径）
_ERROR_INFO: dict[str, dict[str, str]] = {
    "platform_unreachable": {
        "title": "院内上报平台不可达",
        "message": "HTTP 读取失败，本批已按「本批仅含文件来源」回落，平台增量未拉取。",
        "action": describe("CONN_FAILED")["action"],
    },
    "rule_skipped": {
        "title": "部分规则被跳过",
        "message": "有规则因目录资产缺失或执行异常被标为 skipped，未计入通过率分母，不是静默通过。",
        "action": describe("RULE_EXEC_FAILED")["action"],
    },
    "attribution_degraded": {
        "title": "归因降级为人工指定",
        "message": "模型端点不可用，越界行照常计数并出清单，但归因草稿降级、该批不写回。",
        "action": describe("MODEL_UNAVAILABLE")["action"],
    },
}

# kind 别名：界面 / 错误码 / 事件名都能落到三类之一
_KIND_ALIASES: dict[str, str] = {
    "platform": "platform_unreachable", "unreachable": "platform_unreachable",
    "CONN_FAILED": "platform_unreachable", "TIMEOUT": "platform_unreachable",
    "HTTP_5XX": "platform_unreachable",
    "skipped": "rule_skipped", "RULE_EXEC_FAILED": "rule_skipped",
    "RULE_ASSET_MISSING": "rule_skipped", "RULE_KIND_UNKNOWN": "rule_skipped",
    "degraded": "attribution_degraded", "MODEL_UNAVAILABLE": "attribution_degraded",
    "MODEL_TIMEOUT": "attribution_degraded", "MODEL_SCHEMA_INVALID": "attribution_degraded",
}


def loading_text() -> str:
    """加载态：跑批进度提示 + 可取消说明（长任务在后台线程，界面保持响应）。"""
    return ("正在跑批校验，逐条执行期望套件并定位越界行，请稍候…"
            "（长任务在后台线程运行，界面保持响应，可在状态栏取消）")


def empty_text(pass_rate: float, rule_count: int) -> str:
    """空态：明确写「本批未发现越界」+ 通过率 + 检查过的规则数。"""
    pct = f"{float(pass_rate) * 100:.1f}%"
    rules = int(rule_count)
    return (f"本批未发现越界，这是好消息。通过率 {pct}（来自聚合结果），"
            f"共检查 {rules} 条规则；面板不再留空白，可切换到其它批次。")


def error_detail(kind: str) -> dict[str, str]:
    """结构化错误态：标题 / 现象 / 处置建议 / 归一化 kind（供界面分区显示）。"""
    normalized = _KIND_ALIASES.get(str(kind), str(kind))
    info = _ERROR_INFO.get(normalized)
    if info is None:
        info = {
            "title": "出现未分类错误",
            "message": f"错误类型 {kind} 暂未登记专门文案。",
            "action": describe(str(kind))["action"],
        }
        normalized = "unknown"
    return {"kind": normalized, "title": info["title"], "message": info["message"],
            "action": info["action"]}


def error_text(kind: str) -> str:
    """错误态文案：含现象与可执行的处置建议（测试断言含「处置建议」）。"""
    d = error_detail(kind)
    return f"【{d['title']}】{d['message']} 处置建议：{d['action']}"


def pending_writeback_banner(count: int) -> str:
    """状态栏顶部红条：待写队列条数 + 恢复后按幂等键重放的处置说明。"""
    n = int(count)
    if n <= 0:
        return "待写队列：0 条（无积压）"
    return (f"待写队列红条提示：{n} 条发现单尚未写回质控平台，"
            f"网络恢复后按幂等键重放；未确认或降级行不会进入此队列。")


def state_text(state: str, **kwargs: Any) -> str:
    """三态统一入口：loading / empty / error 各取对应文案函数。"""
    if state == "loading":
        return loading_text()
    if state == "empty":
        return empty_text(kwargs.get("pass_rate", 1.0), kwargs.get("rule_count", 0))
    if state == "error":
        return error_text(kwargs.get("kind", "platform_unreachable"))
    raise ValueError(f"未知界面状态 {state!r}，允许 loading / empty / error")


__all__ = [
    "loading_text", "empty_text", "error_text", "error_detail",
    "pending_writeback_banner", "state_text", "ERROR_KINDS",
]
