from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

import mdqc.crash as crash_mod
from mdqc.crash import (
    GITHUB_ISSUE_URL,
    _build_issue_url,
    _compose_crash_report,
    _excepthook,
    _truncate_for_url,
    install_crash_handlers,
)


def _fake_exc_info() -> tuple[type[BaseException], BaseException, Any]:
    try:
        raise ValueError("Boom: bad value with unicode ☃")
    except ValueError as exc:
        return type(exc), exc, exc.__traceback__


def test_compose_includes_required_fields() -> None:
    exc_type, exc_value, tb = _fake_exc_info()
    report = _compose_crash_report(exc_type, exc_value, tb)
    assert "ValueError" in report
    assert "Boom" in report
    assert "Traceback" in report
    assert "Agent version:" in report
    assert "Python version:" in report
    assert "Platform:" in report
    assert "Timestamp:" in report


def test_truncate_for_url_under_limit_unchanged() -> None:
    s = "abc"
    assert _truncate_for_url(s, max_chars=100) == s


def test_truncate_for_url_truncates_with_marker() -> None:
    long = "x" * 5000
    out = _truncate_for_url(long, max_chars=1500)
    assert len(out) <= 1500
    assert out.endswith("...[truncated]")


def test_url_uses_urllib_quote() -> None:
    exc_type, exc_value, tb = _fake_exc_info()
    report = _compose_crash_report(exc_type, exc_value, tb)
    url = _build_issue_url(report, exc_type)
    assert url.startswith(GITHUB_ISSUE_URL + "?title=")
    expected_title = quote("Crash: ValueError")
    assert expected_title in url
    body_part = url.split("&body=", 1)[1]
    assert "%E2%98%83" in body_part or "%5Cu2603" in body_part or "%E2%98%83" in url


def test_install_crash_handlers_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crash_mod, "_installed", False)
    monkeypatch.setattr(crash_mod, "_original_excepthook", None)
    original = sys.excepthook
    try:
        install_crash_handlers(log_messagebox=False)
        first = sys.excepthook
        install_crash_handlers(log_messagebox=False)
        second = sys.excepthook
        assert first is second
    finally:
        sys.excepthook = original
        monkeypatch.setattr(crash_mod, "_installed", False)


def test_excepthook_writes_crash_file(
    tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crash_mod, "_show_messagebox_enabled", False)
    monkeypatch.setattr(crash_mod, "_original_excepthook", lambda *a, **k: None)

    exc_type, exc_value, tb = _fake_exc_info()
    _excepthook(exc_type, exc_value, tb)

    crashes = tmp_data_dir / "crashes"
    assert crashes.exists()
    files = list(crashes.glob("crash_*.txt"))
    assert len(files) >= 1
    content = files[0].read_text(encoding="utf-8")
    assert "ValueError" in content
    assert "Boom" in content


def test_excepthook_skips_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_data_dir: Path
) -> None:
    monkeypatch.setattr(crash_mod, "_show_messagebox_enabled", False)
    called: list[bool] = []

    def fake_original(*a: object, **k: object) -> None:
        called.append(True)

    monkeypatch.setattr(crash_mod, "_original_excepthook", fake_original)
    try:
        raise KeyboardInterrupt("user cancel")
    except KeyboardInterrupt as exc:
        _excepthook(type(exc), exc, exc.__traceback__)

    assert called == [True]
    crashes = tmp_data_dir / "crashes"
    if crashes.exists():
        assert list(crashes.glob("crash_*.txt")) == []
