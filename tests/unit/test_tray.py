from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC
from pathlib import Path
from typing import Any

import httpx
import pytest

from mdqc.ipc.client import IpcClient
from mdqc.ipc.runtime import RuntimeFile, RuntimeInfo
from mdqc.tray import TrayApp, _open_url, parse_sse_stream


class _RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []
        self.aumid = "test"
        self.enabled = True

    def notify(self, title: str, body: str, *, sound: bool = False) -> None:
        self.calls.append((title, body, sound))

    def notify_info(self, title: str, body: str) -> None:
        self.notify(title, body, sound=False)

    def notify_success(self, title: str, body: str) -> None:
        self.notify(title, body, sound=False)

    def notify_warning(self, title: str, body: str) -> None:
        self.notify(title, body, sound=True)

    def notify_error(self, title: str, body: str) -> None:
        self.notify(title, body, sound=True)


def _make_runtime_file(tmp_path: Path, *, populate: bool = False) -> RuntimeFile:
    rf = RuntimeFile(path=tmp_path / "runtime.json")
    if populate:
        from datetime import datetime

        rf.write(
            RuntimeInfo(
                port=12345,
                token="abc-token",
                pid=4321,
                started_at=datetime.now(UTC),
            )
        )
    return rf


def test_open_url_appends_token_query_string() -> None:
    url = _open_url("http://127.0.0.1:5000", "/wizard", "tok123")
    assert url == "http://127.0.0.1:5000/wizard?token=tok123"


def test_open_url_appends_token_when_path_already_has_query() -> None:
    url = _open_url("http://127.0.0.1:5000", "/dashboard?foo=1", "tok")
    assert url == "http://127.0.0.1:5000/dashboard?foo=1&token=tok"


def test_construction_without_runtime_file_initialises_unavailable_state(
    tmp_path: Path,
) -> None:
    notifier = _RecordingNotifier()
    rf = _make_runtime_file(tmp_path, populate=False)
    app = TrayApp(
        runtime_poll_timeout_s=0.05,
        notifier=notifier,  # type: ignore[arg-type]
        runtime_file=rf,
    )
    assert app.client is None
    assert app._service_available is False


def test_construction_with_runtime_file_loads_client(tmp_path: Path) -> None:
    notifier = _RecordingNotifier()
    rf = _make_runtime_file(tmp_path, populate=True)
    captured: list[RuntimeInfo] = []

    def factory(info: RuntimeInfo) -> IpcClient:
        captured.append(info)
        return IpcClient(
            base_url=f"http://127.0.0.1:{info.port}",
            token=info.token,
            runtime_file=rf,
            client=httpx.Client(),
        )

    app = TrayApp(
        runtime_poll_timeout_s=0.05,
        notifier=notifier,  # type: ignore[arg-type]
        runtime_file=rf,
        client_factory=factory,
    )
    info = rf.read()
    assert info is not None
    app._set_client(info)
    assert app.client is not None
    assert app._service_available is True
    assert captured[0].token == "abc-token"


def test_event_extraction_failed_emits_immediate_error_toast(tmp_path: Path) -> None:
    notifier = _RecordingNotifier()
    rf = _make_runtime_file(tmp_path)
    app = TrayApp(
        runtime_poll_timeout_s=0.05,
        notifier=notifier,  # type: ignore[arg-type]
        runtime_file=rf,
    )
    app._on_event("extraction_failed", {"path": "x.raw"})
    assert len(notifier.calls) == 1
    title, body, sound = notifier.calls[0]
    assert title == "Extraction failed"
    assert body == "x.raw"
    assert sound is True


def test_event_upload_failed_emits_immediate_error_toast(tmp_path: Path) -> None:
    notifier = _RecordingNotifier()
    rf = _make_runtime_file(tmp_path)
    app = TrayApp(
        runtime_poll_timeout_s=0.05,
        notifier=notifier,  # type: ignore[arg-type]
        runtime_file=rf,
    )
    app._on_event("upload_failed", {"path": "y.raw", "error": "boom"})
    assert len(notifier.calls) == 1
    assert notifier.calls[0][0] == "Upload failed"
    assert notifier.calls[0][2] is True


