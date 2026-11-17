"""任务 53：对照评测（语义归并 vs 别名表精确匹配基线）的可复现验收。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import eval_merge as em


class EvalGatewayStub:
    """仅测试层的协议替身：按标注集给出结构化归并响应，用于验证两路径计算逻辑。

    这不是产品模型成绩；真实对照表由 eval_merge.py --model 或 --predictions 产出。
    """

    def __init__(self, items: list[dict]):
        self.by_text = {i["text"]: i for i in items}
        self.call_count = 0

    def structured_call(self, messages: list, schema: dict, **kw) -> dict:
        self.call_count += 1
        content = messages[-1]["content"] if messages else "{}"
        payload = json.loads(content)
        text = payload.get("row", {}).get("event_type_text", "")
        item = self.by_text.get(text, {})
        gold = item.get("gold_code", "UNCLASSIFIABLE")
        unclear = gold == "UNCLASSIFIABLE"
        data = {
            "code": "" if unclear else gold,
            "confidence": 0.0 if unclear else 0.91,
            "reason": "缺少可归属依据" if unclear else f"语义指向 {item.get('gold_category', '')}",
            "unclassifiable": unclear,
            "missing_info": ["事件经过 / 后果描述"] if unclear else [],
        }
        return {"ok": True, "data": data, "unavailable": False, "code": "",
                "meta": {"request_id": kw.get("request_id", ""), "model": "test-stub",
                         "elapsed_ms": 1.0, "tokens": 10, "input_count": 1, "retries": 0}}


def test_labeled_set_has_at_least_200_items_with_basis(project_root):
    payload = json.loads((project_root / "assets/eval/labeled_event_names.json").read_text(encoding="utf-8"))
    assert payload["labeled_count"] >= 200
    items = payload["items"]
    assert len(items) >= 200
    assert len({i["text"] for i in items}) == len(items)
    assert all(i["basis"] and i["label_source"] and i["confidence"] for i in items)
    assert payload["splits"]["unclassifiable"] >= 1
    assert sum(1 for i in items if i["gold_code"] == "UNCLASSIFIABLE") == payload["splits"]["unclassifiable"]


def test_baseline_stands_alone_when_model_unavailable(tmp_path):
    result = em.run(output=tmp_path / "table.md")
    assert result["model_available"] is False
    assert result["model"] is None and result["conflict_count"] is None
    assert result["baseline"]["total"] >= 200
    assert 0.0 <= result["baseline"]["hit_rate"] <= 1.0
    assert result["baseline"]["unclassifiable_count"] > 0
    assert 0.0 <= result["baseline"]["unclassifiable_agreement"] <= 1.0
    text = Path(result["output_path"]).read_text(encoding="utf-8")
    assert "模型不可用，本次无比模型增量" in text


def test_table_columns_are_complete(tmp_path):
    result = em.run(output=tmp_path / "table.md")
    assert result["columns"] == ["指标", "别名表精确匹配基线", "模型语义归并", "说明"]
    text = Path(result["output_path"]).read_text(encoding="utf-8")
    for column in result["columns"]:
        assert column in text
    for metric in ("命中率", "准确率", "不可归类数", "与人工标注一致率", "冲突条数"):
        assert metric in text


def test_two_path_comparison_with_test_double(tmp_path):
    items = em.load_items()
    stub = EvalGatewayStub(items)
    result = em.run(gateway=stub, output=tmp_path / "two-path.md",
                    predictions_source="测试层协议替身（仅验证计算逻辑）")
    assert result["model_available"] is True
    assert result["model"]["total"] == len(items)
    assert result["model"]["hit_rate"] > result["baseline"]["hit_rate"]
    assert result["model_increment"]["newly_decided"] > 0
    assert result["conflict_count"] >= 0
    assert 0.0 <= result["model"]["unclassifiable_agreement"] <= 1.0
    assert stub.call_count > 0  # 别名命中的行不调模型
    assert stub.call_count < len(items)
    text = Path(result["output_path"]).read_text(encoding="utf-8")
    assert "模型路径状态：**可用**" in text


def test_predictions_file_offline_recompute(tmp_path):
    items = em.load_items()
    preds = [{"id": i["id"], "text": i["text"],
              "prediction": None if i["gold_code"] == "UNCLASSIFIABLE" else i["gold_code"],
              "unclassifiable": i["gold_code"] == "UNCLASSIFIABLE", "source": "model"}
             for i in items]
    src = tmp_path / "predictions.json"
    src.write_text(json.dumps({"source": "院内自建模型（外部真实预测样例）", "predictions": preds},
                              ensure_ascii=False), encoding="utf-8")
    result = em.run(predictions_path=src, output=tmp_path / "table.md")
    assert result["model_available"] is True
    assert result["model"]["accuracy"] == 1.0
    assert "院内自建模型" in result["predictions_source"]


def test_script_is_repeatable(tmp_path):
    first = em.run(output=tmp_path / "a.md")
    second = em.run(output=tmp_path / "a.md")
    assert first["baseline"] == second["baseline"]
    assert first["labeled_count"] == second["labeled_count"]
    assert (tmp_path / "a.md").read_text(encoding="utf-8")
