"""导出表头与出站 body 的字段白名单 + 脱敏不变量（272 号文硬约束）。

272 号文：不产出上报量排名／扣分／绩效联动；不落患者可识别明细
（姓名／住院号／诊断明细）。本模块是**唯一字段真源与统一检查器**：

- ``EXPORT_HEADERS``        整改清单 6 列（科室/行号/越界项/现值/应改为/依据）；
- ``ANNUAL_HEADERS``        年度汇总表底稿 5 列（期间/科室/类别/等级/条数）；
- ``FINDING_BODY_FIELDS``   出站 body 14 字段（spec「出站」段，逐字段一致）；
- ``FORBIDDEN_FIELD_TOKENS`` 禁用 token（考核类 + 患者可识别字段），与底座同源；
- ``assert_no_forbidden``   扫描字段名／表头，命中即抛 ValueError（可执行不变量）；
- ``sanitize``             递归删除命中禁用 token 的字段，返回脱敏副本。

所有导出器（整改清单 / 年度底稿 / 证据包）与 ``writeback.guard`` / ``qc_client``
在产出前必须经过本检查器。检查只针对**字段名与表头**，不扫描正常中文文案
（依据、提示语）的内容，避免误伤；注入一个禁用字段／患者可识别字段必被拒。

注意：``row_key`` 只用于计算幂等键 ``sha256(batch_ref|rule_id|row_key)``，
按 spec 不进入出站 body，故不在 14 字段白名单内。
"""
from __future__ import annotations

from typing import Any

from ..models import FORBIDDEN_TOKENS

# ---- 字段白名单（唯一真源，guard / qc_client / 导出器共用，杜绝各处漂移）----

EXPORT_HEADERS: tuple[str, ...] = (
    "科室", "行号", "越界项", "现值", "应改为", "依据",
)

ANNUAL_HEADERS: tuple[str, ...] = ("期间", "科室", "类别", "等级", "条数")

FINDING_BODY_FIELDS: tuple[str, ...] = (
    "finding_id", "batch_ref", "dept_code", "period", "rule_id",
    "rule_set_version", "observed_value", "unexpected_count",
    "unexpected_samples", "attribution_draft", "attribution_confidence",
    "status", "confirmed_by", "confirmed_at",
)

# 272 号文禁用 token：考核类字段 + 患者可识别字段。以底座 models.FORBIDDEN_TOKENS
# 为基础（score/rank/penalty/绩效/排名/扣分/罚分 + 姓名/住院号/诊断明细），
# 再补充常见变体，保证跨导出器与写回 body 口径一致。
FORBIDDEN_FIELD_TOKENS: tuple[str, ...] = tuple(FORBIDDEN_TOKENS) + (
    "patient_id", "id_card", "身份证", "患者姓名", "病人姓名", "门诊号", "病历号",
    "performance", "merit", "deduction",
)


def _token_hit(name: Any) -> str | None:
    """返回字段名命中的第一个禁用 token；未命中返回 None（英文大小写不敏感）。"""
    low = str(name).lower()
    for token in FORBIDDEN_FIELD_TOKENS:
        if token.lower() in low:
            return token
    return None


def _walk(value: Any, path: str) -> list[str]:
    """递归收集命中禁用 token 的字段路径。

    只检查 dict 的 key 与「字符串序列」元素（表头／字段名清单）；dict 的标量
    字符串**值**不扫描内容，避免把依据、提示文案误判为禁用字段。
    """
    hits: list[str] = []
    if isinstance(value, dict):
        for key, sub in value.items():
            here = f"{path}.{key}" if path else str(key)
            token = _token_hit(key)
            if token:
                hits.append(f"{here}（命中禁用 token {token!r}）")
            if isinstance(sub, (dict, list, tuple)):
                hits.extend(_walk(sub, here))
    elif isinstance(value, (list, tuple)):
        for idx, item in enumerate(value):
            here = f"{path}[{idx}]"
            if isinstance(item, str):
                token = _token_hit(item)
                if token:
                    hits.append(f"{here}={item!r}（命中禁用 token {token!r}）")
            elif isinstance(item, (dict, list, tuple)):
                hits.extend(_walk(item, here))
    return hits


def assert_no_forbidden(payload: Any) -> None:
    """272 号文硬约束：产物／出站 body 出现禁用字段（考核类或患者可识别）即抛 ValueError。

    接受 dict（递归查 key）、list/tuple（表头或行清单）、单个字段名字符串。
    命中时错误信息逐条点名字段路径与命中 token，便于排障。
    """
    if isinstance(payload, str):
        token = _token_hit(payload)
        hits = [f"{payload!r}（命中禁用 token {token!r}）"] if token else []
    else:
        hits = _walk(payload, "")
    if hits:
        raise ValueError(
            "272 号文硬约束：产物／出站 body 出现禁用字段（上报量排名／扣分／绩效联动"
            "或患者可识别字段），已拒绝产出：\n" + "\n".join(f"  - {h}" for h in hits)
        )


def sanitize(payload: Any) -> Any:
    """递归删除命中禁用 token 的字段，返回脱敏副本（不修改入参）。

    用于导出前的最后一道脱敏；配合 assert_no_forbidden 可实现「先拒后清」。
    """
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for key, value in payload.items():
            if _token_hit(key):
                continue
            out[key] = sanitize(value) if isinstance(value, (dict, list, tuple)) else value
        return out
    if isinstance(payload, list):
        return [sanitize(v) if isinstance(v, (dict, list, tuple)) else v for v in payload]
    if isinstance(payload, tuple):
        return tuple(sanitize(v) if isinstance(v, (dict, list, tuple)) else v for v in payload)
    return payload


__all__ = [
    "EXPORT_HEADERS", "ANNUAL_HEADERS", "FINDING_BODY_FIELDS",
    "FORBIDDEN_FIELD_TOKENS", "assert_no_forbidden", "sanitize",
]
