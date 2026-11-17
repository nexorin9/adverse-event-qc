"""对照评测：模型语义归并 vs 别名表精确匹配基线（200 条人工标注集）。

深度落点 (b)：模型相对规则的增量必须可量化。两条路径在同一份标注集上跑，
输出命中率 / 准确率 / 不可归类数 / 与人工标注一致率 / 冲突条数的对比表。

诚实性纪律：模型端点不可达时**只出基线结果**并标注「模型不可用，本次无比模型
增量」，绝不伪造对比数字。若已有真实模型跑出的预测，可用 --predictions 传入
JSON 离线复算（文件须注明来源）。本地协议替身只准出现在测试层。

用法：
  python scripts/eval_merge.py                      # 离线，只出基线 + 降级说明
  python scripts/eval_merge.py --model             # 尝试 env 配置的真实模型端点
  python scripts/eval_merge.py --predictions p.json --predictions-source '院内自建模型 v1'
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ae_qc.attribution.merge import MERGE_SCHEMA, merge_event_names  # noqa: E402
from ae_qc.engine.rule_kinds import alias_lookup  # noqa: E402

EVAL_SET = PROJECT_ROOT / "assets" / "eval" / "labeled_event_names.json"
ALIASES_CSV = PROJECT_ROOT / "catalogs" / "aliases.csv"
CATEGORIES_JSON = PROJECT_ROOT / "catalogs" / "event_categories.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "docs" / "语义归并对照表.md"

TABLE_COLUMNS = ("指标", "别名表精确匹配基线", "模型语义归并", "说明")


def load_items(path: str | Path | None = None) -> list[dict[str, Any]]:
    payload = json.loads(Path(path or EVAL_SET).read_text(encoding="utf-8"))
    return list(payload["items"])


def load_aliases() -> dict[str, str]:
    with ALIASES_CSV.open("r", encoding="utf-8-sig", newline="") as fh:
        return {r["alias_text"].strip(): r["category_code"].strip() for r in csv.DictReader(fh)}


def load_categories() -> list[dict[str, Any]]:
    return json.loads(CATEGORIES_JSON.read_text(encoding="utf-8"))["categories"]


def baseline_predictions(items: list[dict], aliases: dict[str, str]) -> list[dict[str, Any]]:
    """确定性基线：别名表精确匹配；未命中一律不猜，落「无法归类·待人工指定」。"""
    out = []
    for item in items:
        code = alias_lookup(item["text"], aliases)
        out.append({"id": item["id"], "text": item["text"], "prediction": code,
                    "unclassifiable": code is None, "source": "alias"})
    return out


def model_predictions(items: list[dict], gateway: Any, aliases: dict[str, str],
                      categories: list[dict]) -> tuple[list[dict[str, Any]], bool, str]:
    """模型路径：别名命中优先，未命中才调模型；目录外候选一律拒绝。"""
    rows = [{"_row_key": it["id"], "event_type_text": it["text"],
             "severity_level": "", "severity_class": "", "category_family": ""} for it in items]
    try:
        merged = merge_event_names(rows, gateway, categories, aliases)
    except Exception as exc:
        return [], False, f"{type(exc).__name__}: {exc}"
    # 任一行降级即判整体不可用：评测要求两路径在同一份标注集上完整可比对，
    # 不能把「部分行模型挂了」悄悄算成模型成绩。
    degraded = [m for m in merged if m.get("unavailable")]
    if not merged or degraded:
        reason = (degraded[0].get("error_code") if degraded else "") or "MODEL_UNAVAILABLE"
        return [], False, reason
    by_key = {str(m.get("row_key")): m for m in merged}
    out = []
    for item in items:
        m = by_key.get(item["id"], {})
        code = m.get("decision_code") or None
        out.append({"id": item["id"], "text": item["text"], "prediction": code,
                    "unclassifiable": code is None,
                    "source": m.get("source", "model"), "status": m.get("status", "")})
    return out, True, ""


def _metrics(items: list[dict], preds: list[dict]) -> dict[str, Any]:
    gold = {it["id"]: it["gold_code"] for it in items}
    decided = [p for p in preds if p["prediction"]]
    correct = [p for p in decided if p["prediction"] == gold.get(p["id"])]
    unclassifiable = [p for p in preds if p["unclassifiable"]]
    agree = 0
    for p in preds:
        is_unclear = gold.get(p["id"]) == "UNCLASSIFIABLE"
        if (is_unclear and p["unclassifiable"]) or (not is_unclear and p["prediction"] == gold.get(p["id"])):
            agree += 1
    total = max(1, len(items))
    return {
        "total": len(items),
        "hit_count": len(decided),
        "hit_rate": round(len(decided) / total, 4),
        "accuracy": round(len(correct) / len(decided), 4) if decided else 0.0,
        "unclassifiable_count": len(unclassifiable),
        "unclassifiable_agreement": round(agree / total, 4),
        "wrong_decisions": len(decided) - len(correct),
    }


def evaluate(items: list[dict], baseline: list[dict],
             model: list[dict] | None = None, *, model_available: bool = False,
             unavailable_reason: str = "", predictions_source: str = "") -> dict[str, Any]:
    """汇总两条路径的对比结果；模型不可用时模型列为空，不编造数字。"""
    base = _metrics(items, baseline)
    result: dict[str, Any] = {
        "labeled_count": len(items), "baseline": base,
        "model_available": bool(model_available and model),
        "unavailable_reason": unavailable_reason,
        "predictions_source": predictions_source,
        "columns": list(TABLE_COLUMNS),
    }
    if result["model_available"]:
        result["model"] = _metrics(items, model)
        bmap = {p["id"]: p for p in baseline}
        mmap = {p["id"]: p for p in model}
        result["conflict_count"] = sum(
            1 for i in items
            if bmap[i["id"]]["prediction"] and mmap[i["id"]]["prediction"]
            and bmap[i["id"]]["prediction"] != mmap[i["id"]]["prediction"]
        )
        result["model_increment"] = {
            "hit_rate_delta": round(result["model"]["hit_rate"] - base["hit_rate"], 4),
            "accuracy_delta": round(result["model"]["accuracy"] - base["accuracy"], 4),
            "newly_decided": result["model"]["hit_count"] - base["hit_count"],
        }
    else:
        result["model"] = None
        result["conflict_count"] = None
        result["model_increment"] = None
    return result


def render_markdown(result: dict[str, Any]) -> str:
    b = result["baseline"]
    m = result["model"]
    na = "—（模型不可用，本次无比模型增量）"
    rows = [
        ("评测集条数", str(result["labeled_count"]), str(result["labeled_count"]) if m else na,
         "200 条脱敏合成标注，来源与 basis 见 assets/eval/labeled_event_names.json"),
        ("命中率（给出明确目录编码）", f"{b['hit_rate']:.2%}", f"{m['hit_rate']:.2%}" if m else na,
         "命中条数 ÷ 标注总数；基线只对已登记别名精确匹配"),
        ("准确率（命中且判对）", f"{b['accuracy']:.2%}", f"{m['accuracy']:.2%}" if m else na,
         "判对条数 ÷ 有明确判定条数；模型候选须过目录成员性复核"),
        ("不可归类数", str(b["unclassifiable_count"]), str(m["unclassifiable_count"]) if m else na,
         "含无法归类·待人工指定；基线未命中一律不猜"),
        ("不可归类判定与人工标注一致率", f"{b['unclassifiable_agreement']:.2%}",
         f"{m['unclassifiable_agreement']:.2%}" if m else na,
         "应不可归类且未判定 + 应归类且判对，合计 ÷ 总数"),
        ("两路径冲突条数", "—", str(result["conflict_count"]) if m else na,
         "两路径都有明确编码但不一致，交人工仲裁"),
    ]
    lines = [
        "# 语义归并 vs 别名表精确匹配基线 · 对照表",
        "",
        "> 深度落点 (b)：量化模型相对确定性规则的增量。评测集为 200 条脱敏合成标注"
        "（8 条已登记别名、152 条需语义归并、40 条人工标注为不可归类），"
        "标注来源与 basis 见 `assets/eval/labeled_event_names.json`。",
        "",
        f"- 模型路径状态：**{'可用' if result['model_available'] else '不可用'}**"
        + (f"（{result['unavailable_reason']}）" if result["unavailable_reason"] else ""),
        f"- 预测来源：{result['predictions_source'] or '本次未接入真实模型预测'}",
        "",
        "| " + " | ".join(TABLE_COLUMNS) + " |",
        "|---|---|---|---|",
    ]
    for r in rows:
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    lines += ["", "## 结论与边界", ""]
    if result["model_available"]:
        inc = result["model_increment"]
        lines += [
            f"- 模型路径相对基线命中率变化 {inc['hit_rate_delta']:+.2%}，新增明确判定 {inc['newly_decided']} 条。",
            "- 模型输出先过固定 JSON schema，再过院内目录成员性复核；目录外候选一律落「无法归类·待人工指定」。",
            "- 两条路径冲突时不静默取其一，双方证据都留痕交人工（见 engine/severity_matrix.arbitrate）。",
        ]
    else:
        lines += [
            "- **模型不可用，本次无比模型增量**：只出别名表精确匹配基线结果，"
            "不伪造对比数字，也不把基线结果改写成模型成绩。",
            "- 确定性基线可独立出表，正是模型不可达时业务仍跑得完的依据；"
            "接入真实端点后重跑本脚本即可填出模型列。",
        ]
    lines += [
        "",
        "- 评测集是脱敏合成标注，不含真实院内数据；不得据此宣称临床准确率。",
        "- 本地协议替身只出现在测试层（`tests/test_eval_baseline.py`），不构成最终对接方式。",
        "",
        "重跑：`python scripts/eval_merge.py`（离线基线）或 `python scripts/eval_merge.py --model`。",
        "",
    ]
    return "\n".join(lines)


def _try_real_gateway():
    """按 env 配置真实网关；缺配置或不可用时返回 (None, 原因)。"""
    try:
        from ae_qc.attribution.model_gateway import ModelGateway
        from ae_qc.config import require_env
        require_env(["MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME"], env=dict(os.environ))
        return ModelGateway(), ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def run(*, use_model: bool = False, predictions_path: str | None = None,
        predictions_source: str = "", gateway: Any = None,
        output: str | Path | None = None, items: list[dict] | None = None) -> dict[str, Any]:
    """可重跑的评测主流程，返回对比结果 dict（测试直接调用）。"""
    items = items if items is not None else load_items()
    aliases, categories = load_aliases(), load_categories()
    baseline = baseline_predictions(items, aliases)
    model, available, reason, source = None, False, "", predictions_source

    if predictions_path:
        payload = json.loads(Path(predictions_path).read_text(encoding="utf-8"))
        model = list(payload["predictions"] if isinstance(payload, dict) else payload)
        available = True
        source = source or (payload.get("source", "外部提供的真实模型预测") if isinstance(payload, dict)
                            else "外部提供的真实模型预测")
    elif gateway is not None:
        model, ok, why = model_predictions(items, gateway, aliases, categories)
        available, reason = ok, why
        source = source or "注入的模型网关（测试层替身或真实端点）"
    elif use_model:
        gw, why = _try_real_gateway()
        if gw is None:
            reason = why
        else:
            model, available, reason = model_predictions(items, gw, aliases, categories)
            source = source or "环境变量配置的 OpenAI-compatible 端点"
            try:
                gw.close()
            except Exception:
                pass
    else:
        reason = "离线模式：未请求模型端点"

    result = evaluate(items, baseline, model, model_available=available,
                      unavailable_reason=reason, predictions_source=source)
    target = Path(output) if output else DEFAULT_OUTPUT
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_markdown(result), encoding="utf-8")
    result["output_path"] = str(target)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="语义归并 vs 别名基线对照评测（可重跑）")
    parser.add_argument("--model", action="store_true", help="尝试 env 配置的真实模型端点")
    parser.add_argument("--predictions", default=None, help="外部真实模型预测 JSON（离线复算）")
    parser.add_argument("--predictions-source", default="", help="预测来源说明（写入对照表）")
    parser.add_argument("--output", default=None, help="对照表输出路径")
    args = parser.parse_args(argv)
    result = run(use_model=args.model, predictions_path=args.predictions,
                 predictions_source=args.predictions_source, output=args.output)
    print(f"评测集 {result['labeled_count']} 条；基线命中率 {result['baseline']['hit_rate']:.2%}，"
          f"准确率 {result['baseline']['accuracy']:.2%}，不可归类 {result['baseline']['unclassifiable_count']} 条")
    if result["model_available"]:
        print(f"模型路径命中率 {result['model']['hit_rate']:.2%}，准确率 {result['model']['accuracy']:.2%}，"
              f"冲突 {result['conflict_count']} 条")
    else:
        print(f"模型不可用，本次无比模型增量（{result['unavailable_reason']}）；只出基线结果，未伪造对比数字")
    print(f"对照表已写入 {result['output_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