def test_event_extraction_completed_batches_above_threshold(tmp_path: Path) -> None:
    notifier = _RecordingNotifier()
    rf = _make_runtime_file(tmp_path)
    app = TrayApp(
        runtime_poll_timeout_s=0.05,
        notifier=notifier,  # type: ignore[arg-type]
        runtime_file=rf,
    )
    app.batcher.window_s = 0.05
    app.batcher.threshold = 3
    for i in range(5):
        app._on_event("extraction_completed", {"path": f"file_{i}.raw"})
    time.sleep(0.2)
    assert len(notifier.calls) == 1
    title, body, _sound = notifier.calls[0]
    assert "5" in title or "5" in body
    app.batcher.close()


def test_event_extraction_completed_below_threshold_emits_individual(
    tmp_path: Path,
) -> None:
    notifier = _RecordingNotifier()
    rf = _make_runtime_file(tmp_path)
    app = TrayApp(
        runtime_poll_timeout_s=0.05,
        notifier=notifier,  # type: ignore[arg-type]
        runtime_file=rf,
    )
    app.batcher.window_s = 0.05
    app.batcher.threshold = 3
    app._on_event("extraction_completed", {"path": "file.raw"})
    time.sleep(1.0)
    assert len(notifier.calls) == 1
    assert notifier.calls[0][0] == "Extraction completed"
    app.batcher.close()


def test_event_paused_resumed_emit_info_toasts(tmp_path: Path) -> None:
    notifier = _RecordingNotifier()
    rf = _make_runtime_file(tmp_path)
    app = TrayApp(
        runtime_poll_timeout_s=0.05,
        notifier=notifier,  # type: ignore[arg-type]
        runtime_file=rf,
    )
    app._on_event("paused", {})
    app._on_event("resumed", {})
    titles = [c[0] for c in notifier.calls]
    assert titles == ["Paused", "Resumed"]
    assert all(c[2] is False for c in notifier.calls)


def test_event_update_available_includes_version(tmp_path: Path) -> None:
    notifier = _RecordingNotifier()
    rf = _make_runtime_file(tmp_path)
    app = TrayApp(
        runtime_poll_timeout_s=0.05,
        notifier=notifier,  # type: ignore[arg-type]
        runtime_file=rf,
    )
    app._on_event("update_available", {"version": "1.2.3"})
    assert len(notifier.calls) == 1
    assert "1.2.3" in notifier.calls[0][1]
    assert notifier.calls[0][2] is True


def _bytes_iter(chunks: list[bytes]) -> Iterator[bytes]:
    yield from chunks


def test_sse_parser_emits_event_with_data() -> None:
    chunks = [b"event: extraction_failed\ndata: {\"path\": \"x.raw\"}\n\n"]
    events = list(parse_sse_stream(_bytes_iter(chunks)))
    assert events == [("extraction_failed", {"path": "x.raw"})]


def test_sse_parser_handles_message_default_event() -> None:
    chunks = [b"data: {\"x\": 1}\n\n"]
    events = list(parse_sse_stream(_bytes_iter(chunks)))
    assert events == [("message", {"x": 1})]


def test_sse_parser_handles_split_chunks() -> None:
    chunks = [
        b"event: foo\nda",
        b"ta: {\"x\":",
        b" 1}\n",
        b"\n",
    ]
    events = list(parse_sse_stream(_bytes_iter(chunks)))
    assert events == [("foo", {"x": 1})]


def test_sse_parser_ignores_comment_lines() -> None:
    chunks = [b": ping\nevent: heartbeat\ndata: {}\n\n"]
    events = list(parse_sse_stream(_bytes_iter(chunks)))
    assert events == [("heartbeat", {})]


