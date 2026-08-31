"""Regenerate the reference payloads in docs/examples for the MD platform team.

Run after any change to the payload schema or to the comparison/verdict logic:

    python scripts/gen_example_payloads.py

Payloads are produced by driving the real ``Spool.enqueue`` and
``gold_standards`` code paths rather than being written by hand, so their
shape cannot drift from what an agent actually emits. Values are synthetic and
modelled on the Evosep diagnostic panel at 200 SPD — eight targets across a
~6.4 min gradient. No customer data or credentials appear anywhere.

Deterministic by construction: fixed peptide values, fixed timestamps, and
UUIDs derived from a fixed namespace. Re-running without a code change
produces byte-identical files, so a diff shows only what genuinely moved.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "examples"

# Isolate all agent state; nothing here touches a real install.
_TMP = Path(tempfile.mkdtemp(prefix="mdqc_examples_"))
os.environ["MDQC_DATA_DIR"] = str(_TMP)
sys.path.insert(0, str(REPO / "src"))

from mdqc import __version__  # noqa: E402
from mdqc import gold_standards as gs  # noqa: E402
from mdqc.config.schema import (  # noqa: E402
    PeptideClassRule,
    QcThresholdsConfig,
)
from mdqc.spool import Spool  # noqa: E402
from mdqc.types import (  # noqa: E402
    ClassificationSource,
    Confidence,
    ControlType,
    ExtractionResult,
    ExtractionStatus,
    RunClassification,
    RunMetrics,
    TargetMetric,
    WellPosition,
)

# ── fixed inputs ────────────────────────────────────────────────────────────

NS = uuid.UUID("d1f0c0de-0000-4000-8000-000000000000")
INSTRUMENT = "Astral_0001"
T0 = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)

# protein, peptide, RT (min), peak area at full load, fwhm, mass error
PANEL = [
    ("Non_reactive_Targets", "PVSSAASVYAGAGGSGSR", 1.82, 7_506_081, 0.059, -2.5),
    ("Non_reactive_Targets", "LGGEEVSVACK",        2.41, 1_884_402, 0.055, -2.1),
    ("Non_reactive_Targets", "YNSQNQSNNQFVLYR",    3.05, 2_981_337, 0.061, -3.0),
    ("Non_reactive_Targets", "IGGIGTVPVGR",        3.58, 3_954_118, 0.058, -1.9),
    ("Non_reactive_Targets", "GDLDAASYYAPVR",      4.22, 4_731_006, 0.060, -2.8),
    ("Non_reactive_Targets", "TTPSVVCFK",          4.86, 5_803_774, 0.057, -2.2),
    ("Miss-clevage_pair",    "ISGLIYEETR",         5.44, 6_820_915, 0.062, -2.4),
    ("Miss-clevage_pair",    "RISGLIYEETR",        5.97,   345_602, 0.064, -3.3),
]

PEPTIDE_RULES = [
    PeptideClassRule(protein_name="Non_reactive_Targets", purpose="recovery"),
    PeptideClassRule(
        protein_name="Miss-clevage_pair",
        purpose="digest_efficiency",
        exclude_from_recovery=True,
    ),
]


def uid(tag: str) -> uuid.UUID:
    """Stable UUID so regeneration does not churn every file."""
    return uuid.uuid5(NS, tag)


def targets(
    scale: float = 1.0,
    *,
    rt_shift: float = 0.0,
    nudge: int = 0,
    mis_extract: str | None = None,
) -> list[TargetMetric]:
    """Build the panel at a given load.

    ``nudge`` walks values by a fixed, tiny amount per run so a series has
    realistic spread without randomness. ``mis_extract`` reproduces a wrongly
    integrated target: shifted retention time, depressed dot products and a
    collapsed area — the signature Evosep found on RISGLIYEETR at 500 SPD.
    """
    out = []
    for i, (prot, seq, rt, area, fwhm, mz_err) in enumerate(PANEL):
        drift = ((i * 7 + nudge * 13) % 11 - 5) / 1000.0
        a = area * scale * (1 + drift)
        r = rt + rt_shift + drift / 3
        idotp, dotp = 0.97, 0.94
        if mis_extract and seq == mis_extract:
            a *= 0.34
            r += 1.35
            idotp, dotp = 0.59, 0.61
        out.append(
            TargetMetric(
                target_id=seq,
                peptide_sequence=seq,
                protein_name=prot,
                retention_time=round(r, 3),
                peak_area=round(a),
                peak_width_fwhm=fwhm,
                mass_error_ppm=mz_err,
                isotope_dot_product=idotp,
                library_dot_product=dotp,
                detected=True,
                peptide_class=prot,
                peptide_class_purpose=(
                    "digest_efficiency" if prot == "Miss-clevage_pair" else "recovery"
                ),
                extra_metrics={
                    "Points Across Peak": 11.0,
                    "Total Ion Current Area": 124_980_000.0,
                },
            )
        )
    return out


def classification(
    ct: ControlType, *, well: str, spd: int | None = 200, dilution: int | None = None
) -> RunClassification:
    c = RunClassification(
        control_type=ct,
        well_position=WellPosition.parse(well),
        instrument_id=INSTRUMENT,
        plate_id=None,
        confidence=Confidence.HIGH,
        source=ClassificationSource.FILENAME,
        spd=spd,
    )
    c.dilution_pct = dilution
    return c


def extraction(
    tag: str,
    name: str,
    ms: list[TargetMetric],
    when: datetime,
    *,
    status: ExtractionStatus = ExtractionStatus.SUCCESS,
    error: str | None = None,
) -> ExtractionResult:
    recovery = [m for m in ms if m.peptide_class_purpose == "recovery"]
    return ExtractionResult(
        run_id=uid(tag),
        raw_file_path=Path(r"D:\Data\Astral\QC") / name,
        raw_file_hash="sha256:0000000000000000",
        template_path=Path("QC_Method.sky"),
        template_hash="sha256:1111111111111111",
        backend="skyline",
        backend_version="26.1.0.259",
        extraction_time_ms=21_400,
        status=status,
        error_message=error,
        target_metrics=ms,
        run_metrics=(
            None
            if status is not ExtractionStatus.SUCCESS
            else RunMetrics(
                targets_found=len(recovery),
                targets_expected=len(recovery),
                target_recovery_pct=100.0,
                median_rt_shift=0.004,
                median_mass_error_ppm=-2.4,
                digest_efficiency_pct=95.2,
            )
        ),
        acquired_time=when.isoformat(),
    )


def emit(spool: Spool, cls_, ext, out_name: str, thresholds=None) -> Path:
    """Enqueue through the real spool, then move the payload to docs/examples."""
    ctx, cmp_ = gs.build_payload_comparison(cls_, ext, thresholds or QcThresholdsConfig())
    p = spool.enqueue(cls_, ext, baseline_context=ctx, comparison_metrics=cmp_)
    dst = OUT / out_name
    data = json.loads(Path(p).read_text(encoding="utf-8"))
    # payload_id is random per call; pin it so files are stable across runs.
    data["payload_id"] = str(uid(out_name))
    data["correlation_id"] = f"{data['agent_id']}-20260811090000-{out_name[8:10]}00beef"
    data["timestamp"] = T0.isoformat()
    dst.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    Path(p).unlink(missing_ok=True)
    return dst


def _pin_baseline() -> None:
    """Give the saved baseline a fixed id and timestamp.

    ``save_baseline`` mints a uuid4 and stamps ``datetime.now`` — correct in
    production, but it means every regeneration rewrites baseline_id and
    created_at into all seven payloads that carry a baseline_context, burying
    real schema changes in churn.
    """
    from mdqc.config import paths

    path = paths.baselines_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    for bucket, entry in data.items():
        pinned = {}
        for rec in entry["baselines"].values():
            rec["baseline_id"] = str(uid(f"baseline-{bucket}"))
            rec["created_at"] = T0.isoformat()
            pinned[rec["baseline_id"]] = rec
            entry["active_baseline_id"] = rec["baseline_id"]
        entry["baselines"] = pinned
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def main() -> None:
    spool = Spool(agent_id="astral0001-md", agent_version=__version__)

    # ── baseline: 16 SSC0 runs, the 4th unequilibrated and excluded ─────────
    ssc0_ids = []
    for i in range(16):
        tag = f"ssc0-{i:02d}"
        out = i == 3
        e = extraction(
            tag,
            f"SSC0_2026-08-11_ss_50ng_200spd_k562_S1-{'ABCDEFGH'[i % 8]}{i // 8 + 1}.d",
            targets(scale=0.62 if out else 1.0, rt_shift=0.55 if out else 0.0, nudge=i),
            T0 + timedelta(minutes=7 * i),
        )
        gs.record_ssc0_run(classification(ControlType.SSC0, well=f"{'ABCDEFGH'[i % 8]}{i // 8 + 1}"), e)
        ssc0_ids.append((tag, str(e.run_id), e))

    gs.save_baseline(
        INSTRUMENT, 200,
        [rid for i, (_, rid, _) in enumerate(ssc0_ids) if i != 3],
        label="Install 2026-08-11",
        rules=PEPTIDE_RULES,
    )
    _pin_baseline()

    written = []

    # 01 — an SSC0 run that fed the baseline
    written.append(emit(
        spool, classification(ControlType.SSC0, well="A1"), ssc0_ids[0][2],
        "payload_01_ssc0_baseline_source.json"))

    # 02/03/04 — QC B at 100 / 75 / 50 % load
    for n, (pct, scale, well) in enumerate(
        [(100, 1.00, "A2"), (75, 0.905, "B2"), (50, 0.725, "C2")], start=2
    ):
        name = f"QCB_{pct}perc_2026-08-11_ss_50ng_200spd_k562_S1-{well}.d"
        e = extraction(f"qcb-{pct}", name, targets(scale=scale, nudge=pct),
                       T0 + timedelta(hours=2, minutes=n * 8))
        # 75% is deliberately named "boundary", not "warn": it medians at
        # -9.5%, which clears the stock 10% threshold by half a point and so
        # reads "ok". Payload 06 is the same run on tuned thresholds, where it
        # does warn. The pair is the clearest statement of why the threshold is
        # under review.
        label = {100: "healthy", 75: "75pct_reads_ok", 50: "50pct_fail"}[pct]
        written.append(emit(
            spool,
            classification(ControlType.QC_B, well=well, dilution=pct),
            e, f"payload_0{n}_qcb_{label}.json"))

    # 05 — one wrongly integrated target
    e = extraction(
        "qcb-misx", "QCB_100perc_2026-08-11_ss_50ng_200spd_k562_S1-D2.d",
        targets(nudge=5, mis_extract="RISGLIYEETR"), T0 + timedelta(hours=3))
    written.append(emit(
        spool, classification(ControlType.QC_B, well="D2", dilution=100), e,
        "payload_05_mis_extracted_target.json"))

    # 06 — same run as 03, but on tuned thresholds
    e = extraction("qcb-75-custom", "QCB_75perc_2026-08-11_ss_50ng_200spd_k562_S1-E2.d",
                   targets(scale=0.905, nudge=75), T0 + timedelta(hours=4))
    written.append(emit(
        spool, classification(ControlType.QC_B, well="E2", dilution=75), e,
        "payload_06_custom_thresholds.json",
        thresholds=QcThresholdsConfig(peak_area_deviation_pct_warn=8.0,
                                      peak_area_deviation_pct_fail=20.0)))

    # 07 — PC (QC A): ~20x the SSC-gold load, so the verdict is withheld.
    # Evosep's figure, SOP review annotation 10; an earlier ~6x estimate
    # assumed only ~300 ng of the 1 ug reached the column.
    e = extraction("qca", "PC_2026-08-11_ss_1ug_200spd_S00462_k562_S1-F2.d",
                   targets(scale=19.4, nudge=9), T0 + timedelta(hours=5))
    written.append(emit(
        spool, classification(ControlType.QC_A, well="F2"), e, "payload_07_qca.json"))

    # 08 — an instrument with no baseline yet (different SPD, none recorded)
    e = extraction("qcb-nobase", "QCB_100perc_2026-08-11_ss_50ng_500spd_k562_S1-G2.d",
                   targets(nudge=3), T0 + timedelta(hours=6))
    written.append(emit(
        spool, classification(ControlType.QC_B, well="G2", spd=500, dilution=100), e,
        "payload_08_no_baseline.json"))

    # 09 — failed extraction, still spooled
    e = extraction("qcb-failed", "QCB_100perc_2026-08-11_ss_50ng_200spd_k562_S1-H2.d",
                   [], T0 + timedelta(hours=7),
                   status=ExtractionStatus.FAILED,
                   error="Skyline extraction timed out after 900 s")
    written.append(emit(
        spool, classification(ControlType.QC_B, well="H2", dilution=100), e,
        "payload_09_failed_extraction.json"))

    for f in written:
        print(f"  {f.name:44s} {f.stat().st_size:>7,} bytes")
    print(f"\n{len(written)} payloads written from mdqc v{__version__}")
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
