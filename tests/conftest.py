"""Shared pytest fixtures.

The MDQC_DATA_DIR env var redirects all data paths to a tmp dir for the test
session, so we never accidentally read or write %PROGRAMDATA% during testing.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Generator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolate_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Generator[Path, None, None]:
    """Redirect mdqc.config.paths.data_dir() to a session-scoped tmp dir."""
    tmp = tmp_path_factory.mktemp("mdqc_data")
    old = os.environ.get("MDQC_DATA_DIR")
    os.environ["MDQC_DATA_DIR"] = str(tmp)
    try:
        yield tmp
    finally:
        if old is None:
            os.environ.pop("MDQC_DATA_DIR", None)
        else:
            os.environ["MDQC_DATA_DIR"] = old


@pytest.fixture
def tmp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test isolated data dir."""
    monkeypatch.setenv("MDQC_DATA_DIR", str(tmp_path))
    return tmp_path


def _is_windows() -> bool:
    return sys.platform == "win32"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-skip windows_only tests on non-Windows."""
    if _is_windows():
        return
    skip_win = pytest.mark.skip(reason="Windows-only")
    for item in items:
        if "windows_only" in item.keywords:
            item.add_marker(skip_win)


@pytest.fixture(autouse=True)
def _isolate_single_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each test its own agent/tray mutex names.

    The real names are PC-wide: a test agent would refuse to start while the
    developer's own agent runs, and a mutex one test holds (handles live for
    the process) would block the next test's agent.
    """
    import uuid

    from mdqc import single_instance

    tag = uuid.uuid4().hex
    monkeypatch.setattr(single_instance, "AGENT_MUTEX", f"Local\\MassDynamicsQCTestAgent-{tag}")
    monkeypatch.setattr(single_instance, "TRAY_MUTEX", f"Local\\MassDynamicsQCTestTray-{tag}")
