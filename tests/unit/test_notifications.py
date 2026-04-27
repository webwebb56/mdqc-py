from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest

from mdqc.notifications import NotificationBatcher, Notifier, register_aumid


class _RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []
        self.aumid = "test"
        self.enabled = True

    def notify(self, title: str, body: str, *, sound: bool = False) -> None:
        self.calls.append((title, body, sound))


def test_notifier_non_windows_does_not_raise(caplog: pytest.LogCaptureFixture) -> None:
    n = Notifier()
    n.notify("Hello", "World")
    n.notify_info("i", "b")
    n.notify_success("s", "b")
    n.notify_warning("w", "b")
    n.notify_error("e", "b")


def test_notifier_disabled_logs_only() -> None:
    n = Notifier(enabled=False)
    n.notify("disabled", "body")


def test_register_aumid_non_windows_returns_false() -> None:
    if sys.platform == "win32":
        assert register_aumid("MassDynamics.QCAgent") in (True, False)
    else:
        assert register_aumid("MassDynamics.QCAgent") is False


@pytest.mark.asyncio
async def test_batcher_threshold_collapses() -> None:
    rec = _RecordingNotifier()
    batcher = NotificationBatcher(rec, window_s=0.05, threshold=3)  # type: ignore[arg-type]
    for _ in range(5):
        batcher.submit("file_processed", "title", "body", sound=False)
    await asyncio.sleep(0.15)
    assert len(rec.calls) == 1
    title, body, _sound = rec.calls[0]
    assert "5" in title or "5" in body


@pytest.mark.asyncio
async def test_batcher_below_threshold_emits_single() -> None:
    rec = _RecordingNotifier()
    batcher = NotificationBatcher(rec, window_s=0.05, threshold=3)  # type: ignore[arg-type]
    batcher.submit("processing_started", "Started", "file.raw", sound=False)
    await asyncio.sleep(0.15)
    assert len(rec.calls) == 1
    assert rec.calls[0] == ("Started", "file.raw", False)


@pytest.mark.asyncio
async def test_batcher_aclose_cancels_pending_cleanly() -> None:
    rec = _RecordingNotifier()
    batcher = NotificationBatcher(rec, window_s=5.0, threshold=3)  # type: ignore[arg-type]
    batcher.submit("evt", "t", "b")
    batcher.submit("evt2", "t2", "b2")
    await batcher.aclose()
    assert rec.calls == []
    batcher.submit("evt3", "t3", "b3")
    assert rec.calls == []


@pytest.mark.asyncio
async def test_batcher_flush_emits_pending() -> None:
    rec = _RecordingNotifier()
    batcher = NotificationBatcher(rec, window_s=5.0, threshold=3)  # type: ignore[arg-type]
    batcher.submit("evt", "title", "body")
    await batcher.flush()
    assert len(rec.calls) == 1
    await batcher.aclose()


def test_notifier_helpers_severity_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, str, bool]] = []

    def fake_notify(self: Any, title: str, body: str, *, sound: bool = False) -> None:
        captured.append((title, body, sound))

    monkeypatch.setattr(Notifier, "notify", fake_notify)
    n = Notifier()
    n.notify_info("a", "b")
    n.notify_success("a", "b")
    n.notify_warning("a", "b")
    n.notify_error("a", "b")
    sounds = [c[2] for c in captured]
    assert sounds == [False, False, True, True]
