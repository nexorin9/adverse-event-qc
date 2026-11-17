"""期望套件、期望结果、行级证据、归因草稿与发现单的 pydantic v2 模型。

这些对象是各层之间唯一的口径契约：规则条目（RuleEntry）来自院内可版本化的
期望套件；一次校验产出 ExpectedResult；越界计数展开为 RowEvidence；模型只产出
AttributionDraft（状态恒为待人工确认）；人工确认后才允许构造 Finding 出站。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RULE_KINDS = (
    "set_membership",
    "not_null",
    "type_format",
    "time_order",
    "cross_column_derived",
    "alias_resolution",
    "catalog_coverage",
)

STANDARD_COLUMNS = (
    "incident_id", "dept_code", "event_type_text", "event_type_code",
    "severity_level", "severity_class", "occur_time", "report_time",
    "reporter_role", "harm_desc", "root_cause_text",
)

REQUIRED_COLUMNS = ("incident_id", "dept_code", "occur_time", "report_time")

FINDING_STATES = (
    "collected", "validated", "attributed_pending", "confirmed",
    "written_back", "rectified", "archived",
)

# 272 号文：导出与出站 body 一律不得出现考核类字段或患者可识别明细。
FORBIDDEN_TOKENS = (
    "score", "rank", "penalty", "绩效", "排名", "扣分", "罚分",
    "patient_name", "姓名", "住院号", "inpatient_no", "diagnosis_detail", "诊断明细",
)


class RuleEntry(BaseModel):
    """一条声明式期望：哪一列、按哪种规则族、允许集合是什么、依据在哪里。"""

    model_config = ConfigDict(extra="allow")

    id: str
    kind: str
    column: str | None = None
    value_set: list[str] = Field(default_factory=list)
    basis: str
    severity: Literal["error", "info"] = "error"
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id_required(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("规则条目 id 不得为空")
        return v.strip()

    @field_validator("kind")
    @classmethod
    def _kind_known(cls, v: str) -> str:
        if v not in RULE_KINDS:
            raise ValueError(f"未知规则族 kind={v!r}，允许值：{', '.join(RULE_KINDS)}")
        return v

    @field_validator("basis")
    @classmethod
    def _basis_required(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("规则条目 basis 不得为空（每条口径必须可追溯到出处）")
        return v.strip()

    @model_validator(mode="after")
    def _validate_by_kind(self) -> "RuleEntry":
        # 与源仓库 _validate_value_set 同形：集合成员性期望的 value_set 为空时，
        # 必须在「加载期」被拒，而不是到运行期静默把所有值判为越界。
        if self.kind == "set_membership" and not self.value_set:
            raise ValueError("set_membership 条目的 value_set 不得为空（加载期拒绝）")
        if self.kind == "catalog_coverage" and not self.params.get("expected_categories"):
            raise ValueError("catalog_coverage 条目 params.expected_categories 不得为空")
        if not self.column and not self.params.get("columns"):
            raise ValueError(f"规则条目 {self.id} 缺少 column（或 params.columns）")
        return self


class RuleSet(BaseModel):
    """可版本化的一等资产：一批规则条目 + 版本号。"""

    model_config = ConfigDict(extra="allow")

    version: str
    entries: list[RuleEntry] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("version")
    @classmethod
    def _version_required(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("套件版本号不得为空")
        return v.strip()

    def entry_map(self) -> dict[str, RuleEntry]:
        return {e.id: e for e in self.entries}


class ExpectedResult(BaseModel):
    """统一结果结构：success / observed_value / unexpected_count / unexpected_samples[]。

    total_count 与 truncated_count 用于「展示截断但全量明细可查」；status=skipped
    的单条规则不进通过率分母；info_findings 承载反向覆盖类提示（提示级，不计错误）。
    """

    model_config = ConfigDict(extra="allow")

    rule_id: str
    column: str = ""
    success: bool = True
    observed_value: Any = None
    unexpected_count: int = 0
    unexpected_samples: list[dict[str, Any]] = Field(default_factory=list)
    total_count: int = 0
    truncated_count: int = 0
    status: Literal["executed", "skipped"] = "executed"
    skip_reason: str | None = None
    severity: str = "error"
    info_findings: list[dict[str, Any]] = Field(default_factory=list)
    rule_set_version: str = ""
    basis: str = ""
    elapsed_ms: float = 0.0
    kind: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExpectedResult":
        return cls.model_validate(data)


class RowEvidence(BaseModel):
    """一条越界计数对应的行级证据，可回指原始行（文件名 + 工作表 + 行号）。"""

    model_config = ConfigDict(extra="allow")

    row_key: str
    row_no: int
    dept_code: str = ""
    period: str = ""
    raw_values: dict[str, Any] = Field(default_factory=dict)
    column: str = ""
    present_value: Any = None
    expected_value: Any = None
    normalized_value: Any = None
    file_name: str = ""
    sheet_name: str = ""
    batch_ref: str = ""
    rule_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class AttributionDraft(BaseModel):
    """模型产出的归因草稿：永远不推进状态，人工确认是唯一放行口。"""

    model_config = ConfigDict(extra="allow")

    row_key: str
    draft_text: str = ""
    cause_type: Literal["spelling_drift", "standard_change", "new_event", "missing_info"] = "spelling_drift"
    suggested_code: str = ""
    confidence: float = 0.0
    missing_info: list[str] = Field(default_factory=list)
    state: str = "attributed_pending"
    source: Literal["alias", "model", "manual", "degraded"] = "model"
    degraded_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class Finding(BaseModel):
    """已确认发现单；出站 body 的字段白名单以此为准。"""

    model_config = ConfigDict(extra="allow")

    finding_id: str
    batch_ref: str
    dept_code: str = ""
    period: str = ""
    rule_id: str = ""
    rule_set_version: str = ""
    observed_value: Any = None
    unexpected_count: int = 0
    unexpected_samples: list[Any] = Field(default_factory=list)
    attribution_draft: str = ""
    attribution_confidence: float = 0.0
    status: str = "confirmed"
    confirmed_by: str = ""
    confirmed_at: str = ""
    row_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
