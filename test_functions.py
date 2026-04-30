"""Manual function tests for mdqc-py troubleshooting."""
import asyncio
import os
import tempfile
import csv
from pathlib import Path

PASS = "PASS"
FAIL = "FAIL"
failures = []

def check(label, condition, detail=""):
    status = PASS if condition else FAIL
    if not condition:
        failures.append(label)
    suffix = f" — {detail}" if detail else ""
    print(f"  [{status}] {label}{suffix}")


# ── 1. Classifier ─────────────────────────────────────────────────────────────
print("\n=== 1. Classifier ===")
from mdqc.classifier import classify_filename
from mdqc.types import ControlType, Confidence

cases = [
    ("SSC0_2024-01-15.raw",          ControlType.SSC0,   Confidence.MEDIUM),
    ("InstrA_QCA_2024-01-15.raw",    ControlType.QC_A,   Confidence.MEDIUM),
    ("InstrA_QCB_A3_2024-01-15.raw", ControlType.QC_B,   Confidence.HIGH),
    ("InstrA_BLANK_2024-01-15.raw",  ControlType.BLANK,  Confidence.MEDIUM),
    ("plain_sample.raw",             ControlType.SAMPLE, Confidence.LOW),
    ("Run_A1_plate01.raw",           ControlType.QC_A,   Confidence.MEDIUM),
]
for name, et, ec in cases:
    r = classify_filename(name)
    check(
        f"classify {name!r}",
        r.control_type == et and r.confidence == ec,
        f"got {r.control_type.value}[{r.confidence.value}], expected {et.value}[{ec.value}]",
    )


# ── 2. Config schema ──────────────────────────────────────────────────────────
print("\n=== 2. Config schema ===")
from mdqc.config.schema import Config, CloudConfig, InstrumentConfig
from mdqc.types import Vendor

cfg = Config()
check("default config constructs", True)
check("default is local-only", cfg.is_local_only())
check("default cert guard is None", cfg.cert_thumbprint_unsupported() is None)

cfg2 = Config(cloud=CloudConfig(api_token="tok"))
check("with api_token not local-only", not cfg2.is_local_only())

cfg3 = Config(cloud=CloudConfig(certificate_thumbprint="A" * 40))
check("cert thumbprint guard fires", cfg3.cert_thumbprint_unsupported() is not None)

try:
    InstrumentConfig(id="  ", vendor=Vendor.THERMO, watch_path=Path("."), file_pattern="*.raw", template="t.sky")
    check("empty instrument id raises", False)
except Exception:
    check("empty instrument id raises", True)

try:
    from mdqc.config.schema import CloudConfig as CC
    CC(certificate_thumbprint="ZZZZ")
    check("bad thumbprint raises", False)
except Exception:
    check("bad thumbprint raises", True)


# ── 3. CSV report parser ──────────────────────────────────────────────────────
print("\n=== 3. CSV report parser ===")
from mdqc.extractor.report import parse_skyline_csv

csv_data = (
    "Peptide Sequence,Precursor Mz,Peptide Retention Time,Total Area,Max Height,Average Mass Error Ppm\n"
    "PEPTIDEK,600.3,12.5,1500000,450000,1.2\n"
    "SAMPLEPEPT,750.4,18.7,2000000,600000,-0.5\n"
    "NOTDETECT,500.2,,0,0,\n"
)
with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8") as f:
    f.write(csv_data)
    tmp_csv = f.name

try:
    metrics = parse_skyline_csv(Path(tmp_csv))
    check("parsed 3 rows", len(metrics) == 3, f"got {len(metrics)}")
    check("first peptide sequence", metrics[0].peptide_sequence == "PEPTIDEK")
    check("first retention time", metrics[0].retention_time == 12.5)
    check("detected when area>0", metrics[0].detected is True)
    check("not detected when area=0", metrics[2].detected is False)
    check("mass error ppm parsed", metrics[0].mass_error_ppm == 1.2)
finally:
    os.unlink(tmp_csv)


# ── 4. Spool store ────────────────────────────────────────────────────────────
print("\n=== 4. Spool store ===")
from mdqc.spool.store import Spool, SpoolFull
from mdqc.types import (
    ExtractionResult, ExtractionStatus, RunClassification,
    Confidence, ClassificationSource, ControlType, TargetMetric, RunMetrics,
)

with tempfile.TemporaryDirectory() as tmpdir:
    spool = Spool(agent_id="test-agent", agent_version="0.1.0", root=Path(tmpdir))
    check("spool dirs created", spool.pending_dir.exists())

    classification = RunClassification(
        control_type=ControlType.QC_A,
        well_position=None,
        instrument_id="instr-1",
        plate_id=None,
        confidence=Confidence.HIGH,
        source=ClassificationSource.FILENAME,
    )
    extraction = ExtractionResult(
        raw_file_path=Path("C:/data/run.raw"),
        template_path=Path("C:/methods/QC_Method.sky"),
        status=ExtractionStatus.SUCCESS,
        backend_version="23.1",
        extraction_time_ms=5000,
        target_metrics=[
            TargetMetric(target_id="abc", peptide_sequence="PEPTIDEK", peak_area=1000.0, detected=True)
        ],
        run_metrics=RunMetrics(
            targets_found=1, targets_expected=1, target_recovery_pct=100.0,
            median_rt_shift=0.1, median_mass_error_ppm=0.5,
        ),
    )

    path = spool.enqueue(classification, extraction)
    check("enqueue returns path", path.exists())
    check("pending count = 1", spool.pending_count() == 1)

    import json
    payload = json.loads(path.read_text())
    check("schema_version is 1.1", payload.get("schema_version") == "1.1")
    check("run.raw_file_name present", "raw_file_name" in payload.get("run", {}))
    check("extraction.template_name present", "template_name" in payload.get("extraction", {}))
    check("run.vendor present", "vendor" in payload.get("run", {}))
    check("run.acquisition_time present", "acquisition_time" in payload.get("run", {}))

    claimed = spool.claim_next()
    check("claim_next returns item", claimed is not None)
    check("pending count = 0 after claim", spool.pending_count() == 0)

    uploading_path, claimed_payload = claimed
    spool.mark_completed(uploading_path)
    check("mark_completed moves file", not uploading_path.exists())


