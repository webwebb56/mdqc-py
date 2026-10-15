"""Filename-based run classification."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from mdqc.types import (
    ClassificationSource,
    Confidence,
    ControlType,
    RunClassification,
    WellPosition,
)

if TYPE_CHECKING:
    from mdqc.config.schema import ClassifierRule

# Explicit delimiter classes: \b matches at an underscore in Python, so it
# cannot be used here — and explicit delimiters also handle the real
# `..`/`.-` token separators that appear in acquisition filenames.
_DELIM_BEFORE = r"(?:^|[_\-\s.])"
_DELIM_AFTER = r"(?:$|[_\-\s.])"

# Control-type markers, and how they got their names.
#
# August 2026 SOP review: QC A -> PC (process control), QC B -> SSC (system
# suitability control), SSC0 -> SSC-gold. That changed the filename markers
# only; the payload kept sending QC_A and QC_B, on the grounds that renaming a
# wire value is a schema change.
#
# September 2026: the process control became PCD, Process Control Digest,
# written PCD96 or PCD384 for the kit it came from. The vendor asked that customers
# see these names from launch, and the platform shows `control_type` exactly as
# the payload sends it — so this time the wire values changed too (schema 1.3):
# QC_A -> PCD, QC_B -> SSC.
#
# Every earlier spelling is still accepted, so batches named the old way keep
# classifying. Two behaviours worth knowing:
#
#   * a bare `SSC` means a routine system-suitability run. Gold-standard files
#     carry `SSCBASE` (the newest name, Oct 2026), `SSCGOLD` / `SSC_GOLD` or
#     `SSC0` (legacy). All three mean the same thing; the wire value stays
#     `SSC0`, so adding a name needed no schema change.
#   * `PCD96` and `PCD384` are one control type. The number is the kit's plate
#     format and travels separately, as kit_format.
_SSC_GOLD_RE = re.compile(
    rf"{_DELIM_BEFORE}(SSC[_-]?BASE|SSC[_-]?GOLD|SSC[_-]?0){_DELIM_AFTER}", re.IGNORECASE
)
_PCD_RE = re.compile(
    rf"{_DELIM_BEFORE}PCD(?:[_-]?(96|384))?{_DELIM_AFTER}", re.IGNORECASE
)
# Pre-PCD spellings of the process control.
_PC_RE = re.compile(rf"{_DELIM_BEFORE}(PC|QC[_-]?A|QCA){_DELIM_AFTER}", re.IGNORECASE)
_SSC_RE = re.compile(rf"{_DELIM_BEFORE}(SSC|QC[_-]?B|QCB){_DELIM_AFTER}", re.IGNORECASE)

# Legacy aliases — kept so nothing importing these breaks.
_SSC0_RE = _SSC_GOLD_RE
_QCA_RE = _PC_RE
_QCB_RE = _SSC_RE
_BLANK_RE = re.compile(rf"{_DELIM_BEFORE}(BLANK|BLK){_DELIM_AFTER}", re.IGNORECASE)
_WELL_RE = re.compile(
    rf"{_DELIM_BEFORE}([A-H])(1[0-2]|0?[1-9]){_DELIM_AFTER}", re.IGNORECASE
)
_PLATE_RE = re.compile(
    rf"{_DELIM_BEFORE}(plate[_-]?\w+|plt[_-]?\w+|P\d{{2,}}){_DELIM_AFTER}",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"\d{4}[-_]\d{2}[-_]\d{2}")
# LC "samples per day" — `100spd`, `200SPD`, `500_spd`, etc.
_SPD_RE = re.compile(rf"{_DELIM_BEFORE}(\d{{2,4}})[-_]?SPD{_DELIM_AFTER}", re.IGNORECASE)
# Dilution level of a control — `QCB_100perc`, `75pct`, `50%`. The independent
# variable in a dilution series (the threshold-calibration stress test
# runs QC B at 100/75/50%), so it needs to be a first-class payload field
# rather than something the platform has to re-parse out of the filename.
_DILUTION_RE = re.compile(
    rf"{_DELIM_BEFORE}(\d{{1,3}})[-_]?(?:perc|pct|percent|%){_DELIM_AFTER}", re.IGNORECASE
)
# LC serial — `S00462`. These serials are the letter S followed by
# five digits. Anchored to that exact width so it does not swallow arbitrary
# S-prefixed tokens in a customer's naming scheme.
_LC_SERIAL_RE = re.compile(rf"{_DELIM_BEFORE}(S\d{{5}}){_DELIM_AFTER}", re.IGNORECASE)


def classify_file(
    path: Path,
    rules: list[ClassifierRule] | None = None,
    mode: str = "lenient",
) -> RunClassification:
    return classify_filename(path.name, rules=rules, mode=mode)


def classify_filename(
    name: str,
    rules: list[ClassifierRule] | None = None,
    mode: str = "lenient",
) -> RunClassification:
    """Classify a raw file from its name.

    mode="lenient" finds a control-type marker anywhere in the name, in any
    spelling since the first pilot. mode="strict" only accepts one as the first
    part of the name, where automated sample prep writes it, and never infers a type from
    the well — a customer sample in well A1 is still a sample.
    """
    stem = _strip_vendor_ext(name)

    # The shipped default rules are legacy substring matches (QCB, BLK, ...).
    # Under strict naming they would reintroduce exactly the false positives the
    # mode exists to prevent, so only rules an operator wrote apply.
    if mode == "strict" and rules:
        rules = [r for r in rules if not _is_shipped_default_rule(r)]

    # Custom rules (case-insensitive substring) take priority over built-ins.
    if rules:
        for rule in rules:
            if rule.pattern.lower() in stem.lower():
                ct = ControlType(rule.control_type)
                well = _extract_well(stem)
                plate = _extract_plate(stem)
                instrument = _extract_instrument(stem)
                spd = _extract_spd(stem)
                dilution = _extract_dilution_pct(stem)
                lc_serial = _extract_lc_serial(stem)
                return RunClassification(
                    control_type=ct,
                    well_position=well,
                    instrument_id=instrument,
                    plate_id=plate,
                    confidence=Confidence.HIGH,
                    source=ClassificationSource.FILENAME,
                    spd=spd,
                    dilution_pct=dilution,
                    lc_serial=lc_serial,
                    kit_format=_extract_kit_format(stem, ct),
                )

    if mode == "strict":
        control_type, source = _extract_control_type_strict(stem)
    else:
        control_type, source = _extract_control_type(stem)
    well = _extract_well(stem)
    plate = _extract_plate(stem)
    instrument = _extract_instrument(stem)
    spd = _extract_spd(stem)
    dilution = _extract_dilution_pct(stem)
    lc_serial = _extract_lc_serial(stem)
    confidence = _confidence(control_type, well, source)
    return RunClassification(
        control_type=control_type,
        well_position=well,
        instrument_id=instrument,
        plate_id=plate,
        confidence=confidence,
        source=source,
        spd=spd,
        dilution_pct=dilution,
        lc_serial=lc_serial,
        kit_format=_extract_kit_format(stem, control_type),
    )


def _is_shipped_default_rule(rule: ClassifierRule) -> bool:
    from mdqc.config.schema import Config

    return any(
        rule.pattern == d.pattern and rule.control_type == d.control_type
        for d in Config.model_fields["classifier_rules"].default_factory()  # type: ignore[misc]
    )


def _extract_kit_format(stem: str, control_type: ControlType) -> int | None:
    """The digestion kit's plate format (96 or 384) for a process control.

    Read from `PCD96` / `PCD384` whichever way the type was decided — a legacy
    custom rule can still classify a file named the new way. Only a PCD run
    has a kit format; anything else returns ``None``.
    """
    if control_type is not ControlType.PCD:
        return None
    m = _PCD_RE.search(stem)
    return int(m.group(1)) if m and m.group(1) else None


def _extract_lc_serial(stem: str) -> str | None:
    """Parse the LC serial from a filename (e.g. ``S00462``)."""
    m = _LC_SERIAL_RE.search(stem)
    return m.group(1).upper() if m else None


def _extract_spd(stem: str) -> int | None:
    """Parse samples-per-day (SPD) from a filename (e.g. ``200spd``)."""
    m = _SPD_RE.search(stem)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (ValueError, IndexError):
        return None


def _extract_dilution_pct(stem: str) -> int | None:
    """Parse a control's dilution level from a filename (e.g. ``QCB_75perc``).

    Returns ``None`` when the filename carries no dilution marker — the
    common case for routine QC, where the control is run neat.
    """
    m = _DILUTION_RE.search(stem)
    if not m:
        return None
    try:
        value = int(m.group(1))
    except (ValueError, IndexError):
        return None
    return value if 0 < value <= 100 else None


def _strip_vendor_ext(name: str) -> str:
    lower = name.lower()
    for ext in (".raw", ".d", ".wiff", ".wiff.scan"):
        if lower.endswith(ext):
            return name[: -len(ext)]
    return name


def _extract_control_type(
    stem: str,
) -> tuple[ControlType, ClassificationSource]:
    # Gold standard first: `SSC_GOLD` also contains a bare `SSC`.
    if _SSC_GOLD_RE.search(stem):
        return ControlType.SSC0, ClassificationSource.FILENAME
    if _PCD_RE.search(stem) or _PC_RE.search(stem):
        return ControlType.PCD, ClassificationSource.FILENAME
    if _SSC_RE.search(stem):
        return ControlType.SSC, ClassificationSource.FILENAME
    if _BLANK_RE.search(stem):
        return ControlType.BLANK, ClassificationSource.FILENAME
    well = _extract_well(stem)
    if well is not None:
        inferred = _infer_from_well(well)
        if inferred is not ControlType.SAMPLE:
            return inferred, ClassificationSource.POSITION
    return ControlType.SAMPLE, ClassificationSource.DEFAULT


# The QC types automated sample prep writes as the first part of a raw file name.
_FIRST_TOKEN_TYPES = {
    "PCD": ControlType.PCD,
    "PCD96": ControlType.PCD,
    "PCD384": ControlType.PCD,
    "SSC": ControlType.SSC,
    "SSCBASE": ControlType.SSC0,
    "SSC-BASE": ControlType.SSC0,
    "SSCGOLD": ControlType.SSC0,
    "SSC-GOLD": ControlType.SSC0,
    "BLANK": ControlType.BLANK,
}


def _extract_control_type_strict(stem: str) -> tuple[ControlType, ClassificationSource]:
    first = stem.split("_", 1)[0].strip().upper()
    ct = _FIRST_TOKEN_TYPES.get(first)
    if ct is not None:
        return ct, ClassificationSource.FILENAME
    return ControlType.SAMPLE, ClassificationSource.DEFAULT


def _extract_well(stem: str) -> WellPosition | None:
    for m in _WELL_RE.finditer(stem):
        well = WellPosition.parse(f"{m.group(1)}{m.group(2)}")
        if well is not None:
            return well
    return None


def _extract_plate(stem: str) -> str | None:
    m = _PLATE_RE.search(stem)
    return m.group(1) if m else None


def _extract_instrument(stem: str) -> str | None:
    # Leading [A-Z0-9_]+ token before the first control/well/date marker.
    candidates: list[int] = []
    for pat in (_SSC_GOLD_RE, _PCD_RE, _PC_RE, _SSC_RE, _BLANK_RE, _WELL_RE, _DATE_RE):
        m = pat.search(stem)
        if m is not None:
            candidates.append(m.start())
    if not candidates:
        return None
    cut = min(candidates)
    head = stem[:cut].rstrip("_-. ")
    if not head:
        return None
    m = re.match(r"^([A-Za-z0-9]+)", head)
    return m.group(1) if m else None


def _infer_from_well(well: WellPosition) -> ControlType:
    if well.row == "A":
        if well.column in (1, 2):
            return ControlType.PCD
        if well.column in (3, 4):
            return ControlType.SSC
    return ControlType.SAMPLE


def _confidence(
    control_type: ControlType,
    well: WellPosition | None,
    source: ClassificationSource,
) -> Confidence:
    if source is ClassificationSource.FILENAME and control_type.is_qc():
        return Confidence.HIGH if well is not None else Confidence.MEDIUM
    if source is ClassificationSource.POSITION:
        return Confidence.MEDIUM
    return Confidence.LOW
