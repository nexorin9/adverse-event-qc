"""分层错误码表：每个码带中文说明与处置建议，供结构化日志与 doctor 复用。"""
from __future__ import annotations


class AQCError(Exception):
    """产品内所有可预期错误的基类。"""

    def __init__(self, message: str, code: str = "UNKNOWN"):
        super().__init__(message)
        self.code = code
        self.message = message


class ConfigError(AQCError):
    def __init__(self, message: str, code: str = "CONFIG_MISSING"):
        super().__init__(message, code)


class IngestError(AQCError):
    def __init__(self, message: str, code: str = "INGEST_READ_FAILED"):
        super().__init__(message, code)


class RuleError(AQCError):
    def __init__(self, message: str, code: str = "RULE_EXEC_FAILED"):
        super().__init__(message, code)


class ModelError(AQCError):
    def __init__(self, message: str, code: str = "MODEL_UNAVAILABLE"):
        super().__init__(message, code)


class WritebackError(AQCError):
    def __init__(self, message: str, code: str = "WRITEBACK_REJECTED"):
        super().__init__(message, code)


def _c(layer: str, desc: str, action: str) -> dict[str, str]:
    return {"layer": layer, "desc": desc, "action": action}


ERROR_CODES: dict[str, dict[str, str]] = {
    # 配置
    "CONFIG_MISSING": _c("配置", "必需环境变量缺失", "按报错点名的变量名补 .env，重新启动；缺项不静默降级"),
    "CONFIG_BAD_TOML": _c("配置", "config.toml 无法解析", "检查 TOML 语法，或与 .env.example 对照"),
    # 入站
    "INGEST_FILE_CORRUPT": _c("入站", "xlsx 文件损坏或不是工作簿", "请科室重导上报表；本表结构化报错，不影响其他表"),
    "INGEST_NO_SHEET": _c("入站", "工作表缺失", "确认上报表含明细工作表"),
    "INGEST_EMPTY_HEADER": _c("入站", "表头为空，无法按表头驱动取列", "补表头后重新投递"),
    "INGEST_UNMAPPED_COLUMN": _c("入站", "存在未映射列，已进待确认清单", "在 catalogs/column_mapping.csv 补映射，不猜"),
    "INGEST_REQUIRED_COLUMN_MISSING": _c("入站", "必填标准列缺失，整表判不可用", "补齐 occur_time/report_time/dept_code 等列"),
    "INGEST_AMBIGUOUS_HEADER": _c("入站", "一个表头映射到多列，挂起并列候选", "人工指定映射，相关规则置 skipped"),
    "INGEST_SUSPENDED_DEPT": _c("入站", "科室编码不在主数据或映射到多码", "更新 dept_master.csv 或人工指定，不猜"),
    "CONN_FAILED": _c("入站", "连接失败", "检查端点与网络；平台不可达时回落文件来源"),
    "TIMEOUT": _c("入站", "请求超时", "可重试 ≤2 次，仍失败回落文件来源"),
    "HTTP_4XX": _c("入站", "HTTP 4xx 业务拒绝", "不重试；核对凭据与参数"),
    "HTTP_5XX": _c("入站", "HTTP 5xx 服务端错误", "退避重试 ≤2 次，仍失败回落文件来源"),
    "NOT_JSON": _c("入站", "响应不是合法 JSON", "检查对端接口；不静默补空"),
    "SCHEMA_INVALID": _c("入站", "响应字段缺列/多列/类型不符", "按正式对接规范修正字段"),
    # 规则
    "RULE_EXEC_FAILED": _c("规则", "单条规则执行抛异常", "该条记 skipped 并点名，其余规则照跑"),
    "RULE_KIND_UNKNOWN": _c("规则", "未知规则族", "检查套件条目的 kind 是否在白名单"),
    "RULE_EMPTY_VALUE_SET": _c("规则", "集合成员性条目的允许集合为空", "加载期拒绝该条目"),
    "RULESET_NOT_FOUND": _c("规则", "期望套件版本不存在", "检查 catalogs/rulesets/ 与 AE_RULE_SET"),
    "RULE_ASSET_MISSING": _c("规则", "目录资产缺失", "该规则整条 skipped 并点名，不静默通过"),
    # 模型
    "MODEL_UNAVAILABLE": _c("归因", "模型端点不可达", "越界照常计数，归因降级人工指定，不写回"),
    "MODEL_TIMEOUT": _c("归因", "模型调用超时", "退避重试 ≤2 次后降级"),
    "MODEL_HTTP_4XX": _c("归因", "模型端点 4xx", "不重试；核对凭据与请求体"),
    "MODEL_HTTP_5XX": _c("归因", "模型端点 5xx", "退避重试 ≤2 次后降级"),
    "MODEL_NOT_JSON": _c("归因", "模型响应非 JSON", "不静默兜底，按 schema 不合格处理"),
    "MODEL_SCHEMA_INVALID": _c("归因", "模型响应不符合固定 JSON schema", "不做静默兜底，该行降级人工"),
    "MODEL_CANDIDATE_NOT_IN_CATALOG": _c("归因", "模型候选编码不在院内目录", "落「无法归类·待人工指定」，不取最近类"),
    # 写回
    "WRITEBACK_NOT_CONFIRMED": _c("写回", "状态非 confirmed，拒绝出站", "先由质控员确认；未确认时出站请求数 = 0"),
    "WRITEBACK_DEGRADED": _c("写回", "归因降级行不得写回", "先人工完成归类"),
    "WRITEBACK_FORBIDDEN_FIELD": _c("写回", "body 含白名单外或考核类字段", "按 spec 字段白名单重建 body"),
    "WRITEBACK_4XX": _c("写回", "质控平台业务拒绝", "不重试，落冲突记录"),
    "WRITEBACK_5XX": _c("写回", "质控平台 5xx", "退避 ≤3 次后转 pending_writeback 队列"),
    "WRITEBACK_PENDING": _c("写回", "发现单在待写队列", "主窗口红条提示条数，恢复后按幂等键重放"),
    "STATE_TRANSITION_ILLEGAL": _c("台账", "非法状态迁移", "拒绝并写审计（含被拒迁移与操作人）"),
}


def describe(code: str) -> dict[str, str]:
    """返回错误码的说明与处置建议；未知码给出显式兜底说明。"""
    return ERROR_CODES.get(code, _c("未分类", f"未登记错误码 {code}", "查看结构化日志与 progress.txt"))


def codes_by_layer(layer: str) -> list[str]:
    return [code for code, meta in ERROR_CODES.items() if meta["layer"] == layer]
