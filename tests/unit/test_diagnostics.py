from __future__ import annotations

import logging
from pathlib import Path
from textwrap import dedent

import pytest

from mdqc.diagnostics import (
    DiagnosticsReport,
    render_text_report,
    run_diagnostics,
)
from mdqc.extractor import skyline as sk


def _write_config(tmp_data_dir: Path, body: str) -> Path:
    cfg_path = tmp_data_dir / "config.toml"
    cfg_path.write_text(dedent(body), encoding="utf-8")
    return cfg_path


@pytest.mark.asyncio
async def test_no_instruments_overall_not_ok(tmp_data_dir: Path) -> None:
    _write_config(
        tmp_data_dir,
        """
        [agent]
        agent_id = "test"

        [cloud]
        api_token = "x"

        [skyline]
        path = "auto"
        """,
    )
    report = await run_diagnostics()
    assert isinstance(report, DiagnosticsReport)
    assert report.config_ok is True
    assert report.overall_ok is False  # no instruments


@pytest.mark.asyncio
async def test_skyline_absent_when_not_found(tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(
        tmp_data_dir,
        """
        [agent]
        agent_id = "test"

        [cloud]
        api_token = "x"

        [skyline]
        path = "/definitely/does/not/exist/SkylineCmd.exe"
        """,
    )

    # Force find_skyline to return None by stubbing the lookup helpers.
    import mdqc.extractor.skyline as sk

    monkeypatch.setattr(sk, "_registry_lookup", lambda: None)
    monkeypatch.setattr(sk, "_COMMON_PATHS", ())
    monkeypatch.setattr(sk.shutil, "which", lambda _name: None)

    report = await run_diagnostics()
    assert report.skyline_path is None
    assert report.overall_ok is False


@pytest.mark.asyncio
async def test_cert_thumbprint_without_token_flags_unsupported(tmp_data_dir: Path) -> None:
    _write_config(
        tmp_data_dir,
        """
        [agent]
        agent_id = "test"

        [cloud]
        certificate_thumbprint = "AABBCCDDEEFF00112233445566778899AABBCCDD"
        """,
    )
    report = await run_diagnostics()
    assert report.cert_thumbprint_set_but_unsupported is True
    assert report.overall_ok is False


@pytest.mark.asyncio
async def test_render_text_report_multiline(tmp_data_dir: Path) -> None:
    _write_config(
        tmp_data_dir,
        """
        [agent]
        agent_id = "test"
        """,
    )
    report = await run_diagnostics()
    text = render_text_report(report)
    assert text
    assert "MD Local QC Agent" in text
    assert "Skyline" in text
    assert "Cloud" in text
    assert "Spool" in text
    assert text.count("\n") > 5


@pytest.mark.asyncio
async def test_missing_config_reports_error(tmp_data_dir: Path) -> None:
    # No config.toml present.
    report = await run_diagnostics()
    assert report.config_ok is False
    assert report.config_error is not None
    assert report.overall_ok is False


# ── Skyline version vs template format ──────────────────────────────────────
# Evosep's first Sciex 7500 install failed every extraction: the template had
# been saved in format 26.11 and the instrument PC ran Skyline 26.1. `mdqc
# doctor` hard-coded the Skyline version to "unknown", so it could not have
# warned anyone, and the running service logged nothing about why.


def _write_sky(path: Path, fmt: str, saved_by: str) -> Path:
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<srm_settings format_version="{fmt}" software_version="{saved_by}">\n'
        '  <settings_summary name="Default"/>\n'
        "</srm_settings>\n",
        encoding="utf-8",
    )
    return path


def _instrument_config(tmp_data_dir: Path, template: Path) -> Path:
    fake_exe = tmp_data_dir / "SkylineCmd.exe"
    fake_exe.write_text("", encoding="utf-8")
    watch = tmp_data_dir / "watch"
    watch.mkdir(exist_ok=True)
    return _write_config(
        tmp_data_dir,
        f"""
        [agent]
        agent_id = "test"

        [skyline]
        path = '{fake_exe.as_posix()}'

        [[instruments]]
        id = "Sciex_7500"
        vendor = "sciex"
        watch_path = '{watch.as_posix()}'
        file_pattern = "*.wiff"
        template = '{template.as_posix()}'
        """,
    )


def test_read_template_format(tmp_path: Path) -> None:
    p = _write_sky(tmp_path / "t.sky", "26.11", "Skyline-daily (64-bit) 26.1.1.123")
    assert sk.read_template_format(p) == ("26.11", "Skyline-daily (64-bit) 26.1.1.123")


