from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mdqc.ipc.runtime import RuntimeFile, RuntimeInfo, generate_token


def _info(port: int = 1234, token: str = "abc", pid: int = 4242) -> RuntimeInfo:
    return RuntimeInfo(
        port=port,
        token=token,
        pid=pid,
        started_at=datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
    )


def test_round_trip_write_read(tmp_path: Path) -> None:
    rf = RuntimeFile(tmp_path / "runtime.json")
    info = _info()
    rf.write(info)
    out = rf.read()
    assert out is not None
    assert out.port == info.port
    assert out.token == info.token
    assert out.pid == info.pid
    assert out.started_at == info.started_at


def test_read_missing_returns_none(tmp_path: Path) -> None:
    rf = RuntimeFile(tmp_path / "absent.json")
    assert rf.read() is None


def test_read_unparseable_returns_none(tmp_path: Path) -> None:
    target = tmp_path / "runtime.json"
    target.write_text("{not json", encoding="utf-8")
    rf = RuntimeFile(target)
    assert rf.read() is None


def test_clear_deletes_file(tmp_path: Path) -> None:
    rf = RuntimeFile(tmp_path / "runtime.json")
    rf.write(_info())
    assert rf.path.exists()
    rf.clear()
    assert not rf.path.exists()
    rf.clear()  # idempotent


def test_wait_for_returns_when_written(tmp_path: Path) -> None:
    target = tmp_path / "runtime.json"
    rf = RuntimeFile(target)

    def _writer() -> None:
        time.sleep(0.4)
        RuntimeFile(target).write(_info(port=9999))

    thread = threading.Thread(target=_writer)
    thread.start()
    try:
        info = rf.wait_for(timeout_s=5.0)
        assert info.port == 9999
    finally:
        thread.join()


def test_wait_for_raises_timeout(tmp_path: Path) -> None:
    rf = RuntimeFile(tmp_path / "absent.json")
    with pytest.raises(TimeoutError):
        rf.wait_for(timeout_s=0.5)


def test_atomic_write_no_tmp_left(tmp_path: Path) -> None:
    target = tmp_path / "runtime.json"
    rf = RuntimeFile(target)
    rf.write(_info())
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_token_rotation_overwrites_cleanly(tmp_path: Path) -> None:
    target = tmp_path / "runtime.json"
    rf = RuntimeFile(target)
    rf.write(_info(port=1, token="old"))
    rf.write(_info(port=2, token="new"))
    out = rf.read()
    assert out is not None
    assert out.token == "new"
    assert out.port == 2


def test_generate_token_is_unique() -> None:
    seen = {generate_token() for _ in range(20)}
    assert len(seen) == 20
    for tok in seen:
        assert len(tok) >= 32


def test_write_handles_non_dict_payload(tmp_path: Path) -> None:
    target = tmp_path / "runtime.json"
    target.write_text(json.dumps([1, 2]), encoding="utf-8")
    rf = RuntimeFile(target)
    assert rf.read() is None
