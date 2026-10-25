"""The setup program's moving parts: mdqc stop, start --open, and the .iss."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from mdqc.cli import app, open_cmd, start, stop

ROOT = Path(__file__).resolve().parents[2]
ISS = (ROOT / "installer" / "mdqc.iss").read_text(encoding="utf-8")
runner = CliRunner()


# ── mdqc stop ────────────────────────────────────────────────────────────────


def test_stop_with_nothing_running_ends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MDQC_DATA_DIR", str(tmp_path))
    ended: list[int] = []
    monkeypatch.setattr(stop, "_end", ended.append)
    result = runner.invoke(app, ["stop", "--wait", "0"])
    assert result.exit_code == 0
    assert "No running agent answered" in result.output
    assert ended == []


def test_stop_falls_back_to_restart_on_older_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agents before 1.2 have no /api/shutdown."""
    from datetime import UTC, datetime

    from mdqc.ipc.runtime import RuntimeFile, RuntimeInfo

    monkeypatch.setenv("MDQC_DATA_DIR", str(tmp_path))
    RuntimeFile().write(RuntimeInfo(port=1, token="t", pid=1, started_at=datetime.now(UTC)))
    calls: list[str] = []

    def fake_post(url: str, **_: object) -> httpx.Response:
        calls.append(url.rsplit("/", 1)[-1])
        return httpx.Response(404 if url.endswith("/api/shutdown") else 200)

    monkeypatch.setattr(httpx, "post", fake_post)
    assert stop._ask_agent_to_stop()
    assert calls == ["shutdown", "restart"]


def test_only_the_bundled_runtime_ends_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A venv or system Python is shared; ending everything under it would
    take other programs down."""
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    assert not stop.owns_its_interpreter()
    (tmp_path / stop.RUNTIME_MARKER).write_text("")
    assert stop.owns_its_interpreter()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process list")
def test_processes_are_found_by_their_program_folder() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(0.5)
        exe_dir = Path(sys.executable).resolve().parent
        assert child.pid in stop.processes_under(exe_dir) or child.pid in stop.processes_under(
            Path(sys.base_prefix)
        )
        assert child.pid not in stop.processes_under(ROOT)
    finally:
        child.kill()
        child.wait()


def test_the_marker_name_matches_the_build_script() -> None:
    build = (ROOT / "scripts" / "build_installer.py").read_text(encoding="utf-8")
    assert f'RUNTIME_MARKER = "{stop.RUNTIME_MARKER}"' in build


# ── mdqc start --open ────────────────────────────────────────────────────────


def test_start_open_waits_for_the_agent_then_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([None, None, "http://127.0.0.1:1/dashboard?token=t"])
    opened: list[str] = []
    monkeypatch.setattr(open_cmd, "dashboard_url", lambda page="/dashboard": next(answers))
    monkeypatch.setattr("webbrowser.open_new_tab", opened.append)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    start._open_when_ready(timeout_s=30)
    assert opened == ["http://127.0.0.1:1/dashboard?token=t"]


# ── the Inno Setup script ────────────────────────────────────────────────────


def test_setup_and_install_script_share_the_startup_entry() -> None:
    """So a setup-program install replaces a PowerShell one instead of
    starting a second agent at login."""
    ps1 = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    run_value = re.search(r'#define RunValue "(\w+)"', ISS)
    assert run_value is not None
    assert f'$RunValue = "{run_value.group(1)}"' in ps1


def test_setup_program_sets_the_notification_app_id() -> None:
    from mdqc.config.defaults import AUMID

    assert f'#define Aumid "{AUMID}"' in ISS
    assert 'AppUserModelID: "{#Aumid}"' in ISS


def test_setup_program_starts_and_stops_through_the_cli() -> None:
    assert "-m mdqc start --open" in ISS
    assert "-m mdqc stop" in ISS
    assert "Permissions: users-modify" in ISS


def test_app_id_never_changes() -> None:
    """Upgrades and the uninstall entry are keyed on it."""
    assert "AppId={{6F1B6C1E-3A57-4C4B-9E0C-5D2F8A7B1C40}" in ISS


def test_setup_script_is_ascii() -> None:
    (ROOT / "installer" / "mdqc.iss").read_bytes().decode("ascii")


def test_stop_waits_for_the_agent_process_to_exit() -> None:
    """Not just for it to stop answering: it holds the one-agent lock while
    it closes, and a new agent started in between would exit at once."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
    try:
        assert not stop._wait_for_exit(child.pid, 0.2)
        assert stop._wait_for_exit(child.pid, 10)
    finally:
        child.kill()
        child.wait()


def test_a_process_that_is_gone_counts_as_exited() -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert stop._wait_for_exit(child.pid, 1)
