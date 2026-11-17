"""全项目共享 pytest fixture。

所有样例均为脱敏占位数据（示例科室 / <EXAMPLE_LOT> 风格），不含姓名、住院号、
诊断明细或拟真批次号；仅作测试输入，不反向定义产品架构。
"""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))  # 允许 import scripts.* / 项目根模块

# 本机沙箱对「已存在目录 + mkdir(exist_ok=True)」会抛 EEXIST，pytest 内置
# tmp_path 的固定 basetemp 因此不可用。每次会话给一个全新临时根，保证 basetemp
# 首次创建时路径不存在；不改变测试的 tmp_path 语义。
os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", tempfile.mkdtemp(prefix="ae_qc_pytest_"))


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def catalogs_dir() -> Path:
    return PROJECT_ROOT / "catalogs"


@pytest.fixture(scope="session")
def aliases(catalogs_dir: Path) -> dict[str, str]:
    path = catalogs_dir / "aliases.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return {r["alias_text"].strip(): r["category_code"].strip() for r in csv.DictReader(fh)}


@pytest.fixture(scope="session")
def categories(catalogs_dir: Path) -> list[dict[str, Any]]:
    return json.loads((catalogs_dir / "event_categories.json").read_text(encoding="utf-8"))["categories"]


@pytest.fixture(scope="session")
def severity_levels(catalogs_dir: Path) -> list[dict[str, Any]]:
    return json.loads((catalogs_dir / "severity_levels.json").read_text(encoding="utf-8"))["levels"]


def _row(no: int, **overrides: Any) -> dict[str, Any]:
    base = {
        "incident_id": f"EXAMPLE-{no:04d}",
        "dept_code": "D01",
        "event_type_text": "跌倒",
        "event_type_code": "AE-CAT-08",
        "severity_level": "A",
        "severity_class": "Ⅳ",
        "occur_time": "2026-01-02 10:00:00",
        "report_time": "2026-01-02 11:00:00",
        "reporter_role": "示例安全员",
        "harm_desc": "未累及患者",
        "root_cause_text": "地面湿滑",
        "category_family": "safety",
        "_row_key": f"D01|2026-01|example{no}|{no}",
        "_row_no": no,
        "_dept_code": "D01",
        "_period": "2026-01",
        "_batch_ref": "D01-2026-01",
        "_file_name": "example.xlsx",
        "_sheet_name": "明细",
    }
    base.update(overrides)
    return base


@pytest.fixture
def sample_rows() -> list[dict[str, Any]]:
    """5 行：1 合规 + 4 类越界（等级越界 / 时间倒挂 / 别名未命中 / 派生不一致）。"""
    return [
        _row(1),
        _row(2, severity_level="Z", incident_id="EXAMPLE-0002"),
        _row(3, occur_time="2026-01-05 12:00:00", report_time="2026-01-04 12:00:00"),
        _row(4, event_type_text="未登记的自由文本事件名", event_type_code=""),
        _row(5, severity_level="E", severity_class="Ⅳ", category_family="clinical"),
    ]


@pytest.fixture
def clean_rows() -> list[dict[str, Any]]:
    return [
        _row(1),
        _row(2, incident_id="EXAMPLE-0002", severity_level="B", severity_class="Ⅲ",
             event_type_text="给药错误", event_type_code="AE-CAT-02", category_family="medication"),
        _row(3, incident_id="EXAMPLE-0003", severity_level="I",
             severity_class="Ⅰ", category_family="clinical"),
    ]


@pytest.fixture
def ruleset_v1():
    from ae_qc.engine.ruleset import load_ruleset
    return load_ruleset("v2026-01")


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "ledger.sqlite")


class GatewayStub:
    """测试层协议替身：按队列返回结构化响应，并统计调用次数（仅测试层）。"""

    def __init__(self, responses: list[dict] | None = None, unavailable: bool = False):
        self.responses = list(responses or [])
        self.unavailable = unavailable
        self.calls: list[dict] = []
        self.call_count = 0

    def structured_call(self, messages: list, schema: dict, **kw: Any) -> dict:
        self.call_count += 1
        self.calls.append({"messages": messages, "schema": schema, **kw})
        if self.unavailable:
            return {"ok": False, "data": None, "unavailable": True, "code": "CONN_FAILED",
                    "meta": {"request_id": "stub", "model": "stub", "retries": 0}}
        data = self.responses.pop(0) if self.responses else {"code": "", "confidence": 0.0,
                                                               "reason": "", "unclassifiable": True,
                                                               "missing_info": ["事件经过"]}
        return {"ok": True, "data": data, "unavailable": False, "code": "",
                "meta": {"request_id": f"stub-{self.call_count}", "model": "stub",
                         "elapsed_ms": 1.0, "tokens": 10, "input_count": len(messages),
                         "retries": 0, "code": ""}}


@pytest.fixture
def model_stub() -> GatewayStub:
    return GatewayStub()