# ── 5. Finalizer retry bug ────────────────────────────────────────────────────
print("\n=== 5. Finalizer retry bug (CODE_REVIEW_FINDINGS #6) ===")
from mdqc.watcher.finalizer import Finalizer
from mdqc.watcher.registry import ProcessedRegistry
from mdqc.config.schema import WatcherConfig

async def test_finalizer_retry():
    registry = ProcessedRegistry.__new__(ProcessedRegistry)
    registry._path = Path(tempfile.mktemp())
    registry._entries = __import__("collections").deque()
    registry._set = set()

    async def noop_callback(path, vendor): pass

    fin = Finalizer(WatcherConfig(), registry=registry, processed_callback=noop_callback)
    p = Path("C:/fake/run.raw")

    await fin.observe(p, Vendor.THERMO)
    check("observe adds tracker", fin.state_of(p) is not None)

    await fin.mark_failed(p, "test failure")
    check("mark_failed removes tracker", fin.state_of(p) is None)

    # BUG: mark_failed should NOT add to processed registry
    in_registry = registry.contains(p)
    check(
        "mark_failed does NOT add to processed registry",
        not in_registry,
        "BUG: failed files are permanently blocked from retry" if in_registry else "ok",
    )

    # After failing, observe should be possible again for retry
    await fin.observe(p, Vendor.THERMO)
    check("re-observe possible after failure (retry works)", fin.state_of(p) is not None)

asyncio.run(test_finalizer_retry())


# ── 6. Activity log ───────────────────────────────────────────────────────────
print("\n=== 6. Activity log ===")
from mdqc.activity_log import ActivityLog, ActivityEntry
from mdqc.types import ExtractionStatus
from datetime import datetime, UTC

with tempfile.TemporaryDirectory() as tmpdir:
    log_path = Path(tmpdir) / "activity.json"
    alog = ActivityLog(path=log_path)
    check("empty log has len 0", len(alog) == 0)

    entry = ActivityEntry(
        path="C:/data/run.raw",
        instrument_id="instr-1",
        timestamp=datetime.now(UTC),
        result=ExtractionStatus.SUCCESS,
        targets_found=10,
        targets_expected=10,
    )
    alog.record(entry)
    check("record adds entry", len(alog) == 1)
    check("persists to disk", log_path.exists())

    alog2 = ActivityLog.load(path=log_path)
    check("reload from disk", len(alog2) == 1)
    check("reload preserves path", alog2.entries[0].path == "C:/data/run.raw")
    check("reload preserves result", alog2.entries[0].result == ExtractionStatus.SUCCESS)


# ── 7. FailedFilesStore ───────────────────────────────────────────────────────
print("\n=== 7. FailedFilesStore ===")
from mdqc.failed_files import FailedFilesStore

with tempfile.TemporaryDirectory() as tmpdir:
    store_path = Path(tmpdir) / "failed.json"
    store = FailedFilesStore(path=store_path)
    check("empty store len 0", len(store) == 0)

    store.add("C:/data/run.raw", "instr-1", "extraction failed")
    check("add entry", len(store) == 1)

    store.add("C:/data/run.raw", "instr-1", "retry failed")
    check("re-add same path increments retry_count", store.entries[0].retry_count == 1)

    store2 = FailedFilesStore.load(path=store_path)
    check("reload from disk", len(store2) == 1)

    store.remove("C:/data/run.raw")
    check("remove entry", len(store) == 0)


# ── 8. Uploader (local-only mode) ────────────────────────────────────────────
print("\n=== 8. Uploader worker (local-only) ===")
from mdqc.uploader import Uploader, UploaderWorker
from mdqc.config.schema import CloudConfig as CC2

with tempfile.TemporaryDirectory() as tmpdir:
    cloud_cfg = CC2()  # no token → local-only
    uploader = Uploader(cloud_cfg, agent_version="0.1.0")
    check("local-only detected", uploader.is_local_only)

    spool2 = Spool(agent_id="test", agent_version="0.1.0", root=Path(tmpdir))
    classification2 = RunClassification(
        control_type=ControlType.QC_A, well_position=None, instrument_id="i1",
        plate_id=None, confidence=Confidence.HIGH, source=ClassificationSource.FILENAME,
    )
    extraction2 = ExtractionResult(
        raw_file_path=Path("C:/data/run.raw"),
        template_path=Path("C:/m/t.sky"),
        status=ExtractionStatus.SUCCESS,
        target_metrics=[],
        run_metrics=RunMetrics(targets_found=0, targets_expected=0, target_recovery_pct=0.0),
    )
    spool2.enqueue(classification2, extraction2)
    check("enqueued for local-only test", spool2.pending_count() == 1)

    worker = UploaderWorker(spool2, uploader)
    result = asyncio.run(worker.upload_one())
    check("upload_one returns True (local-only)", result is True)
    check("payload moved to completed", spool2.pending_count() == 0)


# ── Summary ───────────────────────────────────────────────────────────────────
print()
print("=" * 50)
if failures:
    print(f"FAILED ({len(failures)} failures):")
    for f in failures:
        print(f"  - {f}")
else:
    print("ALL TESTS PASSED")
