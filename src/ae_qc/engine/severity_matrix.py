"""严重程度—类别一致性矩阵 + 别名表优先归并与模型仲裁契约。

① 等级 × 类别族的允许组合表（可编辑，catalogs/severity_matrix.csv）；
② 别名表精确匹配优先，模型只处理未命中项；两者冲突时双方都记入证据并交人工，
   不静默取其一。判定来源（别名表 / 模型 / 人工）随结果保留，供事后评估准确率。
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT
from ..models import RuleEntry, ExpectedResult
from .result import build_result

DEFAULT_MATRIX_PATH = PROJECT_ROOT / "catalogs" / "severity_matrix.csv"


def load_matrix(path: str | Path | None = None) -> list[dict[str, str]]:
    """读取允许组合表；列 level, category_family, allowed(1/0)。"""
    target = Path(path) if path else DEFAULT_MATRIX_PATH
    rows: list[dict[str, str]] = []
    if not target.exists():
        return rows
    with target.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            rows.append({k: (v or "").strip() for k, v in row.items()})
    return rows


def check_combination(level: Any, category_family: Any, matrix: list[dict] | None = None) -> bool:
    """组合是否被允许；表中没有的组合默认不允许（保守，不猜）。"""
    matrix = matrix if matrix is not None else load_matrix()
    lv, fam = str(level or "").strip(), str(category_family or "").strip()
    for row in matrix:
        if row.get("level") == lv and row.get("category_family") == fam:
            return str(row.get("allowed", "1")).strip() in ("1", "true", "True", "是")
    return False


def evaluate_matrix(rows: list[dict], matrix: list[dict] | None = None, *,
                    level_col: str = "severity_level", family_col: str = "category_family",
                    rule_id: str = "AE-SEV-MATRIX") -> ExpectedResult:
    """越组合时产出跨列派生一致性发现（错误级），合法组合通过。"""
    matrix = matrix if matrix is not None else load_matrix()
    unexpected = []
    for row in rows:
        level, family = row.get(level_col), row.get(family_col)
        if level is None or family is None or str(level).strip() == "" or str(family).strip() == "":
            continue  # 半缺由 cross_column_derived 族负责，不在这里重复计数
        if not check_combination(level, family, matrix):
            unexpected.append({
                "row_key": row.get("_row_key", ""), "row_no": row.get("_row_no", 0),
                "value": {"level": level, "category_family": family},
                "expected": "等级与类别族组合须在允许矩阵内",
                "reason": "等级—类别族组合越界",
                "raw": {k: v for k, v in row.items() if not k.startswith("_")},
            })
    entry = RuleEntry(id=rule_id, kind="cross_column_derived", column=level_col,
                      value_set=[], basis="院内严重程度—类别一致性矩阵（可编辑）",
                      params={"columns": [level_col, family_col]})
    return build_result(rule_id, level_col, unexpected, kind="cross_column_derived",
                        severity="error", basis=entry.basis, row_count=len(rows),
                        observed_value={"matrix_rows": len(matrix)})


def arbitrate(alias_hit: str | None, model_result: dict | None, row: dict | None = None) -> dict[str, Any]:
    """仲裁一条自由文本事件名的判定来源，不调用模型（由业务层按 needs_model 决定）。

    返回 dict：decision_code / source / needs_model / needs_human / evidence / reason。
    冲突时双方证据都保留，decision_code 置空并交人工，绝不静默取其一。
    """
    row = row or {}
    evidence: list[dict[str, Any]] = []
    model_code = None
    if isinstance(model_result, dict):
        model_code = model_result.get("code") or model_result.get("suggested_code")
        if model_code:
            evidence.append({"source": "model", "code": model_code,
                             "confidence": model_result.get("confidence"),
                             "reason": model_result.get("reason", "")})
    if alias_hit:
        evidence.insert(0, {"source": "alias", "code": alias_hit, "confidence": 1.0,
                            "reason": "别名表精确匹配"})

    if alias_hit and model_code and str(alias_hit) != str(model_code):
        return {
            "row_key": row.get("_row_key", ""), "decision_code": None,
            "source": "manual", "needs_model": False, "needs_human": True,
            "evidence": evidence, "reason": "别名表与模型建议冲突，双方留痕交人工",
        }
    if alias_hit:
        return {
            "row_key": row.get("_row_key", ""), "decision_code": alias_hit,
            "source": "alias", "needs_model": False, "needs_human": False,
            "evidence": evidence, "reason": "别名表命中，直接判定，不调模型",
        }
    if model_code:
        return {
            "row_key": row.get("_row_key", ""), "decision_code": model_code,
            "source": "model", "needs_model": False, "needs_human": False,
            "evidence": evidence, "reason": "别名未命中，采用模型归并结果",
        }
    return {
        "row_key": row.get("_row_key", ""), "decision_code": None,
        "source": "pending", "needs_model": True, "needs_human": True,
        "evidence": evidence, "reason": "别名未命中，标记待模型归并 / 待人工指定",
    }