def test_read_template_format_on_a_non_skyline_file(tmp_path: Path) -> None:
    p = tmp_path / "x.sky"
    p.write_text("not a skyline document", encoding="utf-8")
    assert sk.read_template_format(p) == (None, None)
    assert sk.read_template_format(tmp_path / "missing.sky") == (None, None)


@pytest.mark.parametrize(
    ("fmt", "installed", "expected"),
    [
        ("26.11", "26.1.0.057", True),   # the Sciex 7500 case, verbatim
        ("25.11", "26.1.0.57", False),   # this repo's QC_Method.sky on Skyline 26.1
        ("26.1", "26.1.0.57", False),
        ("26.2", "26.1.0.57", True),
        (None, "26.1.0.57", None),
        ("26.1", None, None),
        ("garbage", "26.1.0.57", None),
    ],
)
def test_template_newer_than_skyline(
    fmt: str | None, installed: str | None, expected: bool | None
) -> None:
    assert sk.template_newer_than_skyline(fmt, installed) is expected


@pytest.mark.asyncio
async def test_doctor_fails_a_template_newer_than_skyline(
    tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tpl = _write_sky(tmp_data_dir / "QC_7500.sky", "26.11", "Skyline-daily (64-bit) 26.1.1.123")
    _instrument_config(tmp_data_dir, tpl)
    monkeypatch.setattr(sk, "read_skyline_version", lambda _exe, **_kw: "26.1.0.057")

    report = await run_diagnostics()

    assert report.skyline_version == "26.1.0.057"
    [t] = report.templates
    assert t.format_version == "26.11"
    assert t.newer_than_skyline is True
    assert report.overall_ok is False
    text = render_text_report(report)
    assert "is newer than installed Skyline 26.1.0.057" in text
    assert "every extraction will fail" in text
    assert "saved for Skyline 26.1" in text


@pytest.mark.asyncio
async def test_doctor_passes_a_compatible_template(
    tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tpl = _write_sky(tmp_data_dir / "QC.sky", "25.11", "Skyline-daily (64-bit) 26.0.9.032")
    _instrument_config(tmp_data_dir, tpl)
    monkeypatch.setattr(sk, "read_skyline_version", lambda _exe, **_kw: "26.1.0.57")

    report = await run_diagnostics()

    [t] = report.templates
    assert t.newer_than_skyline is False
    assert report.overall_ok is True
    text = render_text_report(report)
    assert "format: 25.11 - readable by installed Skyline 26.1.0.57" in text
    # The saving Skyline has its own line, so the template line stays short.
    assert "\n    Saved by: Skyline-daily (64-bit) 26.0.9.032" in text
    assert report.to_dict()["templates"][0]["format_version"] == "25.11"


@pytest.mark.asyncio
async def test_doctor_does_not_fail_when_the_skyline_version_is_unknown(
    tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown is reported as unknown - not as a pass, and not as a failure."""
    tpl = _write_sky(tmp_data_dir / "QC.sky", "26.11", "Skyline-daily (64-bit) 26.1.1.123")
    _instrument_config(tmp_data_dir, tpl)
    monkeypatch.setattr(sk, "read_skyline_version", lambda _exe, **_kw: None)

    report = await run_diagnostics()

    [t] = report.templates
    assert t.newer_than_skyline is None
    assert report.overall_ok is True
    assert "not checked, Skyline version unknown" in render_text_report(report)


def test_startup_logs_a_template_newer_than_skyline(
    tmp_data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from mdqc.config import load_config
    from mdqc.service.lifecycle import _log_templates_newer_than_skyline

    tpl = _write_sky(tmp_data_dir / "QC_7500.sky", "26.11", "Skyline-daily (64-bit) 26.1.1.123")
    cfg = load_config(_instrument_config(tmp_data_dir, tpl), strict_cert_guard=False)

    with caplog.at_level(logging.ERROR):
        _log_templates_newer_than_skyline(cfg, "26.1.0.057")

    assert [r.getMessage() for r in caplog.records] == ["template_newer_than_skyline"]


def test_startup_is_quiet_for_a_compatible_template(
    tmp_data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from mdqc.config import load_config
    from mdqc.service.lifecycle import _log_templates_newer_than_skyline

    tpl = _write_sky(tmp_data_dir / "QC.sky", "25.11", "Skyline-daily (64-bit) 26.0.9.032")
    cfg = load_config(_instrument_config(tmp_data_dir, tpl), strict_cert_guard=False)

    with caplog.at_level(logging.ERROR):
        _log_templates_newer_than_skyline(cfg, "26.1.0.57")

    assert caplog.records == []
