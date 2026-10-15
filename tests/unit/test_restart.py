"""Restart versus stop, and coming back on the same address."""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace

import pytest

from mdqc.service import lifecycle


@pytest.fixture(autouse=True)
def _no_relaunch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle, "_relaunch_after_exit", False)


def _route(app, path: str):  # type: ignore[no-untyped-def]
    return next(r.endpoint for r in app.routes if getattr(r, "path", None) == path)


def _state(service_mode: bool) -> SimpleNamespace:
    return SimpleNamespace(service_mode=service_mode, restarting=False, stop_event=asyncio.Event())


def _api_for(state: SimpleNamespace):  # type: ignore[no-untyped-def]
    # build_api needs only these attributes for the restart/shutdown routes.
    state.app = None
    state.token = "t"
    return lifecycle.build_api(state)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_restart_at_login_relaunches_itself() -> None:
    state = _state(service_mode=False)
    await _route(_api_for(state), "/api/restart")()
    await asyncio.sleep(0.6)
    assert state.stop_event.is_set()
    assert state.restarting
    assert lifecycle._relaunch_after_exit is True


@pytest.mark.asyncio
async def test_restart_as_a_service_leaves_it_to_the_service_manager() -> None:
    state = _state(service_mode=True)
    await _route(_api_for(state), "/api/restart")()
    await asyncio.sleep(0.6)
    assert state.stop_event.is_set()
    assert lifecycle._relaunch_after_exit is False


@pytest.mark.asyncio
async def test_shutdown_stops_and_stays_stopped() -> None:
    state = _state(service_mode=False)
    await _route(_api_for(state), "/api/shutdown")()
    await asyncio.sleep(0.6)
    assert state.stop_event.is_set()
    assert not state.restarting
    assert lifecycle._relaunch_after_exit is False


def test_a_free_previous_port_is_reused() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert lifecycle._reusable_port(port) == port


def test_a_taken_port_falls_back_to_any() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        assert lifecycle._reusable_port(s.getsockname()[1]) == 0


def test_no_previous_port_means_any() -> None:
    assert lifecycle._reusable_port(0) == 0
