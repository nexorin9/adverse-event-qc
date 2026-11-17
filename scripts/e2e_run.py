"""端到端全链路一致性回归编排（仅测试/验收层，不是产品业务入口）。

投递目录 → 批次 → 期望结果 → 归因草稿 → 人工确认 → 幂等写回 → 导出整改清单，
逐段把数字打印出来并断言一致。链路核心不变量：
    越界行数 == 台账越界明细行数 == 归因草稿数 == 确认数 == 写回请求数 == 导出行数
且「未确认时出站请求数 = 0」「幂等键可查」「同键重发不产生第二条发现单」。

本地协议替身用 ``writeback.contract.ContractServer``（标准库 http.server，绑
127.0.0.1 回环），由 ``with`` 自行启停，脚本结束必关闭，不连真实生产端点、不
依赖外网。写回凭据从环境变量读（QC_BASE_URL / QC_TOKEN，均指向本地替身）。

``run_e2e()`` 返回分段数字字典，供 scripts/e2e_check.sh 与
tests/test_e2e_consistency.py 共用，避免脚本与测试各写一套口径。
"""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ae_qc.attribution.explain import build_draft  # noqa: E402
from ae_qc.engine.aggregate import aggregate  # noqa: E402
from ae_qc.engine.executor import build_plan, execute_plan  # noqa: E402
from ae_qc.engine.row_locator import locate  # noqa: E402
from ae_qc.engine.ruleset import load_ruleset_for_period  # noqa: E402
from ae_qc.ingest.batch_splitter import split_batches  # noqa: E402
from ae_qc.ingest.xlsx_reader import read_batch  # noqa: E402
from ae_qc.ledger.audit import AuditLog  # noqa: E402
from ae_qc.ledger.idempotency import IdempotencyLedger, idempotency_key  # noqa: E402
from ae_qc.ledger.queries import LedgerQueries  # noqa: E402
from ae_qc.ledger.state_machine import transition  # noqa: E402
from ae_qc.ledger.store import Store, dumps, now_iso  # noqa: E402
from ae_qc.reporting.rectify_list import export_rectify_list  # noqa: E402
from ae_qc.writeback.contract import ContractServer  # noqa: E402
from ae_qc.writeback.qc_client import QCClient  # noqa: E402
from scripts.gen_sample_batches import generate_batches  # noqa: E402

RULESET_VERSION = "v2026-01"


def _load_aliases() -> dict[str, str]:
    path = _ROOT / "catalogs" / "aliases.csv"
    out: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            alias = str(row.get("alias_text") or "").strip()
            code = str(row.get("category_code") or "").strip()
            if alias and code:
                out[alias] = code
    return out


