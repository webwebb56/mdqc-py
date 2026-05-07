from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from mdqc.extractor.skyline import (
    SkylineTimeout,
    _strip_measured_results,
    find_skyline,
    has_error_marker,
    is_clickonce_install,
    run_skyline,
)


def test_clickonce_detection_true() -> None:
    p = Path(r"C:\Users\u\AppData\Local\Apps\2.0\HASH\HASH2\skylinecmd.exe")
    assert is_clickonce_install(p) is True


def test_clickonce_detection_false_msi() -> None:
    p = Path(r"C:\Program Files\Skyline\SkylineCmd.exe")
    assert is_clickonce_install(p) is False


def test_clickonce_detection_case_insensitive() -> None:
    p = Path(r"D:\Apps\2.0\Foo\skylinecmd.exe")
    assert is_clickonce_install(p) is True


def test_find_skyline_explicit_returns_when_exists(tmp_path: Path) -> None:
    fake = tmp_path / "SkylineCmd.exe"
    fake.write_text("not a real exe")
    assert find_skyline(explicit=fake) == fake


def test_find_skyline_explicit_missing_falls_through(tmp_path: Path) -> None:
    missing = tmp_path / "missing.exe"
    found = find_skyline(explicit=missing)
    if sys.platform != "win32":
        assert found is None


def test_has_error_marker_in_stdout() -> None:
    assert has_error_marker("Error: something broke", "") is True


def test_has_error_marker_in_stderr() -> None:
    assert has_error_marker("", "Exception: boom") is True


def test_has_error_marker_clean() -> None:
    assert has_error_marker("Skyline 24.1.0.198 done", "") is False


def _make_script(tmp_path: Path, name: str, body: str) -> Path:
    script = tmp_path / name
    script.write_text("#!/usr/bin/env bash\n" + body + "\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


@pytest.mark.skipif(sys.platform == "win32", reason="bash script harness not used on Windows")
async def test_run_skyline_happy_path(tmp_path: Path) -> None:
    template = tmp_path / "tmpl.sky"
    template.write_text("template")
    raw = tmp_path / "raw.raw"
    raw.write_text("raw")
    out = tmp_path / "out.csv"

    script = _make_script(
        tmp_path,
        "skyline.sh",
        'echo "Skyline 24.1.0.198"\n'
        'echo "Peptide,Total Area" > "$5" 2>/dev/null || true\n'
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        '    --report-file=*)\n'
        '      path="${arg#--report-file=}"\n'
        '      printf "Peptide,Total Area\\nPEP,1000\\n" > "$path"\n'
        '      ;;\n'
        '  esac\n'
        'done\n'
        'exit 0\n',
    )

    result = await run_skyline(
        skyline_exe=script,
        template=template,
        raw_file=raw,
        report_name="MD_QC_Report",
        output_csv=out,
        timeout_s=10,
    )
    assert result.returncode == 0
    assert "Skyline 24.1.0.198" in result.stdout
    assert result.version == "24.1.0.198"
    assert result.duration_ms >= 0
    assert out.read_text().startswith("Peptide,Total Area")


@pytest.mark.skipif(sys.platform == "win32", reason="bash script harness not used on Windows")
async def test_run_skyline_timeout(tmp_path: Path) -> None:
    template = tmp_path / "tmpl.sky"
    template.write_text("t")
    raw = tmp_path / "raw.raw"
    raw.write_text("r")
    out = tmp_path / "out.csv"

    script = _make_script(tmp_path, "sleeper.sh", "sleep 30\n")

    with pytest.raises(SkylineTimeout):
        await run_skyline(
            skyline_exe=script,
            template=template,
            raw_file=raw,
            report_name="X",
            output_csv=out,
            timeout_s=1,
        )


@pytest.mark.skipif(sys.platform == "win32", reason="bash script harness not used on Windows")
async def test_run_skyline_failure_exit1_stderr(tmp_path: Path) -> None:
    template = tmp_path / "tmpl.sky"
    template.write_text("t")
    raw = tmp_path / "raw.raw"
    raw.write_text("r")
    out = tmp_path / "out.csv"

    script = _make_script(
        tmp_path,
        "fail.sh",
        'echo "Error: bad input" 1>&2\nexit 1\n',
    )
    result = await run_skyline(
        skyline_exe=script,
        template=template,
        raw_file=raw,
        report_name="X",
        output_csv=out,
        timeout_s=10,
    )
    assert result.returncode == 1
    assert "Error: bad input" in result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="bash script harness not used on Windows")
