"""任务 17：行内容指纹 / 幂等行键 / 疑似重复 / 指纹冲突挂起。

所有断言针对返回的新行列表；入参不被改动（合并 / 丢弃都是业务判断）。
"""
from __future__ import annotations

from ae_qc.engine import fingerprint as fp


def _row(no: int, **overrides) -> dict:
    base = {
        "incident_id": "EXAMPLE-0001",
        "event_type_text": "跌倒",
        "event_type_code": "AE-CAT-08",
        "severity_level": "A",
        "severity_class": "Ⅳ",
        "occur_time": "2026-01-02 10:00:00",
        "report_time": "2026-01-02 11:00:00",
        "harm_desc": "未累及患者",
        "_row_no": no,
        "_dept_code": "D01",
        "_period": "2026-01",
    }
    base.update(overrides)
    return base


def test_row_key_stable_reproducible_and_well_formed():
    row = _row(3)
    k1 = fp.row_key("D01", "2026-01", row, 7)
    k2 = fp.row_key("D01", "2026-01", row, 7)
    assert k1 == k2  # 同一输入必得同一行键

    parts = k1.split("|")
    assert len(parts) == 4
    assert parts[0] == "D01"
    assert parts[1] == "2026-01"
    assert parts[2] == fp.content_fingerprint(row)
    assert parts[3] == "7"
    assert len(parts[2]) == 16

    # 与行序无关：打乱 dict 的字段插入顺序，指纹不变
    shuffled = {k: row[k] for k in reversed(list(row))}
    assert fp.content_fingerprint(shuffled) == fp.content_fingerprint(row)

    # 不同行号必得不同行键
    assert fp.row_key("D01", "2026-01", row, 8) != k1
    # 未显式给 row_no 时回退到行的 _row_no
    assert fp.row_key("D01", "2026-01", row).endswith("|3")


def test_content_fingerprint_ignores_metadata_and_normalizes():
    a = _row(1, harm_desc=" 未累及患者 ")
    b = _row(2, harm_desc="未累及患者", _file_name="x.xlsx", _sheet_name="明细", _batch_ref="B")
    # 下划线元数据（行号 / 文件名 / 批次）不参与业务内容；前后空格被规范化
    assert fp.content_fingerprint(a) == fp.content_fingerprint(b)

    # 内容真的不同，指纹必须不同
    c = _row(1, harm_desc="患者诉髋部疼痛")
    assert fp.content_fingerprint(c) != fp.content_fingerprint(a)

    # 全角 / 大小写归一后视为同一内容
    d = _row(1, severity_level="ａ")
    e = _row(1, severity_level="A")
    assert fp.content_fingerprint(d) == fp.content_fingerprint(e)


def test_same_content_marked_as_suspected_duplicate():
    rows = [_row(1), _row(2)]  # 同内容、不同行号
    out = fp.mark_duplicates(rows)
    assert len(out) == 2
    assert out[0]["_duplicate"] is True and out[1]["_duplicate"] is True
    assert out[0]["_duplicate_note"] == "疑似重复"
    assert 2 in out[0]["_duplicate_with"]
    assert 1 in out[1]["_duplicate_with"]
    assert "_suspended" not in out[0] and "_suspended" not in out[1]
    # 幂等行键补全
    assert all(r["_row_key"] for r in out)
    # 入参不被改动
    assert "_duplicate" not in rows[0]


def test_fingerprint_conflict_suspended_not_merged_or_dropped():
    a = _row(1)
    b = _row(2, severity_level="Z")
    shared_key = "D01|2026-01|deadbeefdeadbeef|1"
    a["_row_key"] = shared_key
    b["_row_key"] = shared_key
    assert fp.content_fingerprint(a) != fp.content_fingerprint(b)

    out = fp.mark_duplicates([a, b])
    assert len(out) == 2  # 不丢弃任何一行
    assert out[0]["_suspended"] is True and out[1]["_suspended"] is True
    assert "挂起" in out[0]["_suspend_reason"]
    # 挂起交人工：不自动合并、不标疑似重复
    assert "_duplicate" not in out[0] and "_duplicate" not in out[1]
    assert out[0]["_row_key"] == shared_key and out[1]["_row_key"] == shared_key


def test_different_dept_or_period_same_content_not_duplicate():
    # 内容指纹不受科室/期间影响（它们在行键里单列），靠分组键把关
    a = _row(1, dept_code="D01", _dept_code="D01")
    b = _row(2, dept_code="D02", _dept_code="D02")
    assert fp.content_fingerprint(a) == fp.content_fingerprint(b)
    out = fp.mark_duplicates([a, b])
    assert all("_duplicate" not in r for r in out)

    p1 = _row(1, _period="2026-01")
    p2 = _row(2, _period="2026-02")
    out2 = fp.mark_duplicates([p1, p2])
    assert all("_duplicate" not in r for r in out2)


def test_mark_duplicates_is_idempotent_on_second_pass():
    first = fp.mark_duplicates([_row(1), _row(2)])
    second = fp.mark_duplicates(first)
    assert first == second  # 补全行键后二次运行不产生新的重复 / 冲突
    assert second[0]["_duplicate_with"] == [2]
