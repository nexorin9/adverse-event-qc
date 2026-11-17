"""任务 23：批次级断点续跑与可重入执行。

覆盖：中断后重跑从断点继续且最终结果与一次跑完一致；同批重复执行幂等
（不增行不翻倍）；断点信息可查可清理；按 plan_id 中断与执行异常隔离后仍可续跑。
"""
from __future__ import annotations

from ae_qc.engine.resume import ResumeState


def _plan(n: int = 6) -> dict:
    return {"items": [
        {"plan_id": f"P{i:03d}", "rule_id": f"R{i}", "column": "severity_level"}
        for i in range(1, n + 1)
    ]}


def _result(item: dict) -> dict:
    return {
        "plan_id": item["plan_id"], "rule_id": item["rule_id"],
        "success": True, "unexpected_count": 0, "status": "executed",
    }


def _expected(plan: dict) -> list[dict]:
    return [_result(it) for it in plan["items"]]


def _execute_fn(counter: list):
    def fn(item, rows, context):
        counter.append(item["plan_id"])
        return _result(item)
    return fn


def test_resume_from_checkpoint_matches_single_run(tmp_path):
    st = ResumeState(root=tmp_path / "resume1")
    plan = _plan(6)
    ids = [it["plan_id"] for it in plan["items"]]
    counter: list = []
    fn = _execute_fn(counter)

    first = st.run_resumable("B1", plan, [], {}, fn, failure_at=2)
    assert first["interrupted"] is True
    assert first["summary"]["done"] == 2
    assert first["executed_now"] == ["P001", "P002"]
    assert counter == ["P001", "P002"]
    assert st.remaining("B1", ids) == ids[2:]
    assert "中断" in st.state("B1", ids)["last_failure"]

    second = st.run_resumable("B1", plan, [], {}, fn, failure_at=None)
    assert second["interrupted"] is False
    assert second["results"] == _expected(plan)  # 与一次跑完逐条一致
    assert second["executed_now"] == ids[2:]     # 从断点继续
    assert second["summary"]["from_checkpoint"] == 2
    assert counter == ids                        # 每条只执行一次
    assert st.remaining("B1", ids) == []


def test_repeated_execution_is_idempotent(tmp_path):
    st = ResumeState(root=tmp_path / "resume2")
    plan = _plan(4)
    counter: list = []
    fn = _execute_fn(counter)

    r1 = st.run_resumable("B2", plan, [], {}, fn)
    calls_after_first = len(counter)
    r2 = st.run_resumable("B2", plan, [], {}, fn)

    assert len(counter) == calls_after_first      # 同批重复执行不再调用 execute_fn
    assert r2["executed_now"] == []
    assert r2["results"] == r1["results"]         # 不增行
    assert len(r2["results"]) == len(r1["results"]) == 4
    assert sum(x["unexpected_count"] for x in r2["results"]) == \
           sum(x["unexpected_count"] for x in r1["results"])  # 越界计数不翻倍


def test_checkpoint_query_and_clear(tmp_path):
    st = ResumeState(root=tmp_path / "resume3")
    plan = _plan(3)
    ids = [it["plan_id"] for it in plan["items"]]
    st.run_resumable("B3", plan, [], {}, _execute_fn([]), failure_at=1)

    info = st.state("B3", ids)
    assert info["done_count"] == 1
    assert info["remaining"] == ids[1:]
    assert info["last_failure"]

    assert st.clear("B3")["cleared"] is True
    assert st.remaining("B3", ids) == ids          # 清理后从头重跑
    assert st.state("B3", ids)["done_count"] == 0

    counter: list = []
    out = st.run_resumable("B3", plan, [], {}, _execute_fn(counter))
    assert out["results"] == _expected(plan)
    assert counter == ids
    # 重跑后断点文件会重建，可再次清理；清理后又能从头执行
    assert st.clear("B3")["cleared"] is True
    assert st.remaining("B3", ids) == ids


def test_mark_done_idempotent_and_remaining(tmp_path):
    st = ResumeState(root=tmp_path / "resume4")
    st.mark_done("B4", "P001", {"ok": 1})
    st.mark_done("B4", "P001", {"ok": 2})  # 同键覆盖，不增行
    assert st.state("B4")["done_count"] == 1
    assert st.remaining("B4", ["P001", "P002", "P003"]) == ["P002", "P003"]


def test_failure_at_plan_id_string(tmp_path):
    st = ResumeState(root=tmp_path / "resume5")
    plan = _plan(4)
    ids = [it["plan_id"] for it in plan["items"]]
    out = st.run_resumable("B5", plan, [], {}, _execute_fn([]), failure_at="P003")
    assert out["interrupted"] is True
    assert out["executed_now"] == ["P001", "P002"]
    assert st.remaining("B5", ids) == ids[2:]


def test_execute_fn_exception_is_isolated_and_resumable(tmp_path):
    st = ResumeState(root=tmp_path / "resume6")
    plan = _plan(4)

    def bad_fn(item, rows, context):
        if item["plan_id"] == "P002":
            raise RuntimeError("boom")
        return _result(item)

    first = st.run_resumable("B6", plan, [], {}, bad_fn)
    assert first["interrupted"] is True
    assert "boom" in first["summary"]["last_failure"]
    assert first["executed_now"] == ["P001"]

    second = st.run_resumable("B6", plan, [], {}, _execute_fn([]))
    assert second["interrupted"] is False
    assert second["results"] == _expected(plan)