def test_sse_parser_handles_multiple_events_in_one_chunk() -> None:
    chunks = [
        b"event: a\ndata: {\"i\": 1}\n\nevent: b\ndata: {\"i\": 2}\n\n",
    ]
    events = list(parse_sse_stream(_bytes_iter(chunks)))
    assert events == [("a", {"i": 1}), ("b", {"i": 2})]


def test_sse_parser_handles_non_json_data() -> None:
    chunks = [b"data: not-json\n\n"]
    events = list(parse_sse_stream(_bytes_iter(chunks)))
    assert events == [("message", {"raw": "not-json"})]


def test_sse_loop_reconnects_after_disconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    notifier = _RecordingNotifier()
    rf = _make_runtime_file(tmp_path, populate=True)
    info = rf.read()
    assert info is not None

    app = TrayApp(
        runtime_poll_timeout_s=0.05,
        notifier=notifier,  # type: ignore[arg-type]
        runtime_file=rf,
        client_factory=lambda i: IpcClient(
        base_url=f"http://127.0.0.1:{i.port}",
        token=i.token,
        runtime_file=rf,
        client=httpx.Client(),
    ),
    )
    app._set_client(info)

    attempt_count = {"n": 0}
    backoff_waits: list[float] = []

    monkeypatch.setattr(
        "mdqc.tray._BACKOFF_SCHEDULE_S",
        (0.01, 0.02, 0.04),
    )

    real_wait = app._stop.wait

    def recording_wait(timeout: float | None = None) -> bool:
        if timeout is not None and timeout < 1.0:
            backoff_waits.append(timeout)
        return real_wait(timeout)

    monkeypatch.setattr(app._stop, "wait", recording_wait)

    class _FakeStream:
        def __init__(self, kind: str) -> None:
            self._kind = kind
            self.status_code = 200

        def __enter__(self) -> _FakeStream:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self) -> Iterator[bytes]:
            if self._kind == "fail":
                raise httpx.ReadError("boom")
            elif self._kind == "ok":
                yield b"event: extraction_failed\ndata: {\"path\": \"r.raw\"}\n\n"
                app._stop.set()

    def fake_stream(method: str, url: str, **kwargs: Any) -> _FakeStream:
        attempt_count["n"] += 1
        if attempt_count["n"] == 1:
            return _FakeStream("fail")
        return _FakeStream("ok")

    monkeypatch.setattr(httpx, "stream", fake_stream)

    thread = threading.Thread(target=app._sse_loop, daemon=True)
    thread.start()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert attempt_count["n"] >= 2
    assert backoff_waits, "expected at least one backoff sleep after disconnect"
    assert backoff_waits[0] in (0.01, 0.02, 0.04)
    assert any(c[0] == "Extraction failed" for c in notifier.calls)


def test_sse_loop_re_reads_runtime_on_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime

    notifier = _RecordingNotifier()
    rf = _make_runtime_file(tmp_path, populate=True)
    info = rf.read()
    assert info is not None

    rotated = RuntimeInfo(
        port=info.port,
        token="rotated-token",
        pid=info.pid,
        started_at=datetime.now(UTC),
    )

    app = TrayApp(
        runtime_poll_timeout_s=0.05,
        notifier=notifier,  # type: ignore[arg-type]
        runtime_file=rf,
        client_factory=lambda i: IpcClient(
        base_url=f"http://127.0.0.1:{i.port}",
        token=i.token,
        runtime_file=rf,
        client=httpx.Client(),
    ),
    )
    app._set_client(info)

    monkeypatch.setattr(
        "mdqc.tray._BACKOFF_SCHEDULE_S",
        (0.01,),
    )

    state = {"served_401": False}

    class _FakeStream:
        def __init__(self, status: int, body: bytes) -> None:
            self.status_code = status
            self._body = body

        def __enter__(self) -> _FakeStream:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("err", request=None, response=None)  # type: ignore[arg-type]

        def iter_bytes(self) -> Iterator[bytes]:
            yield self._body
            app._stop.set()

    def fake_stream(method: str, url: str, **kwargs: Any) -> _FakeStream:
        if not state["served_401"]:
            state["served_401"] = True
            rf.write(rotated)
            return _FakeStream(401, b"")
        return _FakeStream(200, b"event: paused\ndata: {}\n\n")

    monkeypatch.setattr(httpx, "stream", fake_stream)

    thread = threading.Thread(target=app._sse_loop, daemon=True)
    thread.start()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert app._info is not None
    assert app._info.token == "rotated-token"


