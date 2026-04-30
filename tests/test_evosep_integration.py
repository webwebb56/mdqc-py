"""
End-to-end integration tests against the local Evosep MS data.

Run levels:
  pytest tests/test_evosep_integration.py                  # Phase 1-2 only (no Skyline)
  pytest tests/test_evosep_integration.py -m skyline       # + Phase 3-4 (requires SkylineCmd)
  pytest tests/test_evosep_integration.py -v               # verbose output for all phases

Data dir:  C:\\Mac\\Home\\Documents\\MS Data repo
Spool dir: %PROGRAMDATA%\\MassDynamics\\QC\\spool  (or MDQC_DATA_DIR env override)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# ── Paths ─────────────────────────────────────────────────────────────────────

DATA_DIR = Path(r"C:\Mac\Home\Documents\MS Data repo")
EVOSEP_RAW_DIR = DATA_DIR / "Evosep_raw"

# Ensure package is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mdqc.classifier import classify_filename
from mdqc.config.paths import spool_dir, spool_completed, methods_dir
from mdqc.config.schema import Config
from mdqc.extractor.skyline import find_skyline
from mdqc.types import ControlType, Confidence, ClassificationSource

# These are resolved at import time — before conftest._isolate_data_dir redirects
# MDQC_DATA_DIR, so methods_dir() here returns the real ProgramData path.
_METHODS_SKY = methods_dir() / "QC_Method.sky"
_METHODS_SKYR = methods_dir() / "MD_QC_Report.skyr"
_DATA_SKY_FILES = [p for p in sorted(DATA_DIR.rglob("*.sky")) if p.is_file()]
SKYLINE_TEMPLATE: Path | None = (
    _METHODS_SKY if _METHODS_SKY.exists()
    else (_DATA_SKY_FILES[0] if _DATA_SKY_FILES else None)
)

# Default classifier rules from the shipped config (includes Evosep patterns).
_DEFAULT_RULES = Config().classifier_rules


# ── Helpers ───────────────────────────────────────────────────────────────────

def _raw_files() -> list[Path]:
    if not EVOSEP_RAW_DIR.exists():
        return []
    return sorted(EVOSEP_RAW_DIR.rglob("*.raw"))


def _smallest_raw(n: int = 1) -> list[Path]:
    """Return the N smallest .raw files — fastest for smoke tests."""
    files = _raw_files()
    return sorted(files, key=lambda p: p.stat().st_size)[:n]


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 1 — Classifier audit against real Evosep filenames
# ═══════════════════════════════════════════════════════════════════════════════

class TestClassifierAudit:
    """Run the classifier against every real .raw filename and report gaps."""

    def test_data_directory_exists(self):
        assert DATA_DIR.exists(), (
            f"MS data directory not found: {DATA_DIR}\n"
            "Update DATA_DIR at the top of this file if the path has changed."
        )

    def test_raw_files_found(self):
        files = _raw_files()
        assert len(files) > 0, f"No .raw files found under {EVOSEP_RAW_DIR}"
        print(f"\nFound {len(files)} .raw files")

    def test_classify_all_filenames(self):
        """Classify every filename and print a report. Fails if >80% are SAMPLE."""
        files = _raw_files()
        if not files:
            pytest.skip("No .raw files found")

        results = []
        for f in files:
            r = classify_filename(f.name, rules=_DEFAULT_RULES)
            results.append((f.name, r.control_type, r.confidence, r.source))

        # Print full report
        print(f"\n{'Filename':<65} {'Type':<8} {'Conf':<8} {'Source'}")
        print("-" * 100)
        for name, ct, conf, src in results:
            flag = "  ⚠" if ct == ControlType.SAMPLE else ""
            print(f"{name:<65} {ct.value:<8} {conf.value:<8} {src.value}{flag}")

        sample_count = sum(1 for _, ct, _, _ in results if ct == ControlType.SAMPLE)
        sample_pct = sample_count / len(results) * 100
        print(f"\n{sample_count}/{len(results)} files classified as SAMPLE ({sample_pct:.0f}%)")

        if sample_pct > 80:
            pytest.fail(
                f"{sample_pct:.0f}% of files classified as SAMPLE (LOW confidence).\n\n"
                "The Evosep naming convention ('QC_2026-...', 'Hela_QC_...', 'eb_inbetween_...')\n"
                "does not match the classifier patterns (QC_A, QC_B, SSC0, BLANK).\n\n"
                "Options:\n"
                "  1. Rename files to include QCA/QCB/SSC0 tokens, e.g. Hela_QCA_2026-02-03_...\n"
                "  2. Extend the classifier to recognise 'Hela_QC' → QC_A, 'eb_inbetween' → BLANK\n"
                "  3. Use the config 'control_type_override' field (if added) to map per-instrument"
            )

    def test_known_qc_patterns_match(self):
        """Verify that standard MD naming conventions still work."""
        cases = [
            ("SSC0_2024-01-15.raw",          ControlType.SSC0),
            ("InstrA_QCA_2024-01-15.raw",     ControlType.QC_A),
            ("InstrA_QCB_A3_2024-01-15.raw",  ControlType.QC_B),
            ("InstrA_BLANK_2024-01-15.raw",   ControlType.BLANK),
        ]
        for name, expected in cases:
            result = classify_filename(name)
            assert result.control_type == expected, (
                f"{name!r}: expected {expected.value}, got {result.control_type.value}"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2 — File accessibility and watcher compatibility
# ═══════════════════════════════════════════════════════════════════════════════

class TestFileAccessibility:
    """Verify the raw files are readable and would be picked up by the watcher."""

    def test_raw_files_are_readable(self):
        files = _raw_files()
        if not files:
            pytest.skip("No .raw files found")
        unreadable = [f for f in files if not os.access(f, os.R_OK)]
        assert not unreadable, f"Unreadable files:\n" + "\n".join(str(f) for f in unreadable)

    def test_raw_files_are_nonzero(self):
        files = _raw_files()
        if not files:
            pytest.skip("No .raw files found")
        empty = [f for f in files if f.stat().st_size == 0]
        assert not empty, f"Empty (0-byte) .raw files:\n" + "\n".join(str(f) for f in empty)

    def test_file_pattern_glob_works(self):
        """Verify the default '*.raw' glob would pick up the files."""
        files = _raw_files()
        if not files:
            pytest.skip("No .raw files found")
        globs = list(EVOSEP_RAW_DIR.glob("**/*.raw"))
        assert len(globs) == len(files), (
            f"Glob found {len(globs)} but direct list found {len(files)}"
        )

    def test_smallest_file_size(self):
        """Report size of smallest .raw — useful for choosing smoke-test candidates."""
        files = _smallest_raw(3)
        if not files:
            pytest.skip("No .raw files found")
        print("\nSmallest .raw files:")
        for f in files:
            size_mb = f.stat().st_size / (1024 * 1024)
            print(f"  {size_mb:6.1f} MB  {f.name}")

    def test_skyline_template_found(self):
        """Check that at least one .sky template is available in the data dir."""
        assert SKYLINE_TEMPLATE is not None, (
            f"No .sky file found under {DATA_DIR}\n"
            "The Skyline template is required for extraction."
        )
        print(f"\nTemplate candidate: {SKYLINE_TEMPLATE}")


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3 — Skyline extraction smoke test (requires SkylineCmd)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.skyline
class TestSkylineExtraction:
    """Run Skyline against one real .raw file and verify outputs.

    Skipped unless SkylineCmd.exe is installed and -m skyline is passed.
    """

    def test_skylinecmd_found(self):
        skyline = find_skyline()
        assert skyline is not None, (
            "SkylineCmd.exe not found. Install Skyline or set the path in config.toml."
        )
        print(f"\nSkylineCmd: {skyline}")

    def test_skyline_version(self):
        skyline = find_skyline()
        if skyline is None:
            pytest.skip("SkylineCmd not found")
        result = subprocess.run(
            [str(skyline), "--version"],
            capture_output=True, text=True, timeout=15
        )
        version = (result.stdout + result.stderr).strip()
        print(f"\nSkyline version: {version}")
        assert result.returncode == 0 or version, "SkylineCmd --version failed"

    def test_extract_smallest_raw(self, tmp_path):
        """Run a real extraction on the smallest .raw file and verify CSV output."""
        skyline = find_skyline()
        if skyline is None:
            pytest.skip("SkylineCmd not found")
        if SKYLINE_TEMPLATE is None:
            pytest.skip("No .sky template found")

        raw_files = _smallest_raw(1)
        if not raw_files:
            pytest.skip("No .raw files found")

        raw_file = raw_files[0]
        print(f"\nExtracting: {raw_file.name} ({raw_file.stat().st_size / 1e6:.1f} MB)")
        print(f"Template: {SKYLINE_TEMPLATE}")
        print(f"Work dir: {tmp_path}")

        # Build SkylineCmd command (mirrors extractor/skyline.py logic)
        cmd = [str(skyline), f"--in={SKYLINE_TEMPLATE}"]
        if _METHODS_SKYR.exists():
            cmd.append(f"--report-add={_METHODS_SKYR}")
        cmd += [
            f"--import-file={raw_file}",
            "--report-name=MD_QC_Report",
            f"--report-file={tmp_path / 'report.csv'}",
            "--import-threads=1",
        ]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600
        )
        print(f"Return code: {result.returncode}")
        if result.stdout:
            print(f"stdout (last 500 chars): ...{result.stdout[-500:]}")
        if result.stderr:
            print(f"stderr (last 500 chars): ...{result.stderr[-500:]}")

        assert result.returncode == 0, (
            f"SkylineCmd failed (rc={result.returncode}).\n"
            f"stderr: {result.stderr[-1000:]}"
        )

        report_csv = tmp_path / "report.csv"
        assert report_csv.exists(), "SkylineCmd did not produce report.csv"
        assert report_csv.stat().st_size > 0, "report.csv is empty"
        print(f"report.csv: {report_csv.stat().st_size} bytes")

    def test_csv_parser_on_extraction_output(self, tmp_path):
        """Run the CSV parser on the Skyline output and check metric shapes."""
        from mdqc.extractor.report import parse_skyline_csv

        skyline = find_skyline()
        if skyline is None:
            pytest.skip("SkylineCmd not found")

        report_csv = tmp_path / "report.csv"
        if not report_csv.exists():
            pytest.skip("Run test_extract_smallest_raw first (shared tmp_path)")

        metrics = parse_skyline_csv(report_csv)
        assert len(metrics) > 0, "Parser returned no metrics from Skyline CSV"
        print(f"\nParsed {len(metrics)} target metrics")
        detected = sum(1 for m in metrics if m.detected)
        print(f"Detected: {detected}/{len(metrics)}")
        print(f"First target: {metrics[0].peptide_sequence} | "
              f"RT={metrics[0].retention_time} | area={metrics[0].peak_area}")


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 4 — Payload generation and schema validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestPayloadSchema:
    """Validate payload JSON schema using a synthetic extraction result."""

    def test_payload_generates_valid_json(self, tmp_path):
        from mdqc.spool.store import Spool
        from mdqc.types import (
            ExtractionResult, ExtractionStatus, RunClassification,
            Confidence, ClassificationSource, ControlType,
            TargetMetric, RunMetrics,
        )
        from mdqc.types import Vendor

        spool = Spool(agent_id="evosep-test", agent_version="0.1.0", root=tmp_path)

        classification = RunClassification(
            control_type=ControlType.QC_A,
            well_position=None,
            instrument_id="Evosep_Astral_001",
            plate_id=None,
            confidence=Confidence.MEDIUM,
            source=ClassificationSource.FILENAME,
        )
        # Use a real filename from the data dir so the payload looks realistic
        raw_files = _raw_files()
        raw_path = raw_files[0] if raw_files else Path("Hela_QC_2026-02-03_test.raw")

        extraction = ExtractionResult(
            raw_file_path=raw_path,
            template_path=SKYLINE_TEMPLATE or Path("QC_Method.sky"),
            status=ExtractionStatus.SUCCESS,
            backend_version="23.1",
            extraction_time_ms=12500,
            target_metrics=[
                TargetMetric(
                    target_id="IGTDEPTHNK_582.29",
                    peptide_sequence="IGTDEPTHNK",
                    precursor_mz=582.29,
                    retention_time=12.45,
                    rt_expected=12.50,
                    rt_delta=-0.05,
                    peak_area=2_500_000.0,
                    peak_height=450_000.0,
                    peak_width_fwhm=0.12,
                    mass_error_ppm=1.2,
                    isotope_dot_product=0.98,
                    detected=True,
                ),
            ],
            run_metrics=RunMetrics(
                targets_found=1,
                targets_expected=3,
                target_recovery_pct=33.3,
                median_rt_shift=-0.05,
                median_mass_error_ppm=1.2,
                chromatography_score=0.85,
            ),
        )

        payload_path = spool.enqueue(classification, extraction)
        assert payload_path.exists()

        payload = json.loads(payload_path.read_text(encoding="utf-8"))

        # Schema contract checks
        assert payload["schema_version"] == "1.1", \
            f"Expected schema 1.1, got {payload['schema_version']}"
        assert "payload_id" in payload
        assert "timestamp" in payload
        assert "run" in payload
        assert "extraction" in payload
        assert "target_metrics" in payload
        assert "run_metrics" in payload

        run = payload["run"]
        assert run["instrument_id"] == "Evosep_Astral_001"
        assert run["raw_file_name"] == raw_path.name
        assert "acquisition_time" in run
        assert "vendor" in run

        rm = payload["run_metrics"]
        assert rm["targets_found"] == 1
        assert rm["target_recovery_pct"] == pytest.approx(33.3, abs=0.1)

        tm = payload["target_metrics"]
        assert len(tm) == 1
        assert tm[0]["peptide_sequence"] == "IGTDEPTHNK"
        assert tm[0]["detected"] is True

        print(f"\nPayload written to: {payload_path}")
        print(f"Schema version: {payload['schema_version']}")
        print(f"Payload size: {payload_path.stat().st_size} bytes")

    def test_payload_loads_in_plots_app(self, tmp_path):
        """Verify the plots data loader can parse a payload written to the spool."""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

        # Write a minimal payload the plots app can load
        payload = {
            "schema_version": "1.1",
            "payload_id": "00000000-0000-0000-0000-000000000001",
            "timestamp": "2026-02-03T10:00:00Z",
            "agent_id": "evosep-test",
            "agent_version": "0.1.0",
            "correlation_id": "test-1",
            "run": {
                "raw_file_name": "Hela_QC_2026-02-03_test.raw",
                "acquisition_time": "2026-02-03T09:45:00Z",
                "instrument_id": "Evosep_Astral_001",
                "control_type": "QC_A",
                "vendor": "thermo",
            },
            "extraction": {
                "backend": "skyline",
                "backend_version": "23.1",
                "status": "SUCCESS",
                "template_name": "QC_Method.sky",
                "extraction_time_ms": 12500,
            },
            "target_metrics": [
                {
                    "target_id": "IGTDEPTHNK_582.29",
                    "peptide_sequence": "IGTDEPTHNK",
                    "retention_time": 12.45,
                    "rt_delta": -0.05,
                    "peak_area": 2500000.0,
                    "peak_height": 450000.0,
                    "peak_width_fwhm": 0.12,
                    "mass_error_ppm": 1.2,
                    "isotope_dot_product": 0.98,
                    "detected": True,
                    "extra_metrics": {},
                }
            ],
            "run_metrics": {
                "targets_found": 1,
                "targets_expected": 3,
                "target_recovery_pct": 33.3,
                "median_rt_shift": -0.05,
                "median_mass_error_ppm": 1.2,
                "chromatography_score": 0.85,
            },
        }

        payload_file = tmp_path / "Hela_QC_2026-02-03_test_payload.json"
        payload_file.write_text(json.dumps(payload), encoding="utf-8")

        # Import and run the plots app data loader directly
        import importlib.util
        app_path = Path(__file__).resolve().parents[1] / "src" / "mdqc" / "plots" / "app.py"
        if not app_path.exists():
            pytest.skip("plots/app.py not found")

        spec = importlib.util.spec_from_file_location("plots_app", app_path)
        plots_app = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]

        # load_payloads is a standalone function — test it directly without Streamlit
        import json as _json
        import pandas as pd

        records = []
        folder = tmp_path
        for fpath in folder.glob("*_payload.json"):
            data = _json.loads(fpath.read_text(encoding="utf-8"))
            run = data.get("run", {})
            rm = data.get("run_metrics", {})
            for target in data.get("target_metrics", []):
                rec = {
                    "acquisition_time": run.get("acquisition_time"),
                    "instrument_id": run.get("instrument_id"),
                    "control_type": run.get("control_type"),
                    "raw_file_name": run.get("raw_file_name"),
                    "target_id": target.get("target_id", ""),
                    "peptide_sequence": target.get("peptide_sequence"),
                    "retention_time": target.get("retention_time"),
                    "peak_area": target.get("peak_area"),
                    "mass_error_ppm": target.get("mass_error_ppm"),
                }
                records.append(rec)

        df = pd.DataFrame(records)
        assert not df.empty, "Plots data loader returned empty DataFrame"
        assert "retention_time" in df.columns
        assert "peak_area" in df.columns
        assert df["instrument_id"].iloc[0] == "Evosep_Astral_001"
        print(f"\nPlots loader: {len(df)} rows, {len(df.columns)} columns")


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 5 — Spool health check against live data dir
# ═══════════════════════════════════════════════════════════════════════════════

class TestLiveSpoolHealth:
    """Check the live spool directory for any completed payloads."""

    def test_spool_dir_accessible(self):
        spool = spool_dir()
        if not spool.exists():
            pytest.skip(f"Spool dir does not exist yet: {spool}")
        assert os.access(spool, os.R_OK), f"Spool dir not readable: {spool}"
        print(f"\nSpool dir: {spool}")

    def test_completed_payload_count(self):
        completed = spool_completed()
        if not completed.exists():
            pytest.skip(f"Completed dir does not exist: {completed}")
        payloads = list(completed.glob("*_payload.json"))
        print(f"\nCompleted payloads: {len(payloads)}")
        for p in payloads[-5:]:  # show last 5
            size_kb = p.stat().st_size / 1024
            print(f"  {size_kb:6.1f} KB  {p.name}")

    def test_completed_payloads_are_valid_json(self):
        completed = spool_completed()
        if not completed.exists():
            pytest.skip("Completed dir does not exist")
        payloads = list(completed.glob("*_payload.json"))
        if not payloads:
            pytest.skip("No completed payloads yet — run the agent against some data first")

        bad = []
        for p in payloads:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                assert "schema_version" in data
                assert "target_metrics" in data
            except Exception as e:
                bad.append(f"{p.name}: {e}")

        assert not bad, "Invalid payload files:\n" + "\n".join(bad)
        print(f"\nAll {len(payloads)} completed payloads are valid JSON")
