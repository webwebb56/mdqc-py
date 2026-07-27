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

# Explicit delimiter classes — see AGENT_NOTES § Classifier (\b matches at
# underscore in Python but not in Rust; use explicit delimiters for parity
# and to handle real `..`/`.-` token separators.)
_DELIM_BEFORE = r"(?:^|[_\-\s.])"
_DELIM_AFTER = r"(?:$|[_\-\s.])"

_SSC0_RE = re.compile(rf"{_DELIM_BEFORE}(SSC[_-]?0|SSC){_DELIM_AFTER}", re.IGNORECASE)
_QCA_RE = re.compile(rf"{_DELIM_BEFORE}(QC[_-]?A|QCA){_DELIM_AFTER}", re.IGNORECASE)
_QCB_RE = re.compile(rf"{_DELIM_BEFORE}(QC[_-]?B|QCB){_DELIM_AFTER}", re.IGNORECASE)
_BLANK_RE = re.compile(rf"{_DELIM_BEFORE}(BLANK|BLK){_DELIM_AFTER}", re.IGNORECASE)
_WELL_RE = re.compile(
    rf"{_DELIM_BEFORE}([A-H])(1[0-2]|0?[1-9]){_DELIM_AFTER}", re.IGNORECASE
)
_PLATE_RE = re.compile(
    rf"{_DELIM_BEFORE}(plate[_-]?\w+|plt[_-]?\w+|P\d{{2,}}){_DELIM_AFTER}",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"\d{4}[-_]\d{2}[-_]\d{2}")
# Evosep "samples per day" — `100spd`, `200SPD`, `500_spd`, etc.
_SPD_RE = re.compile(rf"{_DELIM_BEFORE}(\d{{2,4}})[-_]?SPD{_DELIM_AFTER}", re.IGNORECASE)
# Dilution level of a control — `QCB_100perc`, `75pct`, `50%`. The independent
# variable in a dilution series (Evosep's threshold-calibration stress test
# runs QC B at 100/75/50%), so it needs to be a first-class payload field
# rather than something the platform has to re-parse out of the filename.
_DILUTION_RE = re.compile(
    rf"{_DELIM_BEFORE}(\d{{1,3}})[-_]?(?:perc|pct|percent|%){_DELIM_AFTER}", re.IGNORECASE
)


def classify_file(
    path: Path,
    rules: list[ClassifierRule] | None = None,
) -> RunClassification:
    return classify_filename(path.name, rules=rules)


def classify_filename(
    name: str,
    rules: list[ClassifierRule] | None = None,
) -> RunClassification:
    stem = _strip_vendor_ext(name)

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
                return RunClassification(
                    control_type=ct,
                    well_position=well,
                    instrument_id=instrument,
                    plate_id=plate,
                    confidence=Confidence.HIGH,
                    source=ClassificationSource.FILENAME,
                    spd=spd,
                    dilution_pct=dilution,
                )

    control_type, source = _extract_control_type(stem)
    well = _extract_well(stem)
    plate = _extract_plate(stem)
    instrument = _extract_instrument(stem)
    spd = _extract_spd(stem)
    dilution = _extract_dilution_pct(stem)
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
    )


def _extract_spd(stem: str) -> int | None:
    """Parse Evosep samples-per-day from a filename (e.g. ``200spd``)."""
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
    if _SSC0_RE.search(stem):
        return ControlType.SSC0, ClassificationSource.FILENAME
    if _QCA_RE.search(stem):
        return ControlType.QC_A, ClassificationSource.FILENAME
    if _QCB_RE.search(stem):
        return ControlType.QC_B, ClassificationSource.FILENAME
    if _BLANK_RE.search(stem):
        return ControlType.BLANK, ClassificationSource.FILENAME
    well = _extract_well(stem)
    if well is not None:
        inferred = _infer_from_well(well)
        if inferred is not ControlType.SAMPLE:
            return inferred, ClassificationSource.POSITION
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
    for pat in (_SSC0_RE, _QCA_RE, _QCB_RE, _BLANK_RE, _WELL_RE, _DATE_RE):
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
            return ControlType.QC_A
        if well.column in (3, 4):
            return ControlType.QC_B
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
