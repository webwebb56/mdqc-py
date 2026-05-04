"""
Live end-to-end sequential replay.

Copies every real .raw file from EVOSEP_RAW_DIR into the configured watch
folder in chronological order (mtime), waits for each payload to land in
spool/completed/, and records the full QC metrics per run.

Unlike the old simulation (which copied the same file with different names),
this uses the actual instrument files so each payload has a distinct
acquisition_time and real varying metrics — suitable for Levey-Jennings QC.

Usage
-----
    pytest tests/test_live_e2e.py -m live -v -s

    # Preflight only (fast):
    pytest tests/test_live_e2e.py::TestLivePreflight -m live -v -s

Requirements
------------
  - mdqc agent running (tray app or: python -m mdqc run)
  - At least one instrument configured with an accessible watch_path
  - Source raw files at C:\\Mac\\Home\\Documents\\MS Data repo\\Evosep_raw
  - [spool] completed_retention_count >= (number of raw files) in config.toml

Timing
------
  Files are dropped in batches of BATCH_SIZE.  Each batch waits for all files
  to complete (stability window + extraction) before the next batch starts.
  Stability window (~60s) dominates; extraction is ~5-10s per file.
  ~56 files in batches of 10  =>  ~6 batches x ~120s  =>  ~12 min.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, stdev
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import contextlib

from mdqc.config.paths import (
    config_path,
    spool_completed,
)
from mdqc.config.paths import (
    runtime_file as _rt_path_fn,
)
from mdqc.config.schema import Config
from mdqc.ipc.client import IpcClient
from mdqc.ipc.runtime import RuntimeFile

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

MAX_FILES    = 200    # cap on how many sequential files to replay
BATCH_SIZE   = 5      # files dropped into watch folder simultaneously
BATCH_TIMEOUT_S = 600 # per-batch deadline — large files (up to 900 MB) need extra time
POLL_INTERVAL_S = 5.0

DATA_DIR       = Path(r"C:\Mac\Home\Documents\MS Data repo")
EVOSEP_RAW_DIR = DATA_DIR / "Evosep_raw"

# ---------------------------------------------------------------------------
# Real paths — resolved at import time, before conftest redirects MDQC_DATA_DIR
# ---------------------------------------------------------------------------

_REAL_COMPLETED:    Path = spool_completed()
_REAL_RUNTIME_PATH: Path = _rt_path_fn()
_REAL_CONFIG_PATH:  Path = config_path()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _live_config() -> Config | None:
    if not _REAL_CONFIG_PATH.exists():
        return None
    try:
        import tomllib
        raw = tomllib.loads(_REAL_CONFIG_PATH.read_text(encoding="utf-8-sig"))
        return Config.model_validate(raw)
    except Exception:
        return None


_CFG: Config | None = _live_config()


def _make_ipc_client() -> IpcClient | None:
    rt = RuntimeFile(path=_REAL_RUNTIME_PATH)
    info = rt.read()
    if info is None:
        return None
    try:
        return IpcClient(base_url=f"http://127.0.0.1:{info.port}", token=info.token)
    except Exception:
        return None


def _agent_healthy() -> bool:
    client = _make_ipc_client()
    if client is None:
        return False
    try:
        return client.health()
    except Exception:
        return False
    finally:
        with contextlib.suppress(Exception):
            client.close()


def _all_raw_files() -> list[Path]:
    """All .raw files under EVOSEP_RAW_DIR sorted chronologically by mtime."""
    if not EVOSEP_RAW_DIR.exists():
        return []
    files = sorted(EVOSEP_RAW_DIR.rglob("*.raw"), key=lambda p: p.stat().st_mtime)
    return files[:MAX_FILES]


def _watch_path() -> Path | None:
    if _CFG is None or not _CFG.instruments:
        return None
    return _CFG.instruments[0].watch_path


def _already_in_spool(filename: str) -> bool:
    """Return True if a payload for this filename already exists in completed/."""
    try:
        for p in _REAL_COMPLETED.glob("*_payload.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if data.get("run", {}).get("raw_file_name") == filename:
                    return True
            except (json.JSONDecodeError, OSError):
                continue
    except OSError:
        pass
    return False


def _metrics_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    run     = payload.get("run", {})
    rm      = payload.get("run_metrics", {})
    ext     = payload.get("extraction", {})
    targets = payload.get("target_metrics", [])
    detected = [t for t in targets if t.get("detected")]
    rts    = [t["retention_time"] for t in detected if t.get("retention_time") is not None]
    areas  = [t["peak_area"]      for t in detected if t.get("peak_area")      is not None]
    merrs  = [t["mass_error_ppm"] for t in detected if t.get("mass_error_ppm") is not None]
    return {
        "control_type":          run.get("control_type"),
        "acquisition_time":      run.get("acquisition_time"),
        "extraction_time_ms":    ext.get("extraction_time_ms"),
        "targets_found":         rm.get("targets_found"),
        "targets_expected":      rm.get("targets_expected"),
        "target_recovery_pct":   rm.get("target_recovery_pct"),
        "mean_rt":               mean(rts)   if rts   else None,
        "mean_area":             mean(areas) if areas else None,
        "mean_mass_error_ppm":   mean(merrs) if merrs else None,
    }


def _fmt(val: float | None, fmt: str = ".1f", unit: str = "") -> str:
    return f"{val:{fmt}}{unit}" if val is not None else "-"


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

@pytest.mark.live
class TestLivePreflight:
    """Fast checks before committing to the replay run."""

    def test_agent_running(self):
        assert _agent_healthy(), (
            "mdqc agent is not running or not responding.\n"
            "Start it via the tray app or:  python -m mdqc run\n"
            f"Runtime file expected at: {_REAL_RUNTIME_PATH}"
        )

    def test_agent_not_paused(self):
        client = _make_ipc_client()
        if client is None:
            pytest.skip("agent not running")
        try:
            status = client.get_status()
            assert not status.paused, (
                "Agent is PAUSED - resume it before running the live test."
            )
            print(f"\nAgent status: uptime={status.uptime_s}s  "
                  f"pending={status.pending_count}  failed={status.failed_count}")
        finally:
            client.close()

    def test_watch_path_configured(self):
        assert _CFG is not None, (
            f"No config.toml found at {_REAL_CONFIG_PATH}.\n"
            "Run the setup wizard first."
        )
        assert _CFG.instruments, "No instruments configured."
        wp = _watch_path()
        assert wp is not None and wp.exists() and wp.is_dir(), (
            f"Watch path not accessible: {wp}"
        )
        print(f"\nWatch path: {wp}")
        print(f"Instrument: {_CFG.instruments[0].id}")
        print(f"Stability window: {_CFG.watcher.stability_window_seconds}s")

    def test_source_files_available(self):
        files = _all_raw_files()
        assert files, f"No .raw files found under {EVOSEP_RAW_DIR}"
        total_mb = sum(f.stat().st_size for f in files) / 1e6
        print(f"\nFound {len(files)} .raw files  ({total_mb:.0f} MB total)")
        print(f"Oldest: {files[0].name}")
        print(f"Newest: {files[-1].name}")

    def test_retention_count_adequate(self):
        if _CFG is None:
            pytest.skip("no config")
        files = _all_raw_files()
        needed = len(files)
        got = _CFG.spool.completed_retention_count
        if got < needed:
            pytest.fail(
                f"completed_retention_count={got} < {needed} files to replay.\n\n"
                f"Add to config.toml and restart the agent:\n"
                f"  [spool]\n"
                f"  completed_retention_count = {needed + 20}"
            )
        print(f"\nRetention count: {got} (need {needed}) ok")

    def test_timing_estimate(self):
        if _CFG is None:
            pytest.skip("no config")
        files = _all_raw_files()
        sw = _CFG.watcher.stability_window_seconds
        n_batches = (len(files) + BATCH_SIZE - 1) // BATCH_SIZE
        est_batch_s = sw + BATCH_SIZE * 10
        total_min = n_batches * est_batch_s / 60
        print(
            f"\nTiming estimate:"
            f"\n  Files            : {len(files)}"
            f"\n  Batch size       : {BATCH_SIZE}"
            f"\n  Batches          : {n_batches}"
            f"\n  Stability window : {sw}s"
            f"\n  Est. batch time  : ~{est_batch_s}s"
            f"\n  Est. total       : ~{total_min:.0f} min"
        )


# ---------------------------------------------------------------------------
# Sequential replay
# ---------------------------------------------------------------------------

@pytest.mark.live
class TestLiveContinuous:
    """
    Replay all raw files from EVOSEP_RAW_DIR in chronological order.

    Files are dropped into the watch folder in batches of BATCH_SIZE.
    Each batch completes before the next is started.  Files are copied
    under their original names so the classifier handles them correctly
    and acquisition_time comes from the real instrument metadata.

    Files already present in spool/completed/ are skipped (idempotent).
    """

    def test_replay_sequential_files(self):
        if not _agent_healthy():
            pytest.skip("agent not running")
        if _CFG is None or not _CFG.instruments:
            pytest.skip("not configured")

        files = _all_raw_files()
        if not files:
            pytest.skip(f"no .raw files found under {EVOSEP_RAW_DIR}")

        watch_path = _watch_path()
        assert watch_path is not None

        # Skip files already in the spool so re-runs are safe
        todo = [f for f in files if not _already_in_spool(f.name)]
        skipped_existing = len(files) - len(todo)

        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

        print(f"\n{'='*72}")
        print(f"  Sequential Replay  --  {len(todo)} files  (skipping {skipped_existing} already in spool)")
        print(f"  Source dir : {EVOSEP_RAW_DIR}")
        print(f"  Watch path : {watch_path}")
        print(f"  Spool      : {_REAL_COMPLETED}")
        print(f"  Batch size : {BATCH_SIZE}  |  timeout per batch : {BATCH_TIMEOUT_S}s")
        print(f"{'='*72}")

        all_results: list[dict[str, Any]] = []
        placed_files: list[Path] = []

        batches = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]

        try:
            for batch_n, batch in enumerate(batches, start=1):
                batch_start = time.time()
                print(f"\n--- Batch {batch_n:>2}/{len(batches)}  ({len(batch)} files) {'-'*45}")

                # Drop all files in this batch simultaneously
                batch_names: list[str] = []
                for src in batch:
                    dest = watch_path / src.name
                    shutil.copy2(src, dest)
                    placed_files.append(dest)
                    batch_names.append(src.name)

                size_mb = sum(src.stat().st_size for src in batch) / 1e6
                print(f"  Dropped {len(batch)} files ({size_mb:.1f} MB) at T+0s")

                # Poll until all payloads appear
                deadline = time.time() + BATCH_TIMEOUT_S
                pending: set[str] = set(batch_names)
                found: dict[str, dict[str, Any]] = {}

                while pending and time.time() < deadline:
                    try:
                        for p in _REAL_COMPLETED.glob("*_payload.json"):
                            if not pending:
                                break
                            try:
                                data = json.loads(p.read_text(encoding="utf-8"))
                                fname = data.get("run", {}).get("raw_file_name", "")
                                if fname in pending:
                                    found[fname] = data
                                    pending.discard(fname)
                                    elapsed = time.time() - batch_start
                                    ct  = data.get("run", {}).get("control_type", "?")
                                    rec = (data.get("run_metrics", {}).get("target_recovery_pct") or 0)
                                    extr = (data.get("extraction", {}).get("extraction_time_ms") or 0) / 1000
                                    print(
                                        f"  ok [{ct:<6}] {fname:<50} "
                                        f"recovery={rec:5.1f}%  extr={extr:.1f}s  +{elapsed:.0f}s"
                                    )
                            except (json.JSONDecodeError, OSError):
                                continue
                    except OSError:
                        pass
                    if pending:
                        time.sleep(POLL_INTERVAL_S)

                # Record results for this batch
                for src in batch:
                    fname = src.name
                    timed_out = fname in pending
                    payload   = found.get(fname)
                    if timed_out:
                        print(f"  TIMEOUT  {fname}  (>{BATCH_TIMEOUT_S}s)")
                    result: dict[str, Any] = {
                        "batch":      batch_n,
                        "filename":   fname,
                        "source":     str(src),
                        "timed_out":  timed_out,
                        "elapsed_s":  round(time.time() - batch_start, 1),
                    }
                    if payload:
                        result.update(_metrics_from_payload(payload))
                    all_results.append(result)

                batch_elapsed = time.time() - batch_start
                ok = sum(1 for r in all_results[-len(batch):] if not r["timed_out"])
                print(f"  Batch {batch_n} done: {ok}/{len(batch)} OK  ({batch_elapsed:.0f}s)")

        finally:
            # Remove copies from watch folder
            removed = 0
            for dest in placed_files:
                try:
                    if dest.exists():
                        dest.unlink()
                        removed += 1
                except OSError:
                    pass
            if removed:
                print(f"\n  Cleanup: removed {removed} files from watch folder.")

        # Write results JSON
        results_path = Path(__file__).parent / f"live_results_{ts}.json"
        results_path.write_text(
            json.dumps(
                {
                    "meta": {
                        "ts":         ts,
                        "source_dir": str(EVOSEP_RAW_DIR),
                        "total":      len(all_results),
                        "batch_size": BATCH_SIZE,
                    },
                    "results": all_results,
                },
                indent=2, default=str,
            ),
            encoding="utf-8",
        )
        print(f"\n  Results -> {results_path}")

        _print_summary(all_results)

        # Assertions
        total      = len(all_results)
        succeeded  = sum(1 for r in all_results if not r["timed_out"])
        success_rate = succeeded / total if total else 0
        assert success_rate >= 0.95, (
            f"Pipeline success rate {success_rate*100:.0f}% ({succeeded}/{total}). "
            "Check agent logs for extraction failures."
        )


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def _print_summary(results: list[dict[str, Any]]) -> None:
    print(f"\n{'='*72}")
    print("  SUMMARY BY CONTROL TYPE")
    print(f"{'='*72}")

    by_ct: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        ct = str(r.get("control_type") or "unknown")
        by_ct.setdefault(ct, []).append(r)

    def _ms(key: str, rows: list[dict[str, Any]]) -> str:
        vals = [r[key] for r in rows if r.get(key) is not None]
        if not vals:
            return "-"
        m = mean(vals)
        s = stdev(vals) if len(vals) > 1 else 0.0
        return f"{m:.1f}+/-{s:.1f}"

    header = f"  {'Control':<12} {'N':>5} {'Timeout':>8} {'Recovery%':>12} {'RT mean':>10} {'Area mean':>12}"
    print(header)
    print(f"  {'-'*72}")

    for ct, rows in sorted(by_ct.items()):
        ok      = [r for r in rows if not r["timed_out"]]
        n_ok    = len(ok)
        n_fail  = len(rows) - n_ok
        recovery = _ms("target_recovery_pct", ok)
        rt       = _ms("mean_rt", ok)
        area     = _ms("mean_area", ok)
        print(f"  {ct:<12} {n_ok:>5} {n_fail:>8} {recovery:>12} {rt:>10} {area:>12}")

    total     = len(results)
    succeeded = sum(1 for r in results if not r["timed_out"])
    print(f"\n  Total: {succeeded}/{total} succeeded")

    # Recovery distribution
    recs = [r["target_recovery_pct"] for r in results
            if not r["timed_out"] and r.get("target_recovery_pct") is not None]
    if recs:
        print(f"  Recovery:  mean={mean(recs):.1f}%  "
              f"min={min(recs):.1f}%  max={max(recs):.1f}%  "
              f"sd={stdev(recs):.1f}%" if len(recs) > 1 else
              f"  Recovery:  {recs[0]:.1f}%")
    print(f"{'='*72}")