def test_runtime_poll_loop_updates_client_when_token_rotates(
    tmp_path: Path,
) -> None:
    from datetime import datetime

    notifier = _RecordingNotifier()
    rf = _make_runtime_file(tmp_path, populate=True)
    info1 = rf.read()
    assert info1 is not None

    factory_calls: list[str] = []

    def factory(i: RuntimeInfo) -> IpcClient:
        factory_calls.append(i.token)
        return IpcClient(
            base_url=f"http://127.0.0.1:{i.port}",
            token=i.token,
            runtime_file=rf,
            client=httpx.Client(),
        )

    app = TrayApp(
        runtime_poll_timeout_s=0.05,
        notifier=notifier,  # type: ignore[arg-type]
        runtime_file=rf,
        client_factory=factory,
    )
    app._set_client(info1)
    assert factory_calls == ["abc-token"]

    new_info = RuntimeInfo(
        port=info1.port,
        token="new-token",
        pid=info1.pid,
        started_at=datetime.now(UTC),
    )
    rf.write(new_info)
    refreshed = app._refresh_runtime()
    assert refreshed is not None
    assert refreshed.token == "new-token"
    assert factory_calls == ["abc-token", "new-token"]


def test_open_path_no_runtime_does_not_open_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notifier = _RecordingNotifier()
    rf = _make_runtime_file(tmp_path, populate=False)
    app = TrayApp(
        runtime_poll_timeout_s=0.05,
        notifier=notifier,  # type: ignore[arg-type]
        runtime_file=rf,
    )
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    app.open_path("/wizard")
    time.sleep(0.05)
    assert opened == []


def test_open_path_with_runtime_opens_browser_with_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notifier = _RecordingNotifier()
    rf = _make_runtime_file(tmp_path, populate=True)
    app = TrayApp(
        runtime_poll_timeout_s=0.05,
        notifier=notifier,  # type: ignore[arg-type]
        runtime_file=rf,
    )
    info = rf.read()
    assert info is not None
    app._set_client(info)

    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    app.open_path("/dashboard")
    time.sleep(0.1)
    assert len(opened) == 1
    assert opened[0].endswith("/dashboard?token=abc-token")
    assert opened[0].startswith(f"http://127.0.0.1:{info.port}")


def test_pause_calls_ipc_client(tmp_path: Path) -> None:
    notifier = _RecordingNotifier()
    rf = _make_runtime_file(tmp_path, populate=True)
    info = rf.read()
    assert info is not None

    pause_calls = {"n": 0}

    class _FakeClient:
        def __init__(self, info: RuntimeInfo) -> None:
            self.info = info
            self.token = info.token
            self.port = info.port

        def pause(self) -> None:
            pause_calls["n"] += 1

        def resume(self) -> None:
            pass

        def close(self) -> None:
            pass

    app = TrayApp(
        runtime_poll_timeout_s=0.05,
        notifier=notifier,  # type: ignore[arg-type]
        runtime_file=rf,
        client_factory=lambda i: _FakeClient(i),  # type: ignore[arg-type, return-value]
    )
    app._set_client(info)
    app._call_pause()
    assert pause_calls["n"] == 1


@pytest.mark.windows_only
def test_run_smoke_windows() -> None:
    pytest.skip("Manual smoke test only — pystray.run() blocks the main thread.")
