"""任务 49：目录新增文件触发一次跑批（计数断言）；平台轮询间隔生效；
无 pystray 时降级 window_only 且启动日志 / 说明文案齐全；重启后待确认数可恢复。
"""
from __future__ import annotations

from pathlib import Path

from ae_qc.desktop import tray as tray_mod
from ae_qc.desktop.tray import TrayController
from ae_qc.desktop.watcher import DirectoryWatcher
from ae_qc.ledger.queries import LedgerQueries
from ae_qc.ledger.store import Store


def test_new_file_triggers_exactly_one_run(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    calls = []
    watcher = DirectoryWatcher(inbox, lambda files: calls.append(list(files)),
                               interval=5.0, clock=lambda: 0.0)
    first = watcher.poll_once()
    assert first["triggered"] is False and watcher.trigger_count == 0
    (inbox / "D01-2026-01.xlsx").write_text("x", encoding="utf-8")
    second = watcher.poll_once()
    assert second["triggered"] is True and watcher.trigger_count == 1
    assert len(calls) == 1 and len(calls[0]) == 1
    third = watcher.poll_once()
    assert third["triggered"] is False and watcher.trigger_count == 1  # 不重复触发
    (inbox / "D02-2026-01.xlsx").write_text("y", encoding="utf-8")
    watcher.poll_once()
    assert watcher.trigger_count == 2 and len(calls) == 2                # 再新增再触发一次


def test_multiple_new_files_in_one_poll_trigger_single_run(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    calls = []
    watcher = DirectoryWatcher(inbox, lambda files: calls.append(list(files)),
                               interval=5.0, clock=lambda: 0.0)
    watcher.poll_once()
    for name in ("a.xlsx", "b.xlsx", "c.xlsx"):
        (inbox / name).write_text("x", encoding="utf-8")
    result = watcher.poll_once()
    assert result["triggered"] is True and watcher.trigger_count == 1
    assert len(calls) == 1 and len(calls[0]) == 3                        # 合并为一次跑批


def test_platform_poll_interval_is_configurable(tmp_path):
    now = [0.0]
    platform_calls = []
    watcher = DirectoryWatcher(tmp_path, None, interval=10.0,
                               platform_callback=lambda: platform_calls.append(now[0]),
                               clock=lambda: now[0])
    assert watcher.platform_poll_once()["due"] is True                    # 首次到点
    assert watcher.platform_poll_count == 1
    now[0] = 4.0
    assert watcher.platform_poll_once()["due"] is False                   # 间隔内不触发
    assert watcher.platform_poll_count == 1
    now[0] = 9.999
    assert watcher.platform_poll_once()["due"] is False
    now[0] = 10.0
    assert watcher.platform_poll_once()["due"] is True                    # 达到间隔
    assert watcher.platform_poll_count == 2
    assert platform_calls == [0.0, 10.0]
    watcher.set_interval(2.0)                                             # 间隔可配
    now[0] = 12.0
    assert watcher.platform_poll_once()["due"] is True
    assert watcher.platform_poll_count == 3


def test_watcher_state_persisted_via_queries(tmp_path, tmp_db):
    q = LedgerQueries(Store(tmp_db))
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    watcher = DirectoryWatcher(inbox, lambda files: None, interval=5.0,
                               queries=q, clock=lambda: 0.0)
    (inbox / "batch.xlsx").write_text("x", encoding="utf-8")
    result = watcher.poll_once()
    assert result["triggered"] is True
    assert watcher.batch_records and all(rec["persisted"] for rec in watcher.batch_records)
    refs = [b["batch_ref"] for b in q.list_batches()]
    assert any(ref.startswith("watch-state-inbox-") for ref in refs)      # 状态确实落台账


def test_restart_recovers_pending_count(tmp_db):
    q1 = LedgerQueries(Store(tmp_db))
    q1.save_batch("D01-2026-01", dept_code="D01", period="2026-01")
    q1.save_draft("D01-2026-01", {"row_key": "R2", "state": "attributed_pending",
                                  "source": "model", "draft_text": "草稿"})
    q1.save_draft("D01-2026-01", {"row_key": "R5", "state": "pending_manual_classification",
                                  "source": "degraded", "draft_text": "降级"})
    assert q1.pending_count() == 2
    # 重启：同一台账新建查询对象与 watcher，待确认数从台账恢复
    q2 = LedgerQueries(Store(tmp_db))
    watcher = DirectoryWatcher(Path(tmp_db).parent, None, queries=q2, clock=lambda: 0.0)
    view = watcher.restore_view()
    assert view["pending_count"] == 2
    assert watcher.pending_count() == 2
    assert any(b["batch_ref"] == "D01-2026-01" for b in view["batches"])
    assert view["pending_writeback_count"] == q2.pending_writeback_count()


def test_pystray_missing_degrades_to_window_only(monkeypatch):
    monkeypatch.setattr(tray_mod, "_probe_pystray", lambda: None)
    controller = TrayController()
    assert controller.mode == "window_only"
    assert controller.started is True
    assert "pystray" in controller.fallback_note and "窗口" in controller.fallback_note
    assert controller.startup_log["mode"] == "window_only"
    assert "pystray" in controller.startup_log["message"]
    # 降级路径可跑：通知不抛、带说明、空闲隐藏
    note = controller.notify_new_batch("D01-2026-01")
    assert note["mode"] == "window_only" and "pystray" in note.get("note", "")
    controller.set_idle()
    assert controller.visible is False
    assert controller.status()["mode"] == "window_only"


def test_tray_mode_when_pystray_available(monkeypatch):
    class FakeIcon:
        def __init__(self):
            self.notified = []

        def notify(self, *args):
            self.notified.append(args)

    icon = FakeIcon()
    monkeypatch.setattr(tray_mod, "_probe_pystray", lambda: object())
    controller = TrayController(icon=icon)
    assert controller.mode == "tray"
    assert "pystray" in controller.notice
    result = controller.notify_new_batch("D01-2026-01")
    assert result["badge"] is True and len(icon.notified) == 1
    assert controller.visible is True
