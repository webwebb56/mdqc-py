"""Hold an instrument's cloud uploads until its gold standard is in place.

Comparisons are computed when a run is spooled, against whatever baseline is
active at that moment. If runs upload before the gold standard is chosen — an
overnight batch on a fresh install, or the runs after a re-baseline — they
reach the platform scored against nothing, or against the old reference, and
the platform cannot replace them. That is what a site hit with 192 plasma runs
on a Sciex 7500.

So, per (instrument, SPD):

* no gold standard yet            -> held, reason "no_baseline"
* gold-standard runs arrive (SSC0) -> held, reason "new_gold_standard",
                                      until a baseline is saved
* an operator pauses it by hand   -> held, reason "manual" — the case where
                                      someone forgot to mark the runs

Held runs sit in spool/held, which the uploader never reads. Saving a baseline
releases them, each re-scored against the new baseline first. A held bucket
can also be released as-is, which then stops holding for lack of a baseline
until one is saved.

Only applies when uploads are configured: a local-only agent uploads nothing,
so there is nothing to hold.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mdqc import gold_standards as gs
from mdqc.config import paths
from mdqc.types import ControlType, TargetMetric

if TYPE_CHECKING:
    from mdqc.config.schema import Config
    from mdqc.spool import Spool
    from mdqc.types import RunClassification

log = logging.getLogger(__name__)

REASON_NO_BASELINE = "no_baseline"
REASON_NEW_GOLD = "new_gold_standard"
REASON_MANUAL = "manual"

REASON_TEXT = {
    REASON_NO_BASELINE: "no gold standard has been saved yet",
    REASON_NEW_GOLD: "new gold-standard runs are waiting to be saved as the baseline",
    REASON_MANUAL: "paused while a new baseline is set",
}


def _state_path() -> Path:
    return paths.gold_standards_dir() / "upload_gate.json"


def _read() -> dict[str, Any]:
    p = _state_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("upload_gate_state_unreadable", extra={"path": str(p)})
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, Any]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def enabled(cfg: Config) -> bool:
    return cfg.cloud.hold_until_gold_standard and not cfg.is_local_only()


def hold_reason(instrument_id: str | None, spd: int | None) -> str | None:
    """Why this bucket's uploads are held right now, or None if they aren't."""
    entry = _read().get(gs._bucket_key(instrument_id, spd), {})
    if entry.get("held"):
        return str(entry.get("reason") or REASON_MANUAL)
    if gs.get_active_baseline(instrument_id, spd) is None and not entry.get(
        "released_without_baseline"
    ):
        return REASON_NO_BASELINE
    return None


def should_hold(cfg: Config, classification: RunClassification, run_id: str) -> bool:
    """Decide where a freshly extracted run goes, noting gold-standard runs."""
    if not enabled(cfg):
        return False
    instrument_id, spd = classification.instrument_id, classification.spd
    if classification.control_type is ControlType.SSC0:
        data = _read()
        key = gs._bucket_key(instrument_id, spd)
        entry = data.get(key, {})
        if not (entry.get("held") and entry.get("reason") == REASON_NEW_GOLD):
            entry = {"held": True, "reason": REASON_NEW_GOLD, "since": _now(), "gold_run_ids": []}
            log.warning(
                "upload_gate_new_gold_standard",
                extra={"instrument_id": instrument_id, "spd": spd},
            )
        entry.setdefault("gold_run_ids", []).append(run_id)
        data[key] = entry
        _write(data)
    return hold_reason(instrument_id, spd) is not None


def pause(instrument_id: str | None, spd: int | None) -> None:
    """Hold uploads by hand — for gold-standard runs nobody marked as such."""
    data = _read()
    key = gs._bucket_key(instrument_id, spd)
    entry = data.get(key, {})
    if not entry.get("held"):
        data[key] = {"held": True, "reason": REASON_MANUAL, "since": _now(), "gold_run_ids": []}
        _write(data)
        log.warning("upload_gate_paused", extra={"instrument_id": instrument_id, "spd": spd})


def gold_run_ids(instrument_id: str | None, spd: int | None) -> list[str]:
    """Runs that arrived marked as gold standards while this bucket was held."""
    entry = _read().get(gs._bucket_key(instrument_id, spd), {})
    return list(entry.get("gold_run_ids", [])) if entry.get("held") else []


def release(
    cfg: Config,
    spool: Spool,
    instrument_id: str | None,
    spd: int | None,
    *,
    rescore: bool = True,
) -> int:
    """Open the gate for a bucket and move its held runs to pending.

    With rescore (after a baseline is saved), each held run is compared
    against the bucket's active baseline before it leaves. Without it (the
    operator's "release as-is"), runs go as they are and the bucket stops
    holding for lack of a baseline until one is saved. Returns the number
    released.
    """
    data = _read()
    key = gs._bucket_key(instrument_id, spd)
    baseline = gs.get_active_baseline(instrument_id, spd)
    data[key] = (
        {"held": False}
        if rescore or baseline is not None
        else {"held": False, "released_without_baseline": True}
    )
    _write(data)

    thresholds = cfg.thresholds_for(spd)
    released = 0
    for path, payload in spool.held_payloads():
        run = payload.get("run") or {}
        if run.get("instrument_id") != instrument_id or run.get("spd") != spd:
            continue
        if rescore and baseline is not None:
            _rescore(payload, baseline, thresholds)
        spool.release_held(path, payload)
        released += 1
    log.info(
        "upload_gate_released",
        extra={"instrument_id": instrument_id, "spd": spd, "released": released, "rescored": rescore},
    )
    return released


def held_summary(spool: Spool) -> list[dict[str, Any]]:
    """One row per held (instrument, SPD): how many runs wait, and why."""
    counts: dict[tuple[str | None, int | None], int] = {}
    for _path, payload in spool.held_payloads():
        run = payload.get("run") or {}
        bucket = (run.get("instrument_id"), run.get("spd"))
        counts[bucket] = counts.get(bucket, 0) + 1
    rows = []
    for (instrument_id, spd), n in sorted(counts.items(), key=lambda kv: str(kv[0])):
        reason = hold_reason(instrument_id, spd) or REASON_NO_BASELINE
        rows.append({
            "instrument_id": instrument_id, "spd": spd, "count": n,
            "reason": reason, "reason_text": REASON_TEXT.get(reason, reason),
        })
    return rows


_TARGET_FIELDS = set(TargetMetric.__dataclass_fields__)


def _rescore(
    payload: dict[str, Any], baseline: dict[str, Any], thresholds: Any
) -> None:
    targets = [
        TargetMetric(**{k: v for k, v in tm.items() if k in _TARGET_FIELDS})
        for tm in payload.get("target_metrics") or []
    ]
    try:
        control_type: ControlType | None = ControlType(payload["run"]["control_type"])
    except (KeyError, ValueError):
        control_type = None
    payload["baseline_context"] = gs.baseline_context(baseline)
    payload["comparison_metrics"] = gs.compute_comparison_metrics(
        targets, baseline, thresholds, control_type=control_type
    )
