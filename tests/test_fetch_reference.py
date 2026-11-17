"""任务 2：公开来源抓取脚本 — 可重跑 / 断网降级 / 内置副本与正式目录逐条一致。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.fetch_reference_docs import (  # noqa: E402
    BUILTIN_LEVELS, CATALOG_LEVELS, MANIFEST, SOURCES, fetch_all, load_levels,
    main, verify_builtin_matches_catalog,
)


@pytest.fixture(autouse=True)
def _isolate_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """隔离 sha256 清单，避免测试写脏发布目录。"""
    fake = tmp_path / "sha256_manifest.json"
    monkeypatch.setattr("scripts.fetch_reference_docs.MANIFEST", fake)
    yield


def test_builtin_levels_match_catalog_level_by_level(severity_levels):
    check = verify_builtin_matches_catalog()
    assert check["ok"] is True, f"内置副本与正式目录不一致：{check}"
    assert [lv["code"] for lv in severity_levels] == list("ABCDEFGHI")
    builtin = load_levels(BUILTIN_LEVELS)
    assert builtin == severity_levels
    assert all(lv["basis"] and lv["desc"] for lv in builtin)


def test_offline_degrades_successfully_and_exit_code_zero(capsys):
    rc = main(["--offline"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "降级" in out and "内置" in out
    result = fetch_all(offline=True)
    assert result["degraded"] is True
    assert result["reasons"]
    assert result["builtin_match"]["ok"] is True


def test_repeatable_no_duplicate_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    dest = tmp_path / "reference"
    # 用离线演练 + 预置占位 PDF，模拟「第二次执行时已存在且清单有 sha256」
    dest.mkdir(parents=True)
    for src in SOURCES:
        (dest / src["name"]).write_bytes(b"%PDF-1.4 placeholder for repeat-run test" * 20)
    monkeypatch.setattr("scripts.fetch_reference_docs.MANIFEST", dest / "sha256_manifest.json")
    (dest / "sha256_manifest.json").write_text(
        json.dumps({s["name"]: {"sha256": "abc", "bytes": 1} for s in SOURCES}), encoding="utf-8")
    first = fetch_all(offline=True, dest=dest)
    before = sorted(p.name for p in dest.iterdir())
    second = fetch_all(offline=True, dest=dest)
    after = sorted(p.name for p in dest.iterdir())
    assert before == after
    assert set(second["skipped"]) == {s["name"] for s in SOURCES}
    assert second["downloaded"] == []


def test_catalog_levels_are_from_verified_source():
    payload = json.loads(CATALOG_LEVELS.read_text(encoding="utf-8"))
    assert "附件2" in payload["source"]
    assert payload["confidence"] == "已核原文"
    assert payload["class_map"]["Ⅲ"] == ["B", "C", "D"]
