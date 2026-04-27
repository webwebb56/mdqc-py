"""Extractor: Skyline subprocess + CSV report parser."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import time
import uuid
from pathlib import Path

from mdqc.config.schema import SkylineConfig
from mdqc.extractor.report import parse_skyline_csv
from mdqc.extractor.skyline import (
    SkylineClickOnceUnsupported,
    SkylineFailed,
    SkylineNotFound,
    SkylineRunResult,
    SkylineTimeout,
    find_skyline,
    has_error_marker,
    is_clickonce_install,
    run_skyline,
)
from mdqc.types import ExtractionResult, ExtractionStatus, RunMetrics

logger = logging.getLogger(__name__)

_FILE_HASH_CAP_BYTES = 50 * 1024 * 1024


def compute_template_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def compute_raw_hash(path: Path) -> str:
    h = hashlib.sha256()
    if path.is_file():
        remaining = _FILE_HASH_CAP_BYTES
        with open(path, "rb") as fh:
            while remaining > 0:
                chunk = fh.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                h.update(chunk)
                remaining -= len(chunk)
        return h.hexdigest()

    if path.is_dir():
        entries: list[tuple[str, int]] = []
        for child in path.rglob("*"):
            if child.is_file():
                try:
                    size = child.stat().st_size
                except OSError:
                    size = 0
                rel = child.relative_to(path).as_posix()
                entries.append((rel, size))
        entries.sort()
        for name, size in entries:
            h.update(f"{name}:{size}\n".encode())
        return h.hexdigest()

    raise FileNotFoundError(path)


def _run_metrics(targets: list) -> RunMetrics:
    targets_expected = len(targets)
    targets_found = sum(1 for t in targets if t.detected)
    recovery = (targets_found / targets_expected * 100.0) if targets_expected else 0.0

    rt_deltas = sorted(t.rt_delta for t in targets if t.rt_delta is not None)
    median_rt = _median(rt_deltas)

    mass_errors = sorted(t.mass_error_ppm for t in targets if t.mass_error_ppm is not None)
    median_mass = _median(mass_errors)

    return RunMetrics(
        targets_found=targets_found,
        targets_expected=targets_expected,
        target_recovery_pct=recovery,
        median_rt_shift=median_rt,
        median_mass_error_ppm=median_mass,
    )


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    n = len(values)
    mid = n // 2
    if n % 2 == 0:
        return (values[mid - 1] + values[mid]) / 2.0
    return values[mid]


class Extractor:
    def __init__(self, skyline_cfg: SkylineConfig, *, work_dir: Path | None = None) -> None:
        self._cfg = skyline_cfg
        self._work_dir = Path(work_dir) if work_dir else Path.cwd()
        explicit = None
        if skyline_cfg.path and skyline_cfg.path.lower() != "auto":
            explicit = Path(skyline_cfg.path)
        self._skyline_path = find_skyline(explicit=explicit)

    @property
    def skyline_path(self) -> Path | None:
        return self._skyline_path

    async def extract(
        self,
        template: Path,
        raw_file: Path,
        report_name: str = "MD_QC_Report",
    ) -> ExtractionResult:
        result = ExtractionResult(
            raw_file_path=raw_file,
            template_path=template,
        )

        skyline_path = self._skyline_path
        if skyline_path is None or not skyline_path.exists():
            raise SkylineNotFound(
                "SkylineCmd.exe not found (searched explicit, registry, common paths, PATH)"
            )

        if is_clickonce_install(skyline_path):
            raise SkylineClickOnceUnsupported(
                f"Skyline at {skyline_path} is a ClickOnce install; install the MSI version instead"
            )

        if not template.exists():
            raise FileNotFoundError(f"template not found: {template}")
        if not raw_file.exists():
            raise FileNotFoundError(f"raw file not found: {raw_file}")

        self._work_dir.mkdir(parents=True, exist_ok=True)
        output_csv = self._work_dir / f"{uuid.uuid4().hex}_report.csv"

        start = time.monotonic()
        try:
            run_result: SkylineRunResult = await run_skyline(
                skyline_exe=skyline_path,
                template=template,
                raw_file=raw_file,
                report_name=report_name,
                output_csv=output_csv,
                timeout_s=self._cfg.timeout_seconds,
                priority=self._cfg.process_priority,
            )
        except SkylineTimeout as exc:
            result.status = ExtractionStatus.FAILED
            result.error_message = str(exc)
            result.extraction_time_ms = int((time.monotonic() - start) * 1000)
            return result

        result.extraction_time_ms = run_result.duration_ms
        result.backend_version = run_result.version
        result.stdout = run_result.stdout
        result.stderr = run_result.stderr

        if run_result.returncode != 0 or has_error_marker(run_result.stdout, run_result.stderr):
            result.status = ExtractionStatus.FAILED
            result.error_message = (
                f"Skyline exited with code {run_result.returncode}: "
                f"{(run_result.stdout or run_result.stderr).strip()[:500]}"
            )
            self._cleanup(output_csv)
            return result

        try:
            target_metrics = parse_skyline_csv(output_csv)
        except FileNotFoundError as exc:
            result.status = ExtractionStatus.FAILED
            result.error_message = f"Skyline did not produce a report file: {exc}"
            return result
        finally:
            self._cleanup(output_csv)

        result.target_metrics = target_metrics
        result.run_metrics = _run_metrics(target_metrics)
        result.template_hash = compute_template_hash(template)
        try:
            result.raw_file_hash = compute_raw_hash(raw_file)
        except OSError as exc:
            logger.warning("raw hash failed for %s: %s", raw_file, exc)

        return result

    @staticmethod
    def _cleanup(path: Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("failed to delete report csv %s: %s", path, exc)


__all__ = [
    "Extractor",
    "SkylineClickOnceUnsupported",
    "SkylineFailed",
    "SkylineNotFound",
    "SkylineTimeout",
    "compute_raw_hash",
    "compute_template_hash",
]