@contextmanager
def _local_writeback_env(server: ContractServer) -> Iterator[None]:
    """把出站凭据指向本地协议替身；退出时恢复，绝不污染调用方 os.environ。"""
    saved = {k: os.environ.get(k) for k in ("QC_BASE_URL", "QC_TOKEN")}
    os.environ["QC_BASE_URL"] = server.base_url
    os.environ["QC_TOKEN"] = "e2e-local-token"
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_e2e(*, work_dir: str | Path | None = None, rows: int = 12) -> dict[str, Any]:
    """跑通全链路并返回逐段数字；任一环数字不一致即抛 AssertionError。"""
    wd = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="ae_qc_e2e_"))
    inbox = wd / "inbox"
    out = wd / "out"
    out.mkdir(parents=True, exist_ok=True)
    db = wd / "ledger.sqlite"

    # ① 生成样例（脱敏，1 科室 × 1 期间，行数可控）
    stats = generate_batches(inbox_dir=inbox, depts=["D01"], periods=["2026-01"],
                             rows_per_sheet=rows)
    generated_rows = int(stats["rows"])

    # ② 读表 → 切批
    files = sorted(inbox.glob("*/*.xlsx"))
    assert files, "生成样例后投递目录无 xlsx"
    frames = [read_batch(p) for p in files]
    split = split_batches(frames)
    batches = split["batches"]
    assert len(batches) == 1, f"期望 1 个可用批次，实得 {len(batches)}"
    batch = batches[0]
    rows_data = batch["rows"]
    batch_ref = batch["batch_ref"]
    valid_rows = len(rows_data)
    assert valid_rows == generated_rows, "生成行数与读入有效行数不一致"

    # ③ 跑批：engine.executor 是执行真源，aggregate 是唯一聚合口径
    ruleset, version = load_ruleset_for_period(batch["period"], locked=RULESET_VERSION)
    columns = sorted({k for r in rows_data for k in r if not str(k).startswith("_")})
    plan = build_plan(ruleset, columns)
    executed = execute_plan(plan, rows_data,
                            {"ruleset_version": version, "aliases": _load_aliases()})
    results = executed["results"]
    agg = aggregate(results, rows_data)
    unexpected_rows = int(agg["total_unexpected_rows"])
    evidence: list[dict[str, Any]] = []
    for res in results:
        for ev in locate(res, rows_data):
            record = dict(ev)
            record["basis"] = res.get("basis", "")
            record["batch_ref"] = batch_ref
            evidence.append(record)
    assert len(evidence) == unexpected_rows, "行级证据数与聚合越界行数不一致"

    # 台账落库（投递 → 台账，供导出复用同一份全量明细）
    store = Store(str(db))
    store.initialize()
    queries = LedgerQueries(store)
    queries.save_batch(batch_ref, dept_code=batch["dept_code"], period=batch["period"],
                       source=batch.get("source", "file"), rule_set_version=version,
                       valid_rows=valid_rows, state="validated")
    for res in results:
        queries.save_result(batch_ref, res)
    for ev in evidence:
        queries.save_evidence(batch_ref, ev.get("rule_id", ""), ev)

    # ④ 归因草稿：每行一条（模型不可用→确定性规则基线，不发任何网络请求），
    #    草稿 state 恒 attributed_pending，人工确认是唯一放行口
    drafts: list[dict[str, Any]] = []
    for ev in evidence:
        row_for_draft = dict(ev.get("raw_values") or {})
        row_for_draft["_row_key"] = ev["row_key"]
        drafts.append(build_draft(row_for_draft, gateway=None).to_dict())
    attribution_drafts = len(drafts)
    assert attribution_drafts == unexpected_rows, "归因草稿数与越界行数不一致"
    assert all(d["state"] == "attributed_pending" for d in drafts), "草稿状态必须待人工确认"

    # 构造发现单（spec 14 字段 + 仅用于算幂等键的 row_key 元字段）
    findings: list[dict[str, Any]] = []
    for i, (ev, draft) in enumerate(zip(evidence, drafts)):
        # time_order 等规则按 columns_for 会生成多个计划项（如 occur_time / report_time），
        # 同一 rule_id + 同一原始 row_key 会出现多条证据。幂等键公式是
        # sha256(batch_ref|rule_id|row_key)，故此处把列与序号并入 row_key 保证逐条唯一，
        # 避免同键被幂等短路而少发写回请求（真实系统一条发现单对应一个幂等键）。
        unique_row_key = f"{ev['row_key']}#{ev.get('column', '')}#{i}"
        findings.append({
            "finding_id": f"EX-F-{i:04d}",
            "batch_ref": batch_ref,
            "dept_code": ev["dept_code"],
            "period": ev["period"],
            "rule_id": ev["rule_id"],
            "rule_set_version": version,
            "observed_value": ev["present_value"],
            "unexpected_count": 1,
            "unexpected_samples": [{"row_no": ev["row_no"],
                                    "value": ev["present_value"],
                                    "expected": ev["expected_value"]}],
            "attribution_draft": draft["draft_text"],
            "attribution_confidence": float(draft["confidence"]),
            "status": "attributed_pending",
            "confirmed_by": "",
            "confirmed_at": "",
            "row_key": unique_row_key,
        })
    # 发现单入库（状态机迁移需要底表行）
    for f in findings:
        key = idempotency_key(batch_ref, f["rule_id"], f["row_key"])
        store.execute(
            "INSERT INTO findings(finding_id,batch_ref,dept_code,period,rule_id,"
            "rule_set_version,row_key,status,body_json,idempotency_key,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f["finding_id"], f["batch_ref"], f["dept_code"], f["period"], f["rule_id"],
             f["rule_set_version"], f["row_key"], "attributed_pending",
             dumps(f), key, now_iso()),
        )

    # ⑤⑥ 本地协议替身 + 人工确认 + 幂等写回 + 导出
    with ContractServer("success", receipt_id="EX-R-E2E") as server, \
            _local_writeback_env(server):
        with QCClient(store=store) as client:  # 凭据读环境变量（指向本地替身）
            # 未确认态：前置校验拒绝出站，出站请求数必须为 0
            for f in findings:
                outcome = client.post_finding(f, row_key=f["row_key"])
                assert outcome["ok"] is False, "未确认发现单不得出站"
                assert outcome["code"] == "WRITEBACK_NOT_CONFIRMED", outcome
            outbound_before_confirm = client.request_count
            assert outbound_before_confirm == 0, "未确认时出站请求数必须为 0"

            # 人工确认：attributed_pending → confirmed（状态机合法迁移 + 审计留痕）
            confirmed = 0
            for f in findings:
                tr = transition(store, f["finding_id"], "attributed_pending", "confirmed",
                                actor="示例质控员", batch_ref=batch_ref)
                assert tr["legal"] is True
                f["status"] = "confirmed"
                f["confirmed_by"] = tr["confirmed_by"] or "示例质控员"
                f["confirmed_at"] = tr["confirmed_at"] or now_iso()
                AuditLog(store).confirmation(batch_ref, f["finding_id"],
                                             f["confirmed_by"], f["confirmed_at"])
                confirmed += 1
            assert confirmed == attribution_drafts, "确认数与草稿数不一致"

            # 幂等写回：每条确认单一次成功 POST（本地替身按 spec 校验 body）
            for f in findings:
                outcome = client.post_finding(f, row_key=f["row_key"])
                assert outcome["ok"] is True, outcome
                assert outcome["status"] == 201, outcome
            writeback_requests = client.request_count
            assert writeback_requests == confirmed, "写回请求数与确认数不一致"

            # 幂等重发：同键短路，不产生第二条发现单 / 不新增出站请求
            again = client.post_finding(findings[0], row_key=findings[0]["row_key"])
            assert again["deduplicated"] is True, again
            assert client.request_count == writeback_requests, "幂等重发不应新增请求"

        assert len(server.seen) == confirmed, "替身收到的请求数与确认数不一致"
        assert server.violations == [], "出站 body 不应有协议违规"

        # 幂等键可查
        ledger = IdempotencyLedger(store)
        keys = [idempotency_key(batch_ref, f["rule_id"], f["row_key"]) for f in findings]
        assert len(set(keys)) == len(findings), "幂等键应逐条唯一"
        idempotency_keys_found = sum(1 for k in set(keys) if ledger.get(k) is not None)
        assert idempotency_keys_found == writeback_requests, "幂等键应逐条可查"

    # 本地替身必须已停（不得常驻后台进程）
    server_stopped = not server._thread.is_alive()
    assert server_stopped, "本地协议替身线程未关闭"

    # 导出整改清单：行数 == 全量越界行数（复用 reporting，且从台账读全量明细）
    rectify_rows = queries.unexpected_rows(batch_ref=batch_ref)
    assert len(rectify_rows) == unexpected_rows, "台账越界明细行数与越界行数不一致"
    meta = export_rectify_list(rectify_rows, out / f"整改清单_{batch_ref}.xlsx")
    export_rows = int(meta["row_count"])
    assert export_rows == unexpected_rows, "导出行数与越界行数不一致"

    numbers: dict[str, Any] = {
        "generated_rows": generated_rows,
        "valid_rows": valid_rows,
        "batches": len(batches),
        "batch_ref": batch_ref,
        "rule_set_version": version,
        "executed_rules": len(results),
        "unexpected_rows": unexpected_rows,
        "ledger_evidence_rows": len(rectify_rows),
        "attribution_drafts": attribution_drafts,
        "confirmed": confirmed,
        "outbound_before_confirm": outbound_before_confirm,
        "writeback_requests": writeback_requests,
        "idempotency_keys_found": idempotency_keys_found,
        "export_rows": export_rows,
        "server_stopped": server_stopped,
    }
    # 逐段数字一致性总断言（真实断言，禁止占位）
    chain = [unexpected_rows, len(rectify_rows), attribution_drafts, confirmed,
             writeback_requests, export_rows]
    assert len(set(chain)) == 1, f"链路数字不一致：{chain}"
    assert generated_rows == valid_rows
    assert outbound_before_confirm == 0
    assert idempotency_keys_found == writeback_requests
    numbers["consistent"] = True
    numbers["chain"] = chain
    store.close()
    return numbers