async def test_run_skyline_error_in_stdout_exit_zero(tmp_path: Path) -> None:
    template = tmp_path / "tmpl.sky"
    template.write_text("t")
    raw = tmp_path / "raw.raw"
    raw.write_text("r")
    out = tmp_path / "out.csv"

    script = _make_script(
        tmp_path,
        "stdout_err.sh",
        'echo "Error: foo happened"\nexit 0\n',
    )
    result = await run_skyline(
        skyline_exe=script,
        template=template,
        raw_file=raw,
        report_name="X",
        output_csv=out,
        timeout_s=10,
    )
    assert result.returncode == 0
    assert "Error:" in result.stdout
    assert has_error_marker(result.stdout, result.stderr) is True


@pytest.mark.skipif(sys.platform == "win32", reason="bash script harness not used on Windows")
async def test_run_skyline_version_parsing_from_stdout(tmp_path: Path) -> None:
    template = tmp_path / "tmpl.sky"
    template.write_text("t")
    raw = tmp_path / "raw.raw"
    raw.write_text("r")
    out = tmp_path / "out.csv"

    script = _make_script(
        tmp_path,
        "ver.sh",
        'echo "Skyline 24.1.0.198 — daily build"\nexit 0\n',
    )
    result = await run_skyline(
        skyline_exe=script,
        template=template,
        raw_file=raw,
        report_name="X",
        output_csv=out,
        timeout_s=10,
    )
    assert result.version == "24.1.0.198"


def test_strip_measured_results_clean_template_is_noop() -> None:
    clean = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b"<srm_settings>\n"
        b"  <peptide_list_settings/>\n"
        b"</srm_settings>\n"
    )
    out, n = _strip_measured_results(clean)
    assert out == clean
    assert n == 0


def test_strip_measured_results_removes_block() -> None:
    polluted = (
        b"<srm_settings>\n"
        b"  <peptide_list_settings/>\n"
        b'  <measured_results time_normal_area="true">\n'
        b'    <replicate name="r1">\n'
        b'      <sample_file id="x" file_path="C:\\old\\path.raw"/>\n'
        b"    </replicate>\n"
        b'    <replicate name="r2">\n'
        b'      <sample_file id="y" file_path="C:\\old\\other.raw"/>\n'
        b"    </replicate>\n"
        b"  </measured_results>\n"
        b"</srm_settings>\n"
    )
    out, n = _strip_measured_results(polluted)
    assert n == 2
    assert b"measured_results" not in out
    assert b"<replicate " not in out
    assert b"<peptide_list_settings/>" in out
    assert b"</srm_settings>" in out


def test_strip_measured_results_with_no_attributes() -> None:
    polluted = (
        b"<root>\n"
        b"  <measured_results>\n"
        b'    <replicate name="r1"/>\n'
        b"  </measured_results>\n"
        b"</root>\n"
    )
    out, n = _strip_measured_results(polluted)
    assert n == 1
    assert b"measured_results" not in out


def test_strip_measured_results_malformed_returns_input_unchanged() -> None:
    # Missing closing tag — be conservative and don't touch the document.
    malformed = (
        b"<root>\n"
        b'  <measured_results>\n    <replicate name="r1"/>\n'
        b"</root>\n"
    )
    out, n = _strip_measured_results(malformed)
    assert out == malformed
    assert n == 0


def test_strip_measured_results_preserves_crlf_line_endings() -> None:
    polluted = (
        b"<root>\r\n"
        b"  <measured_results>\r\n"
        b'    <replicate name="r1"/>\r\n'
        b"  </measured_results>\r\n"
        b"  <other/>\r\n"
        b"</root>\r\n"
    )
    out, n = _strip_measured_results(polluted)
    assert n == 1
    assert b"<other/>" in out
    # Other CRLF line endings must survive untouched.
    assert b"<root>\r\n" in out
    assert b"</root>\r\n" in out


@pytest.mark.skyline
def test_real_skyline_marker_placeholder() -> None:
    """Marker placeholder — real test requires a SkylineCmd.exe install."""
    pytest.skip("requires real SkylineCmd.exe; opt in via -m skyline")


_ = os  # silence unused import on platforms that skip
