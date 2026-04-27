from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from mdqc.diagnostics import (
    DiagnosticsReport,
    render_text_report,
    run_diagnostics,
)


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