def _print_numbers(n: dict[str, Any]) -> None:
    print("===== 端到端一致性：逐段数字 =====")
    labels = [
        ("生成行数（投递目录）", "generated_rows"),
        ("读入有效行数", "valid_rows"),
        ("切出批次数", "batches"),
        ("执行规则数", "executed_rules"),
        ("越界行数（引擎聚合）", "unexpected_rows"),
        ("台账越界明细行数", "ledger_evidence_rows"),
        ("归因草稿数", "attribution_drafts"),
        ("人工确认数", "confirmed"),
        ("未确认时出站请求数", "outbound_before_confirm"),
        ("确认后写回请求数", "writeback_requests"),
        ("可查幂等键数", "idempotency_keys_found"),
        ("导出整改清单行数", "export_rows"),
        ("本地替身已关闭", "server_stopped"),
    ]
    for label, key in labels:
        print(f"  {label}: {n[key]}")
    print(f"  批次={n['batch_ref']}  套件={n['rule_set_version']}")
    print(f"  链路数字（越界=台账=草稿=确认=写回=导出）: {n['chain']}")
    print(f"  逐段一致: {n['consistent']}")
    print("===== e2e 通过，退出码 0 =====")


def main(argv: list[str] | None = None) -> int:
    with tempfile.TemporaryDirectory(prefix="ae_qc_e2e_") as d:
        numbers = run_e2e(work_dir=d)
    _print_numbers(numbers)
    print(json.dumps(numbers, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
