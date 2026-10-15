"""Running with no window: mdqc start / open, single instance, pythonw safety,
and the packaging the install script depends on."""

from __future__ import annotations

import logging
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mdqc import single_instance
from mdqc.cli import app, open_cmd, start

ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutexes")


# ── mdqc start ───────────────────────────────────────────────────────────────


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(start, "_spawn", lambda *a: calls.append(a))
    monkeypatch.setattr(start, "service_installed", lambda: False)
    monkeypatch.setattr(single_instance, "held_elsewhere", lambda _n: False)
    return calls


def test_start_launches_agent_and_tray_when_nothing_runs(
    spawned: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(open_cmd, "dashboard_url", lambda page="/dashboard": None)
    result = runner.invoke(app, ["start"])
    assert result.exit_code == 0, result.output
    assert spawned == [("run", "--foreground"), ("tray",)]


def test_start_leaves_a_running_agent_alone(spawned: list, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(open_cmd, "dashboard_url", lambda page="/dashboard": "http://x")
    result = runner.invoke(app, ["start"])
    assert result.exit_code == 0
    assert spawned == [("tray",)]
    assert "already running" in result.output


def test_start_defers_to_the_windows_service(spawned: list, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(open_cmd, "dashboard_url", lambda page="/dashboard": None)
    monkeypatch.setattr(start, "service_installed", lambda: True)
    result = runner.invoke(app, ["start", "--no-tray"])
    assert result.exit_code == 0
    assert spawned == []
    assert "Windows service" in result.output


def test_start_refuses_when_an_unreachable_agent_holds_the_pc(
    spawned: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another installation's agent: a second would exit at once, so say why."""
    monkeypatch.setattr(open_cmd, "dashboard_url", lambda page="/dashboard": None)
    monkeypatch.setattr(single_instance, "held_elsewhere", lambda _n: True)
    result = runner.invoke(app, ["start"])
    assert result.exit_code == 3
    assert spawned == []


def test_windowless_python_prefers_pythonw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "python.exe").write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    assert start.windowless_python() == str(tmp_path / "pythonw.exe")


# ── mdqc open ────────────────────────────────────────────────────────────────


def test_open_says_when_the_agent_is_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MDQC_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["open", "--print-url"])
    assert result.exit_code == 1
    assert "isn't running" in result.output


def test_open_prints_the_tokened_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        open_cmd, "dashboard_url", lambda page="/dashboard": f"http://127.0.0.1:1{page}?token=t"
    )
    result = runner.invoke(app, ["open", "settings", "--print-url"])
    assert result.exit_code == 0
    assert result.output.strip() == "http://127.0.0.1:1/settings?token=t"


def test_a_stale_runtime_file_is_not_a_running_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """runtime.json outlives a crashed agent; the health check catches it."""
    from datetime import UTC, datetime

    from mdqc.ipc.runtime import RuntimeFile, RuntimeInfo

    monkeypatch.setenv("MDQC_DATA_DIR", str(tmp_path))
    RuntimeFile().write(RuntimeInfo(port=1, token="t", pid=1, started_at=datetime.now(UTC)))
    assert open_cmd.dashboard_url() is None


# ── single instance ──────────────────────────────────────────────────────────


@windows_only
def test_a_second_holder_is_refused() -> None:
    name = "Local\\MassDynamicsQCTest-second-holder"
    assert single_instance.acquire(name)
    # Same process, second CreateMutex: Windows reports it already exists.
    assert not single_instance.acquire(name)


@windows_only
def test_another_process_holding_the_mutex_is_seen() -> None:
    name = "Local\\MassDynamicsQCTest-other-process"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time; from mdqc import single_instance as s; "
            f"s.acquire({name!r}); print('held', flush=True); time.sleep(30)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        assert single_instance.held_elsewhere(name)
    finally:
        holder.kill()
        holder.wait()
    assert not single_instance.held_elsewhere(name)


def test_off_windows_the_guard_never_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert single_instance.acquire("anything")
    assert single_instance.acquire("anything")


# ── no stderr under pythonw ──────────────────────────────────────────────────


def test_logging_skips_the_console_without_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mdqc.log import configure_logging

    monkeypatch.setattr(sys, "stderr", None)
    configure_logging("info", log_file=tmp_path / "x.log")
    handlers = logging.getLogger().handlers
    assert not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in handlers
    )


# ── packaging ────────────────────────────────────────────────────────────────


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_tray_extra_has_wheels_for_every_supported_python() -> None:
    """winsdk stops at 3.12; on 3.13+ it needed a C++ compiler to install."""
    tray = " ".join(_pyproject()["project"]["optional-dependencies"]["tray"])
    assert "winsdk" not in tray
    assert "winrt-Windows.UI.Notifications" in tray


def test_assets_ship_inside_the_wheel() -> None:
    include = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert include == {"assets": "mdqc/_assets"}
    assert (ROOT / "assets" / "icon.png").is_file()


def test_packaged_assets_are_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mdqc.config import paths

    pkg = tmp_path / "mdqc"
    (pkg / "config").mkdir(parents=True)
    (pkg / "_assets").mkdir()
    monkeypatch.setattr(paths, "__file__", str(pkg / "config" / "paths.py"))
    assert paths.bundled_assets_dir() == pkg / "_assets"


def test_notifications_use_the_app_id_overload() -> None:
    """winrt names the app-id overload create_toast_notifier_with_id."""
    import mdqc.notifications as n

    seen: list[str] = []

    class Manager:
        @staticmethod
        def create_toast_notifier_with_id(aumid: str):  # type: ignore[no-untyped-def]
            seen.append(aumid)
            return type("N", (), {"show": lambda self, t: None})()

    class Notifications:
        ToastNotificationManager = Manager

        @staticmethod
        def ToastNotification(xml):  # type: ignore[no-untyped-def]
            return xml

    class Xml:
        class XmlDocument:
            def load_xml(self, s: str) -> None:
                pass

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(sys, "platform", "win32")
        mp.setattr(n, "_WINSDK_AVAILABLE", True)
        mp.setattr(n, "_winsdk_notifications", Notifications)
        mp.setattr(n, "_winsdk_xml", Xml)
        n.Notifier(aumid="Test.App").notify("t", "b")
    finally:
        mp.undo()
    assert seen == ["Test.App"]


# ── the script itself ────────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell")
def test_install_script_parses() -> None:
    script = ROOT / "scripts" / "install.ps1"
    cmd = (
        "$e = $null; [System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script}', [ref]$null, [ref]$e) | Out-Null; $e.Count"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "0", out.stdout + out.stderr


def test_install_script_is_ascii() -> None:
    """Windows PowerShell 5.1 reads a BOM-less script as the ANSI code page."""
    (ROOT / "scripts" / "install.ps1").read_bytes().decode("ascii")
