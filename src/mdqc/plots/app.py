"""
QC Dashboard — Streamlit app for visualizing QC metrics from the MD QC Agent spool.

Reads *_payload.json files from the spool directory (pending + completed)
and renders time-series line plots (one per metric) with individual
peptide/feature traces colored by target_id.

Supports two chart modes:
  - Raw Values: simple time-series traces
  - Levey-Jennings: control charts with mean/SD lines and Westgard rule annotations
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

# Ensure the package is importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mdqc.config.paths import spool_dir

# ---------------------------------------------------------------------------
# CLI arguments (so the tray / dashboard Start button can pass --folder)
# ---------------------------------------------------------------------------
_parser = argparse.ArgumentParser()
_parser.add_argument("--folder", default=None, help="Spool folder path")
_cli_args, _ = _parser.parse_known_args()

# ---------------------------------------------------------------------------
# Defaults & known metrics
# ---------------------------------------------------------------------------

# Known metrics in preferred display order: (column_name, display_label, is_log_candidate)
KNOWN_METRIC_DEFS: list[tuple[str, str, bool]] = [
    ("retention_time", "Retention Time (min)", False),
    ("rt_delta", "RT Deviation (min)", False),
    ("peak_area", "Peak Area", True),
    ("peak_height", "Peak Height", False),
    ("peak_width_fwhm", "FWHM (min)", False),
    ("mass_error_ppm", "Mass Error (ppm)", False),
    ("isotope_dot_product", "Isotope Dot Product", False),
    ("library_dot_product", "Library Dot Product", False),
]

_KNOWN_LABELS: dict[str, str] = {col: label for col, label, _ in KNOWN_METRIC_DEFS}

# Metadata columns that should not be treated as metrics. Includes Skyline-
# report extras that look numeric but are constant per target (e.g. "Precursor
# Charge" arrives via extra_metrics) and so are useless on Levey-Jennings.
_META_COLS = {
    "timestamp", "acquisition_time", "instrument_id", "raw_file_name",
    "control_type", "spd", "method_name", "column_info", "target_id",
    "target_label", "protein_name", "peptide_class", "peptide_class_purpose",
    "peptide_sequence", "precursor_mz", "precursor_charge", "detected",
    "targets_found", "targets_expected", "median_rt_shift",
    "median_mass_error_ppm",
    # Constants per target — never useful as LJ metrics
    "Precursor Charge", "Charge", "Mz",
}


# ---------------------------------------------------------------------------
# Metric auto-discovery
# ---------------------------------------------------------------------------
def discover_metrics(df: pd.DataFrame) -> list[tuple[str, str, bool]]:
    """Discover all plottable metrics from the DataFrame.

    Returns a list of (column_name, display_label, is_log_candidate) tuples.
    Known metrics appear first in their preferred order, then any extra
    numeric columns are appended alphabetically.
    """
    result: list[tuple[str, str, bool]] = []
    seen: set[str] = set()

    # Known metrics first (in preferred order), only if present
    for col, label, is_log in KNOWN_METRIC_DEFS:
        if col in df.columns and df[col].notna().any():
            result.append((col, label, is_log))
            seen.add(col)

    # Discover extra numeric columns (e.g. anything added to the Skyline
    # report template that isn't in KNOWN_METRIC_DEFS).
    for col in sorted(df.columns):
        if col in seen or col in _META_COLS:
            continue
        if pd.api.types.is_numeric_dtype(df[col]) and df[col].notna().any():
            result.append((col, col, False))
            seen.add(col)

    return result


# ---------------------------------------------------------------------------
# Run exclusions (sidebar UI lets the user mark first-of-series outliers etc.
# as excluded from baseline computation without deleting the underlying data)
# ---------------------------------------------------------------------------
def _exclusions_path(folder: str) -> Path:
    return Path(folder) / "exclusions.json"


def load_exclusions(folder: str) -> set[str]:
    """Read the set of excluded raw-file names from ``<folder>/exclusions.json``.

    Returns an empty set if the file is absent or malformed — the dashboard
    should never crash because exclusions are missing.
    """
    p = _exclusions_path(folder)
    if not p.is_file():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("excluded", [])
        if not isinstance(data, list):
            return set()
        return {str(x) for x in data}
    except (OSError, json.JSONDecodeError):
        return set()


def save_exclusions(folder: str, excluded: set[str]) -> None:
    """Persist the exclusion set. Atomic via tmp + rename; silently no-op on
    write failure (read-only network folders, etc.)."""
    p = _exclusions_path(folder)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"excluded": sorted(excluded)}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(p)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
@st.cache_data(ttl=30)
def load_payloads(folder: str) -> pd.DataFrame:
    """Scan *folder* (and pending/completed subdirs) for payload JSON files."""
    records: list[dict] = []
    folder_path = Path(folder)
    if not folder_path.is_dir():
        return pd.DataFrame()

    search_dirs = [folder_path]
    for subdir in ("pending", "completed"):
        child = folder_path / subdir
        if child.is_dir():
            search_dirs.append(child)

    seen: set[str] = set()
    payload_files: list[Path] = []
    for d in search_dirs:
        for f in d.glob("*_payload.json"):
            if f.name not in seen:
                seen.add(f.name)
                payload_files.append(f)
    payload_files.sort()

    for fpath in payload_files:
        try:
            with open(fpath, encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        timestamp = payload.get("timestamp")
        run = payload.get("run", {})
        acquisition_time = run.get("acquisition_time") or timestamp
        instrument_id = run.get("instrument_id", "unknown")
        raw_file_name = run.get("raw_file_name", fpath.stem)
        control_type = run.get("control_type", "")
        spd = run.get("spd")
        method_name = run.get("method_name")
        column_info = run.get("column_info")

        run_metrics = payload.get("run_metrics", {})

        for target in payload.get("target_metrics", []):
            peptide_seq = target.get("peptide_sequence") or ""
            charge = target.get("extra_metrics", {}).get("Precursor Charge")
            # Human-readable label: peptide+charge if available, else target_id, else hash
            if peptide_seq:
                label = f"{peptide_seq}+{int(charge)}" if charge else peptide_seq
            else:
                label = target.get("target_id", "") or "unknown"
            rec = {
                "timestamp": timestamp,
                "acquisition_time": acquisition_time,
                "instrument_id": instrument_id,
                "raw_file_name": raw_file_name,
                "control_type": control_type,
                "spd": spd,
                "method_name": method_name,
                "column_info": column_info,
                "target_id": target.get("target_id", ""),
                "target_label": label,
                "protein_name": target.get("protein_name", ""),
                "peptide_class": target.get("peptide_class") or "",
                "peptide_class_purpose": target.get("peptide_class_purpose") or "",
                "peptide_sequence": peptide_seq,
                "precursor_mz": target.get("precursor_mz"),
                "precursor_charge": target.get("precursor_charge"),
                "retention_time": target.get("retention_time"),
                "rt_delta": target.get("rt_delta"),
                "peak_area": target.get("peak_area"),
                "peak_height": target.get("peak_height"),
                "peak_width_fwhm": target.get("peak_width_fwhm"),
                "mass_error_ppm": target.get("mass_error_ppm"),
                "isotope_dot_product": target.get("isotope_dot_product"),
                "library_dot_product": target.get("library_dot_product"),
                "detected": target.get("detected", False),
                # Run-level summary (repeated per target for easy filtering)
                "targets_found": run_metrics.get("targets_found"),
                "targets_expected": run_metrics.get("targets_expected"),
                "median_rt_shift": run_metrics.get("median_rt_shift"),
                "median_mass_error_ppm": run_metrics.get("median_mass_error_ppm"),
            }
            for k, v in target.get("extra_metrics", {}).items():
                if k not in rec:
                    rec[k] = v
            records.append(rec)

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    for col in ("timestamp", "acquisition_time"):
        df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)
    return df.sort_values("acquisition_time").reset_index(drop=True)


def load_manifest(folder: str) -> dict | None:
    """Load manifest.json from the spool root, if present."""
    manifest_path = Path(folder) / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        with open(manifest_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Westgard rule evaluation
# ---------------------------------------------------------------------------
def evaluate_westgard(values: pd.Series, mean: float, sd: float) -> list[str]:
    """Evaluate Westgard rules for a series of values.

    Returns a list of rule labels per data point:
      'ok'   — within 2 SD (normal)
      '1-2s' — warning: single point > 2 SD from mean
      '1-3s' — reject: single point > 3 SD from mean
      '2-2s' — reject: 2 consecutive points > 2 SD on same side
      'R-4s' — reject: 2 consecutive points spanning > 4 SD range
    """
    if sd == 0 or len(values) == 0:
        return ["ok"] * len(values)

    z_scores = ((values - mean) / sd).to_numpy()
    n = len(z_scores)
    labels = ["ok"] * n

    for i in range(n):
        z = abs(z_scores[i])
        if z > 3:
            labels[i] = "1-3s"
        elif z > 2:
            labels[i] = "1-2s"

    for i in range(1, n):
        z_curr = z_scores[i]
        z_prev = z_scores[i - 1]

        # R-4s: consecutive points on opposite sides, range > 4 SD
        opposite_sides = (z_curr > 2 and z_prev < -2) or (z_curr < -2 and z_prev > 2)
        if opposite_sides and abs(z_curr - z_prev) > 4:
            if labels[i] != "1-3s":
                labels[i] = "R-4s"
            if labels[i - 1] != "1-3s":
                labels[i - 1] = "R-4s"

        # 2-2s: 2 consecutive points > 2 SD on the same side
        if abs(z_curr) > 2 and abs(z_prev) > 2 and (z_curr > 0) == (z_prev > 0):
            if labels[i] not in ("1-3s", "R-4s"):
                labels[i] = "2-2s"
            if labels[i - 1] not in ("1-3s", "R-4s"):
                labels[i - 1] = "2-2s"

    return labels


_WESTGARD_STYLE = {
    "ok":   {"color": "#2ca02c", "symbol": "circle",      "size": 6},
    "1-2s": {"color": "#f0ad4e", "symbol": "diamond",     "size": 8},
    "1-3s": {"color": "#d9534f", "symbol": "triangle-up", "size": 9},
    "2-2s": {"color": "#d9534f", "symbol": "triangle-up", "size": 9},
    "R-4s": {"color": "#d9534f", "symbol": "triangle-up", "size": 9},
}


# ---------------------------------------------------------------------------
# Plotting — Raw Values
# ---------------------------------------------------------------------------
def build_figure(
    df: pd.DataFrame,
    log_area: bool,
    metric_defs: list[tuple[str, str, bool]],
    time_col: str = "acquisition_time",
) -> go.Figure:
    """Build a subplot figure with one trace per target_id per metric."""
    n_metrics = len(metric_defs)
    subplot_titles = [m[1] for m in metric_defs]

    fig = make_subplots(
        rows=n_metrics,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        subplot_titles=subplot_titles,
    )

    label_col = "target_label" if "target_label" in df.columns else "target_id"
    target_labels = sorted(df[label_col].dropna().unique())
    colors = _color_palette(len(target_labels))
    color_map = dict(zip(target_labels, colors, strict=False))

    for row_idx, (col_name, _label, _is_log) in enumerate(metric_defs, start=1):
        if col_name not in df.columns:
            continue
        sub = df.dropna(subset=[col_name])
        if sub.empty:
            continue

        for tid in target_labels:
            tdf = sub[sub[label_col] == tid]
            if tdf.empty:
                continue
            fig.add_trace(
                go.Scatter(
                    x=tdf[time_col],
                    y=tdf[col_name],
                    mode="lines+markers",
                    name=tid,
                    legendgroup=tid,
                    showlegend=(row_idx == 1),
                    marker={"size": 4},
                    line={"color": color_map[tid], "width": 1.5},
                    hovertemplate=(
                        "<b>%{customdata[0]}</b><br>"
                        "%{customdata[1]}<br>"
                        "Value: %{y:.4g}<extra></extra>"
                    ),
                    customdata=list(zip(tdf["raw_file_name"], tdf["peptide_sequence"], strict=False)),
                ),
                row=row_idx,
                col=1,
            )

        if col_name == "peak_area" and log_area:
            fig.update_yaxes(type="log", row=row_idx, col=1)

    fig.update_layout(
        height=280 * n_metrics,
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.01,
            "xanchor": "left",
            "x": 0,
            "font": {"size": 10},
        },
        margin={"l": 60, "r": 20, "t": 80, "b": 40},
    )
    x_title = "Payload Time" if time_col == "timestamp" else "Acquisition Time"
    fig.update_xaxes(title_text=x_title, row=n_metrics, col=1)
    return fig


# ---------------------------------------------------------------------------
# Plotting — Levey-Jennings
# ---------------------------------------------------------------------------
def build_lj_figure(
    df: pd.DataFrame,
    metric_defs: list[tuple[str, str, bool]],
    baseline_mode: str = "All runs",
    baseline_n: int = 20,
    time_col: str = "acquisition_time",
) -> go.Figure:
    """Build a Levey-Jennings chart with z-score normalization and Westgard rules."""
    n_metrics = len(metric_defs)
    subplot_titles = [f"{m[1]}  (SD from mean)" for m in metric_defs]

    fig = make_subplots(
        rows=n_metrics,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        subplot_titles=subplot_titles,
    )

    label_col = "target_label" if "target_label" in df.columns else "target_id"
    target_labels = sorted(df[label_col].dropna().unique())
    colors = _color_palette(len(target_labels))
    color_map = dict(zip(target_labels, colors, strict=False))

    legend_shown: set[str] = set()

    for row_idx, (col_name, _label, _is_log) in enumerate(metric_defs, start=1):
        if col_name not in df.columns:
            continue
        sub = df.dropna(subset=[col_name])
        if sub.empty:
            continue

        for tid in target_labels:
            tdf = sub[sub[label_col] == tid].copy()
            if tdf.empty:
                continue

            vals = tdf[col_name]

            if baseline_mode == "Last N runs":
                baseline_vals = vals.iloc[-baseline_n:]
            elif baseline_mode == "Last N days":
                cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=baseline_n)
                baseline_mask = tdf[time_col] >= cutoff
                baseline_vals = vals[baseline_mask]
                if len(baseline_vals) < 2:
                    baseline_vals = vals
            else:
                baseline_vals = vals

            mean_val = baseline_vals.mean()
            sd_val = baseline_vals.std()

            if len(tdf) < 2 or sd_val == 0:
                z_scores = pd.Series(np.zeros(len(vals)), index=vals.index)
                westgard_labels = ["ok"] * len(vals)
            else:
                z_scores = (vals - mean_val) / sd_val
                westgard_labels = evaluate_westgard(vals, mean_val, sd_val)

            tdf["_z"] = z_scores.values

            for status in ("ok", "1-2s", "1-3s", "2-2s", "R-4s"):
                mask = [w == status for w in westgard_labels]
                if not any(mask):
                    continue
                pts = tdf[mask]
                style = _WESTGARD_STYLE[status]

                show_legend = False
                legend_name = tid
                if status == "ok":
                    if row_idx == 1 and tid not in legend_shown:
                        show_legend = True
                        legend_shown.add(tid)
                else:
                    if status not in legend_shown:
                        show_legend = True
                        legend_shown.add(status)
                    legend_name = f"{status} violation"

                fig.add_trace(
                    go.Scatter(
                        x=pts[time_col],
                        y=pts["_z"],
                        mode="markers",
                        name=legend_name,
                        legendgroup=tid if status == "ok" else status,
                        showlegend=show_legend,
                        marker={
                            "size": style["size"],
                            "symbol": style["symbol"],
                            "color": style["color"] if status != "ok" else color_map[tid],
                            "line": {"width": 1, "color": "#333"},
                        },
                        hovertemplate=(
                            f"<b>{tid}</b><br>"
                            f"Status: {status}<br>"
                            "z-score: %{y:.2f} SD<br>"
                            f"Raw: %{{customdata[0]:.4g}}<br>"
                            f"Mean: {mean_val:.4g}, SD: {sd_val:.4g}"
                            "<extra></extra>"
                        ),
                        customdata=list(zip(pts[col_name])),
                    ),
                    row=row_idx,
                    col=1,
                )

        _add_control_lines(fig, row_idx)
        fig.update_yaxes(range=[-4.5, 4.5], row=row_idx, col=1)

    fig.update_layout(
        height=320 * n_metrics,
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.01,
            "xanchor": "left",
            "x": 0,
            "font": {"size": 10},
        },
        margin={"l": 60, "r": 20, "t": 80, "b": 40},
    )
    x_title = "Payload Time" if time_col == "timestamp" else "Acquisition Time"
    fig.update_xaxes(title_text=x_title, row=n_metrics, col=1)
    return fig


def _add_control_lines(fig: go.Figure, row: int) -> None:
    fig.add_hline(y=0, row=row, col=1,
                  line={"color": "#1f77b4", "width": 1.5, "dash": "solid"}, opacity=0.6)
    for mult in (-1, 1):
        fig.add_hline(y=mult, row=row, col=1,
                      line={"color": "#2ca02c", "width": 1, "dash": "dash"}, opacity=0.5)
    for mult in (-2, 2):
        fig.add_hline(y=mult, row=row, col=1,
                      line={"color": "#f0ad4e", "width": 1.2, "dash": "dash"}, opacity=0.6)
    for mult in (-3, 3):
        fig.add_hline(y=mult, row=row, col=1,
                      line={"color": "#d9534f", "width": 1.5, "dash": "dash"}, opacity=0.7)


def _color_palette(n: int) -> list[str]:
    palette = px.colors.qualitative.Dark24 + px.colors.qualitative.Light24
    return [palette[i % len(palette)] for i in range(n)]


# ---------------------------------------------------------------------------
# Single-page helpers — RT-binned grouping, scorecard, decision summary.
# ---------------------------------------------------------------------------
# Refined Tailwind-inspired palette — same hue families, more designed.
_RT_BIN_COLORS = {
    "Early (<2 min)":     "#3b82f6",   # blue-500
    "Mid (2–3 min)":      "#10b981",   # emerald-500
    "Late (3–4.5 min)":   "#f59e0b",   # amber-500
    "Very late (≥4.5)":   "#ef4444",   # red-500
    "Unknown":            "#94a3b8",   # slate-400
}


def _assign_rt_bin(rt: float | None) -> str:
    if rt is None or pd.isna(rt):
        return "Unknown"
    if rt < 2:
        return "Early (<2 min)"
    if rt < 3:
        return "Mid (2–3 min)"
    if rt < 4.5:
        return "Late (3–4.5 min)"
    return "Very late (≥4.5)"


def _zscore_per_peptide(
    df: pd.DataFrame, metric_col: str, baseline_mode: str, baseline_n: int, time_col: str,
) -> pd.DataFrame:
    """Add a `_z` column to df, computed per-peptide using the chosen baseline window."""
    out = df.copy()
    out["_z"] = np.nan
    label_col = "target_label" if "target_label" in out.columns else "target_id"
    for _label, sub in out.groupby(label_col):
        vals = sub[metric_col].dropna()
        if len(vals) < 2:
            continue
        if baseline_mode == "Last N runs":
            base = vals.iloc[-baseline_n:]
        elif baseline_mode == "Last N days":
            cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=baseline_n)
            mask = sub.loc[vals.index, time_col] >= cutoff
            base = vals[mask]
            if len(base) < 2:
                base = vals
        else:
            base = vals
        m, s = base.mean(), base.std()
        if s == 0 or pd.isna(s):
            continue
        out.loc[sub.index, "_z"] = (sub[metric_col] - m) / s
    return out


def build_grouped_lj(
    df: pd.DataFrame,
    metric_col: str,
    metric_label: str,
    time_col: str,
    baseline_mode: str,
    baseline_n: int,
    height: int = 380,
    value_mode: str = "z",
) -> go.Figure:
    """Single LJ-style chart with peptides grouped by retention-time bin.

    ``value_mode`` controls the y-axis transformation:
      - ``"z"`` (default) — z-score relative to per-peptide baseline; Westgard
        ±σ guide lines drawn at 0/±1/±2/±3. The classic LJ control chart.
      - ``"raw"`` — plot the metric value as-is, median + IQR per RT bin.
        No guide lines, no z-conversion.
      - ``"log"`` — same as "raw" but log10-transformed Y axis. Useful for
        intensity-style metrics (peak area / height) that span decades.
    """
    if metric_col not in df.columns:
        return go.Figure().update_layout(height=height, title=f"{metric_label} — column not found")

    df = df.dropna(subset=[metric_col, time_col]).copy()
    if df.empty:
        return go.Figure().update_layout(
            height=height, title=f"{metric_label} — no data"
        )

    # Use each peptide's median observed RT as its bin assignment (stable across runs)
    rt_per_peptide = df.groupby("peptide_sequence")["retention_time"].median() \
        if "retention_time" in df.columns else None
    if rt_per_peptide is not None:
        df["_rt_bin"] = df["peptide_sequence"].map(lambda p: _assign_rt_bin(rt_per_peptide.get(p)))
    else:
        df["_rt_bin"] = "Unknown"

    if value_mode == "z":
        df = _zscore_per_peptide(df, metric_col, baseline_mode, baseline_n, time_col)
        df = df.dropna(subset=["_z"])
        if df.empty:
            return go.Figure().update_layout(
                height=height, title=f"{metric_label} — insufficient data for z-scores"
            )
        value_col = "_z"
        y_axis_title = f"{metric_label} (SD from baseline)"
        y_range = [-4.5, 4.5]
        y_type = "linear"
        draw_westgard = True
        violation_thresh = 2
    elif value_mode == "log":
        # Drop non-positive values — log of zero or negative is undefined.
        df = df[df[metric_col] > 0].copy()
        if df.empty:
            return go.Figure().update_layout(
                height=height, title=f"{metric_label} — no positive values to log-transform"
            )
        value_col = metric_col
        y_axis_title = f"{metric_label} (log scale)"
        y_range = None
        y_type = "log"
        draw_westgard = False
        violation_thresh = None
    else:  # "raw"
        value_col = metric_col
        y_axis_title = metric_label
        y_range = None
        y_type = "linear"
        draw_westgard = False
        violation_thresh = None

    fig = go.Figure()

    if draw_westgard:
        # Westgard threshold lines (drawn first so traces sit on top)
        for y, dash, color in [
            (0,  "solid", "#1f77b4"), (1,  "dash", "#2ca02c"), (-1, "dash", "#2ca02c"),
            (2,  "dash", "#f0ad4e"), (-2, "dash", "#f0ad4e"),
            (3,  "dash", "#d9534f"), (-3, "dash", "#d9534f"),
        ]:
            fig.add_hline(y=y, line={"color": color, "width": 1, "dash": dash}, opacity=0.4)

    bin_order = ["Early (<2 min)", "Mid (2–3 min)", "Late (3–4.5 min)", "Very late (≥4.5)", "Unknown"]
    seen_bins = [b for b in bin_order if b in df["_rt_bin"].unique()]

    for rt_bin in seen_bins:
        sub = df[df["_rt_bin"] == rt_bin]
        if sub.empty:
            continue
        agg = sub.groupby(time_col)[value_col].agg(["median", "min", "max",
                                                lambda s: s.quantile(0.25),
                                                lambda s: s.quantile(0.75),
                                                "count"]).reset_index()
        agg.columns = [time_col, "median", "min", "max", "q25", "q75", "n"]
        agg = agg.sort_values(time_col)
        color = _RT_BIN_COLORS.get(rt_bin, "#7f7f7f")

        # IQR band
        fig.add_trace(go.Scatter(
            x=pd.concat([agg[time_col], agg[time_col][::-1]]),
            y=pd.concat([agg["q75"], agg["q25"][::-1]]),
            fill="toself", fillcolor=color, opacity=0.12,
            line={"width": 0}, showlegend=False, hoverinfo="skip",
            name=f"{rt_bin} IQR",
        ))
        # Median line + markers — in z-mode, points outside ±2σ get diamond
        # markers; in raw/log modes everything's a plain circle.
        if violation_thresh is not None:
            violation_mask = (agg["min"] < -violation_thresh) | (agg["max"] > violation_thresh)
            marker_sizes = [10 if v else 6 for v in violation_mask]
            marker_symbols = ["diamond" if v else "circle" for v in violation_mask]
        else:
            marker_sizes = [6] * len(agg)
            marker_symbols = ["circle"] * len(agg)
        fig.add_trace(go.Scatter(
            x=agg[time_col], y=agg["median"],
            mode="lines+markers",
            name=f"{rt_bin} (n={int(sub['peptide_sequence'].nunique())})",
            line={"color": color, "width": 2},
            marker={
                "size": marker_sizes,
                "color": color,
                "symbol": marker_symbols,
                "line": {"width": 1, "color": "#333"},
            },
            hovertemplate=(
                f"<b>{rt_bin}</b><br>"
                "Time: %{x}<br>"
                "Median: %{y}<br>"
                "IQR: [%{customdata[0]}, %{customdata[1]}]<br>"
                "Range: [%{customdata[2]}, %{customdata[3]}]<br>"
                "n peptides: %{customdata[4]}<extra></extra>"
            ),
            customdata=agg[["q25", "q75", "min", "max", "n"]].values,
        ))

    yaxis_kwargs = {"title": y_axis_title, "type": y_type}
    if y_range is not None:
        yaxis_kwargs["range"] = y_range
    fig.update_layout(
        height=height,
        margin={"l": 40, "r": 10, "t": 30, "b": 30},
        yaxis=yaxis_kwargs,
        xaxis={"title": "Acquisition Time" if time_col == "acquisition_time" else "Payload Time"},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "left", "x": 0, "font": {"size": 10}},
        hovermode="x unified",
    )
    return fig


def build_all_metrics_grid(
    df: pd.DataFrame,
    metric_defs: list[tuple[str, str, bool]],
    time_col: str,
    baseline_mode: str,
    baseline_n: int,
    height: int = 600,
    n_cols: int = 3,
    value_mode: str = "z",
) -> go.Figure:
    """Small-multiples grid: one compact LJ panel per metric, peptides grouped
    by RT-bin. Shows every metric on the same page."""
    metric_cols_all = [(c, lbl) for c, lbl, _ in metric_defs if c in df.columns]
    if not metric_cols_all:
        return go.Figure().update_layout(height=height, title="No metrics available")

    # Pre-compute RT bin per peptide once (stable across panels)
    if "retention_time" in df.columns:
        rt_per_peptide = df.groupby("peptide_sequence")["retention_time"].median()
    else:
        rt_per_peptide = pd.Series(dtype=float)
    df = df.copy()
    df["_rt_bin"] = df["peptide_sequence"].map(lambda p: _assign_rt_bin(rt_per_peptide.get(p)))

    # Pre-compute per-metric working frames. In z-mode we compute z-scores
    # per peptide (skipping columns where every peptide has <2 observations);
    # in raw/log modes we use the values directly. Columns that produce no
    # data after the relevant transform are dropped.
    metric_cols: list[tuple[str, str]] = []
    data_by_metric: dict[str, tuple[pd.DataFrame, str]] = {}
    for col, lbl in metric_cols_all:
        sub = df.dropna(subset=[col, time_col])
        if sub.empty:
            continue
        if value_mode == "z":
            scored = _zscore_per_peptide(sub, col, baseline_mode, baseline_n, time_col)
            if scored["_z"].notna().any():
                metric_cols.append((col, lbl))
                data_by_metric[col] = (scored, "_z")
        elif value_mode == "log":
            positive = sub[sub[col] > 0]
            if not positive.empty:
                metric_cols.append((col, lbl))
                data_by_metric[col] = (positive, col)
        else:  # "raw"
            metric_cols.append((col, lbl))
            data_by_metric[col] = (sub, col)

    if not metric_cols:
        empty_title = {
            "z": "No metric has enough data for z-scores yet",
            "log": "No positive values to log-transform",
            "raw": "No data",
        }[value_mode]
        return go.Figure().update_layout(height=height, title=empty_title)

    n = len(metric_cols)
    n_rows = (n + n_cols - 1) // n_cols

    # Spacing scales with rows so subplots don't crowd each other regardless
    # of grid dimensions.
    v_spacing = max(0.05, min(0.10, 0.55 / max(n_rows, 1)))
    fig = make_subplots(
        rows=n_rows, cols=n_cols,
        subplot_titles=[lbl for _, lbl in metric_cols],
        shared_xaxes=True,
        vertical_spacing=v_spacing,
        horizontal_spacing=0.06,
    )

    bin_order = ["Early (<2 min)", "Mid (2–3 min)", "Late (3–4.5 min)", "Very late (≥4.5)", "Unknown"]
    seen_bins_global = [b for b in bin_order if b in df["_rt_bin"].unique()]
    legend_shown: set[str] = set()

    for idx, (col, lbl) in enumerate(metric_cols):
        row = idx // n_cols + 1
        col_idx = idx % n_cols + 1

        sub, value_col = data_by_metric[col]
        if value_mode == "z":
            sub = sub.dropna(subset=["_z"])
        if sub.empty:
            continue

        # Threshold lines (z-mode only — meaningless in raw / log)
        if value_mode == "z":
            for y, dash, color in [
                (0, "solid", "#1f77b4"),
                (2, "dash", "#f0ad4e"), (-2, "dash", "#f0ad4e"),
                (3, "dash", "#d9534f"), (-3, "dash", "#d9534f"),
            ]:
                fig.add_hline(y=y, line={"color": color, "width": 1, "dash": dash},
                              opacity=0.35, row=row, col=col_idx)

        for rt_bin in seen_bins_global:
            bin_df = sub[sub["_rt_bin"] == rt_bin]
            if bin_df.empty:
                continue
            agg = bin_df.groupby(time_col)[value_col].agg(
                ["median",
                 lambda s: s.quantile(0.25),
                 lambda s: s.quantile(0.75),
                 "count"]
            ).reset_index()
            agg.columns = [time_col, "median", "q25", "q75", "n"]
            agg = agg.sort_values(time_col)
            color = _RT_BIN_COLORS.get(rt_bin, "#7f7f7f")
            show_legend = rt_bin not in legend_shown
            legend_shown.add(rt_bin)

            # IQR band (no legend)
            fig.add_trace(go.Scatter(
                x=pd.concat([agg[time_col], agg[time_col][::-1]]),
                y=pd.concat([agg["q75"], agg["q25"][::-1]]),
                fill="toself", fillcolor=color, opacity=0.10,
                line={"width": 0}, showlegend=False, hoverinfo="skip",
            ), row=row, col=col_idx)

            # Median line
            hover_value_fmt = "z: %{y:.2f}" if value_mode == "z" else "value: %{y}"
            fig.add_trace(go.Scatter(
                x=agg[time_col], y=agg["median"],
                mode="lines+markers",
                name=rt_bin,
                legendgroup=rt_bin,
                showlegend=show_legend,
                line={"color": color, "width": 1.5},
                marker={"size": 4, "color": color},
                hovertemplate=(
                    f"<b>{lbl} · {rt_bin}</b><br>"
                    f"Time: %{{x}}<br>{hover_value_fmt}<extra></extra>"
                ),
            ), row=row, col=col_idx)

        # Y-axis configuration per-mode.
        if value_mode == "z":
            ytitle = "z-score (σ from baseline)" if col_idx == 1 else None
            fig.update_yaxes(
                range=[-4.5, 4.5], row=row, col=col_idx,
                tickfont={"size": 10, "color": "#475569"},
                tickvals=[-3, -2, -1, 0, 1, 2, 3],
                gridcolor="#e2e8f0",
                zerolinecolor="#cbd5e1",
                title=ytitle,
                title_font={"size": 10, "color": "#64748b"},
            )
        else:
            ytitle = lbl if col_idx == 1 else None
            fig.update_yaxes(
                type="log" if value_mode == "log" else "linear",
                row=row, col=col_idx,
                tickfont={"size": 10, "color": "#475569"},
                gridcolor="#e2e8f0",
                title=ytitle,
                title_font={"size": 10, "color": "#64748b"},
            )
        fig.update_xaxes(
            tickfont={"size": 10, "color": "#475569"},
            gridcolor="#e2e8f0",
            row=row, col=col_idx,
        )

    # Subplot title font sized to match the larger panels
    for ann in fig.layout.annotations:
        ann.font = {"size": 13, "color": "#0f172a", "family": "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"}

    fig.update_layout(
        height=height,
        margin={"l": 10, "r": 10, "t": 50, "b": 20},
        legend={
            "orientation": "h",
            "yanchor": "bottom", "y": 1.02,
            "xanchor": "left", "x": 0,
            "font": {"size": 11, "color": "#334155",
                      "family": "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"},
            "bgcolor": "rgba(0,0,0,0)",
        },
        hovermode="closest",
        plot_bgcolor="white",
        paper_bgcolor="white",
        font={"family": "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"},
    )
    return fig


def build_control_type_response(
    df: pd.DataFrame,
    metric_col: str = "peak_area",
    height: int = 300,
) -> go.Figure | None:
    """Bar chart comparing median response across control types, peptide-by-peptide.

    The SSC0 control is treated as the instrument-optimal reference (since
    it's a clean 50ng-on-Evotip injection without sample-prep variability).
    QC B should track close to SSC0 at the same loading; QC A is at ~6x
    loading on column, so its bar should be visibly taller. A divergence
    of QC B from SSC0 points at digestion / Evotip handling issues.

    Returns ``None`` when there's no SSC0 data to anchor the comparison.
    """
    needed = {"control_type", "peptide_sequence", "raw_file_name", metric_col}
    if not needed.issubset(df.columns):
        return None
    df = df.dropna(subset=[metric_col, "peptide_sequence", "control_type"])
    if df.empty or "SSC0" not in df["control_type"].unique():
        return None
    # For each (control_type, peptide), take the median across runs; then
    # take the median across peptides per control_type. Two-step median
    # avoids one rogue peptide dominating the bar.
    per_pep = df.groupby(["control_type", "peptide_sequence"])[metric_col].median().reset_index()
    per_ct = per_pep.groupby("control_type")[metric_col].median().reset_index()
    # Sort with SSC0 first (the reference), then QC_A, QC_B, others.
    order_pref = ["SSC0", "QC_A", "QC_B", "BLANK"]
    per_ct["_order"] = per_ct["control_type"].map(
        lambda c: order_pref.index(c) if c in order_pref else 100
    )
    per_ct = per_ct.sort_values("_order")
    colors = {"SSC0": "#0f172a", "QC_A": "#1f77b4", "QC_B": "#f0ad4e", "BLANK": "#94a3b8"}

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=per_ct["control_type"], y=per_ct[metric_col],
        marker_color=[colors.get(c, "#7f7f7f") for c in per_ct["control_type"]],
        text=[f"{v:.2g}" for v in per_ct[metric_col]],
        textposition="outside",
        hovertemplate="<b>%{x}</b><br>Median peak area: %{y:.4g}<extra></extra>",
    ))
    fig.update_layout(
        height=height,
        margin={"l": 40, "r": 10, "t": 30, "b": 40},
        xaxis={"title": ""},
        yaxis={"title": "Median peak area (across peptides × runs)"},
        showlegend=False,
        bargap=0.4,
    )
    return fig


def build_scorecard(
    df: pd.DataFrame, time_col: str, metric_defs: list[tuple[str, str, bool]],
    baseline_mode: str, baseline_n: int, max_runs: int = 10,
) -> go.Figure:
    """Heatmap: rows = recent runs, columns = metrics. Cell color = % of peptides
    out-of-control (> ±2σ) for that metric on that run."""
    runs = (df.drop_duplicates("raw_file_name")
              .sort_values(time_col, ascending=True)
              .tail(max_runs)["raw_file_name"].tolist())
    if not runs:
        return go.Figure().update_layout(height=300, title="No runs to score")

    metric_cols_all = [(c, lbl) for c, lbl, _ in metric_defs if c in df.columns]
    if not metric_cols_all:
        return go.Figure().update_layout(height=300, title="No metrics available")

    # Compute z-scores ONCE per metric using the full df as the baseline window
    # and drop metrics where no peptide had ≥2 observations (so the column
    # would be all-NaN — same skip used by the LJ grid).
    z_by_metric: dict[str, pd.DataFrame] = {}
    metric_cols: list[tuple[str, str]] = []
    for col, lbl in metric_cols_all:
        scored = _zscore_per_peptide(df, col, baseline_mode, baseline_n, time_col)
        if scored["_z"].notna().any():
            z_by_metric[col] = scored
            metric_cols.append((col, lbl))

    if not metric_cols:
        return go.Figure().update_layout(
            height=300, title="No metric has enough data for z-scores yet"
        )

    matrix = []
    hover_texts = []
    for run_name in runs:
        row = []
        hover_row = []
        for col, _ in metric_cols:
            scored = z_by_metric[col]
            run_z = scored[scored["raw_file_name"] == run_name]["_z"].dropna()
            if run_z.empty:
                row.append(np.nan)
                hover_row.append("no data")
                continue
            n_violation = int((run_z.abs() > 2).sum())
            pct = 100 * n_violation / len(run_z)
            row.append(pct)
            hover_row.append(
                f"{n_violation}/{len(run_z)} peptides > ±2σ ({pct:.0f}%)"
            )
        matrix.append(row)
        hover_texts.append(hover_row)

    # Truncate run names for y-axis
    run_labels = [
        (r if len(r) <= 26 else r[:12] + "…" + r[-10:]) for r in runs
    ]

    fig = go.Figure(go.Heatmap(
        z=matrix,
        x=[lbl for _, lbl in metric_cols],
        y=run_labels,
        colorscale=[
            [0.00, "#dcfce7"],   # 0%      pale green
            [0.10, "#86efac"],   # 10%     green
            [0.10, "#fef08a"],   # warning yellow
            [0.25, "#fbbf24"],
            [0.25, "#fb923c"],   # orange
            [0.50, "#f97316"],
            [0.50, "#ef4444"],   # red
            [1.00, "#991b1b"],
        ],
        zmin=0, zmax=100,
        text=hover_texts,
        hovertemplate="<b>%{y}</b><br>%{x}<br>%{text}<extra></extra>",
        colorbar={
            "title": {"text": "% peptides ±2σ", "font": {"size": 10, "color": "#64748b"}},
            "thickness": 10, "len": 0.85, "tickfont": {"size": 9, "color": "#64748b"},
            "outlinewidth": 0,
        },
        xgap=2, ygap=2,
        showscale=True,
    ))
    fig.update_layout(
        height=max(280, 30 * len(runs) + 100),
        margin={"l": 10, "r": 10, "t": 30, "b": 80},
        xaxis={"side": "top", "tickangle": -30,
                   "tickfont": {"size": 10, "color": "#475569"},
                   "showgrid": False},
        yaxis={"autorange": "reversed",
                   "tickfont": {"size": 10, "color": "#475569"},
                   "showgrid": False},
        plot_bgcolor="white", paper_bgcolor="white",
        font={"family": "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"},
    )
    return fig


def compute_decision_summary(
    df: pd.DataFrame, time_col: str, metric_defs: list[tuple[str, str, bool]],
    baseline_mode: str, baseline_n: int,
) -> dict:
    """Plain-English status summary for the sidebar/decision card."""
    if df.empty:
        return {"status": "no-data", "headline": "No data", "details": []}

    latest = df.sort_values(time_col).iloc[-1]
    latest_name = str(latest.get("raw_file_name", "—"))
    latest_run = df[df["raw_file_name"] == latest_name]

    # Count violations on the latest run across all metrics
    metric_cols = [(c, lbl) for c, lbl, _ in metric_defs if c in df.columns]
    by_metric: list[tuple[str, int, int]] = []  # (label, violations, total)
    for col, lbl in metric_cols:
        scored = _zscore_per_peptide(latest_run, col, baseline_mode, baseline_n, time_col)
        z = scored["_z"].dropna()
        if z.empty:
            continue
        viol = int((z.abs() > 2).sum())
        by_metric.append((lbl, viol, len(z)))

    total_viol = sum(v for _, v, _ in by_metric)
    if total_viol == 0:
        status = "ok"
        headline = "✅ System nominal"
    elif any(v > t * 0.25 for _, v, t in by_metric):
        status = "fail"
        headline = "❌ Out-of-control: >25% peptides flagged in ≥1 metric"
    elif total_viol > 0:
        status = "warn"
        headline = "⚠ Watch: some peptides drifting ±2σ"
    else:
        status = "ok"
        headline = "✅ System nominal"

    details = []
    for lbl, viol, total in by_metric:
        if viol > 0:
            details.append(f"{lbl}: {viol}/{total} peptides ±2σ")

    return {
        "status": status,
        "headline": headline,
        "details": details,
        "latest_file": latest_name,
        "latest_targets": (latest.get("targets_found"), latest.get("targets_expected")),
        "latest_mass_error": latest.get("median_mass_error_ppm"),
    }


# ---------------------------------------------------------------------------
# Streamlit app
# ---------------------------------------------------------------------------
_STATUS_BANNER_STYLE = {
    "ok":      "background:#f0fdf4; color:#14532d; border-color:#86efac;",
    "warn":    "background:#fffbeb; color:#78350f; border-color:#fcd34d;",
    "fail":    "background:#fef2f2; color:#7f1d1d; border-color:#fca5a5;",
    "no-data": "background:#f8fafc; color:#334155; border-color:#cbd5e1;",
}


def main() -> None:
    st.set_page_config(page_title="QC Dashboard", layout="wide", initial_sidebar_state="collapsed")

    # Professional design system — system fonts, refined typography, subtle UI.
    st.markdown(
        """<style>
        :root {
            --bg:       #f8fafc;
            --surface:  #ffffff;
            --border:   #e2e8f0;
            --muted:    #64748b;
            --text:     #0f172a;
        }
        html, body, [class*="css"] {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            color: var(--text);
        }
        .block-container { padding-top: 3.25rem; padding-bottom: 1rem; max-width: 100%; }

        /* Page title */
        .qc-title {
            font-size: 1.35rem; font-weight: 600; letter-spacing: -0.01em;
            color: var(--text); margin: 0 0 0.5rem 0;
        }
        .qc-toolbar {
            display: flex; align-items: center; gap: 0.75rem;
            padding: 0.5rem 0; border-bottom: 1px solid var(--border);
            margin-bottom: 0.75rem;
        }

        /* Status banner */
        .qc-status {
            padding: 0.6rem 0.9rem; border-radius: 6px; margin: 0 0 0.75rem 0;
            font-size: 0.9rem; line-height: 1.4; font-weight: 500;
            border: 1px solid transparent;
        }

        /* KPI tiles — tighter, more refined */
        [data-testid="stMetric"] {
            background: var(--surface); border: 1px solid var(--border);
            border-radius: 6px; padding: 0.5rem 0.75rem;
        }
        [data-testid="stMetricLabel"] {
            font-size: 0.72rem; font-weight: 500; color: var(--muted);
            letter-spacing: 0.02em; text-transform: uppercase;
        }
        [data-testid="stMetricValue"] {
            font-size: 1.4rem; font-weight: 600; color: var(--text);
        }
        [data-testid="stMetricDelta"] { font-size: 0.72rem; color: var(--muted); }

        /* Streamlit native chrome */
        header[data-testid="stHeader"] {
            background: rgba(255,255,255,0.85);
            backdrop-filter: blur(6px);
            border-bottom: 1px solid var(--border);
            z-index: 999;
        }

        /* Section headings */
        .qc-section {
            font-size: 0.78rem; font-weight: 600; color: var(--muted);
            letter-spacing: 0.04em; text-transform: uppercase;
            margin: 1rem 0 0.4rem 0;
        }

        /* Filter row: compact selectboxes */
        [data-testid="stSelectbox"] label { font-size: 0.72rem; color: var(--muted); }
        </style>""",
        unsafe_allow_html=True,
    )

    # ─── Compact sidebar — only essentials ────────────────────────────────
    with st.sidebar:
        default_folder = _cli_args.folder or str(spool_dir())
        folder = st.text_input("Payload folder", value=default_folder)
        baseline_window_mode = st.selectbox(
            "Baseline window", ["All runs", "Last N runs", "Last N days"]
        )
        baseline_window_n = 20
        if baseline_window_mode == "Last N runs":
            baseline_window_n = st.number_input("N runs", min_value=3, max_value=500, value=20)
        elif baseline_window_mode == "Last N days":
            baseline_window_n = st.number_input("N days", min_value=1, max_value=365, value=30)
        time_axis = st.radio("X-axis", ["Acquisition", "Payload"], index=0, horizontal=True)
        time_col = "acquisition_time" if time_axis == "Acquisition" else "timestamp"
        value_mode_label = st.radio(
            "Y-axis",
            ["z-score", "raw", "log10"],
            index=0,
            horizontal=True,
            help=(
                "z-score: σ from per-peptide baseline (LJ control-chart style). "
                "raw: metric value as-is. log10: raw on a log axis — useful for "
                "intensity-style metrics (peak area / height)."
            ),
        )
        value_mode = {"z-score": "z", "raw": "raw", "log10": "log"}[value_mode_label]
        auto_refresh = st.toggle("Auto-refresh", value=False)
        refresh_secs = st.slider("Refresh (s)", 10, 300, 60) if auto_refresh else 60

    df = load_payloads(folder)
    if df.empty:
        st.warning(f"No `*_payload.json` files found in `{folder}`.")
        if auto_refresh:
            import time
            time.sleep(refresh_secs)
            st.rerun()
        return

    # ─── Title row + filter toolbar (separate rows, cleanly aligned) ─────
    st.markdown('<div class="qc-title">QC Metrics Dashboard</div>',
                unsafe_allow_html=True)

    # Peptide-class filter — only shown when at least one target has been
    # classified, so deployments that don't configure peptide_classes don't
    # see a useless dropdown.
    peptide_classes_present = (
        sorted(c for c in df.get("peptide_class", pd.Series(dtype=str)).dropna().unique() if c)
        if "peptide_class" in df.columns else []
    )
    n_filter_cols = 4 if peptide_classes_present else 3
    if n_filter_cols == 4:
        f1, f2, f3, f4, _f5 = st.columns([1.5, 1.5, 1.2, 1.5, 2.3])
    else:
        f1, f2, f3, _f5 = st.columns([1.5, 1.5, 1.2, 3.8])
        f4 = None
    instruments = sorted(df["instrument_id"].dropna().unique())
    selected_instrument = f1.selectbox(
        "Instrument", ["All", *list(instruments)],
        label_visibility="visible",
    )
    # Always show the canonical QC control types so the user can switch even
    # when the most recent batch happens to be all one type — otherwise the
    # dropdown collapses to whatever the recent data contains and operators
    # can't filter to historical SSC0 / QC_A runs. Add any custom types
    # actually seen in the data (e.g. from user classifier rules).
    from mdqc.types import ControlType as _ControlType
    canonical = [c.value for c in _ControlType if c.value != "SAMPLE"]
    seen_types = set(df["control_type"].dropna().unique()) - set(canonical)
    control_types = canonical + sorted(seen_types)
    selected_control = f2.selectbox(
        "Control type", ["All", *control_types],
        label_visibility="visible",
    )
    # SPD ("samples per day") — Evosep chromatography speed. Orthogonal to
    # control_type; lets operators look at e.g. only 200 SPD QC_B runs
    # without conflating with 500 SPD ones.
    spd_values = sorted(
        v for v in df.get("spd", pd.Series(dtype="float64")).dropna().unique()
    )
    spd_options = ["All", *[f"{int(v)} SPD" for v in spd_values]]
    selected_spd = f3.selectbox(
        "SPD", spd_options, label_visibility="visible",
    )
    if f4 is not None:
        selected_class = f4.selectbox(
            "Peptide class",
            ["All", *peptide_classes_present],
            label_visibility="visible",
            help="Filter to a configured peptide class (Skyline Protein column).",
        )
    else:
        selected_class = "All"
    # Keep a copy before filters so the QC-vs-SSC0 panel further down can
    # always show the reference comparison regardless of what the operator
    # has filtered the main view to.
    df_unfiltered = df.copy()
    if selected_instrument != "All":
        df = df[df["instrument_id"] == selected_instrument]
    if selected_control != "All":
        df = df[df["control_type"] == selected_control]
    if selected_spd != "All":
        spd_int = int(selected_spd.split(" ")[0])
        df = df[df["spd"] == spd_int]
    if selected_class != "All":
        df = df[df["peptide_class"] == selected_class]

    # Exclusions: lets the operator mark first-of-series outliers (column not
    # equilibrated, etc.) as excluded from baseline + chart computations
    # without deleting the underlying payload. Persisted to disk as
    # <folder>/exclusions.json so it survives restarts.
    excluded = load_exclusions(folder)
    with st.sidebar:
        all_runs_in_view = sorted(df["raw_file_name"].dropna().unique())
        with st.expander(
            f"Excluded runs ({len(excluded & set(all_runs_in_view))})",
            expanded=False,
        ):
            st.caption(
                "Excluded runs are hidden from charts and z-score baselines but kept in the spool."
            )
            new_excluded_in_view = set(st.multiselect(
                "Exclude these runs",
                options=all_runs_in_view,
                default=sorted(excluded & set(all_runs_in_view)),
                label_visibility="collapsed",
            ))
            # Preserve exclusions for runs outside the current filter view.
            preserved = excluded - set(all_runs_in_view)
            updated = preserved | new_excluded_in_view
            if updated != excluded:
                save_exclusions(folder, updated)
                excluded = updated
    if excluded:
        df = df[~df["raw_file_name"].isin(excluded)]
    if df.empty:
        st.info("No data matches filters.")
        return

    metric_defs = discover_metrics(df)
    summary = compute_decision_summary(
        df, time_col, metric_defs, baseline_window_mode, baseline_window_n
    )

    # ─── Status banner ───────────────────────────────────────────────────
    style = _STATUS_BANNER_STYLE.get(summary["status"], _STATUS_BANNER_STYLE["no-data"])
    detail_line = (
        f" &nbsp;·&nbsp; {'  ·  '.join(summary['details'][:3])}"
        if summary["details"] else ""
    )
    st.markdown(
        f"<div class='qc-status' style='{style}'>"
        f"<b>{summary['headline']}</b>{detail_line}"
        f"</div>",
        unsafe_allow_html=True,
    )

    # ─── KPI tiles ────────────────────────────────────────────────────────
    latest = df.sort_values(time_col).iloc[-1]
    latest_name = str(latest.get("raw_file_name", "—"))
    display_name = latest_name if len(latest_name) <= 22 else latest_name[:11] + "…" + latest_name[-10:]
    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Latest", display_name, help=latest_name)

    # Peptide-level rollup: each peptide is "detected" if any of its precursor
    # rows shows peak_area > 0 in the latest run. This is the science-meaningful
    # KPI (how many peptides found vs expected); the precursor/transition-level
    # count below is just diagnostic noise unless you're chasing missing
    # transitions.
    latest_rows = df[df["raw_file_name"] == latest["raw_file_name"]]
    if "peptide_sequence" in latest_rows.columns and not latest_rows.empty:
        pep_groups = latest_rows.groupby("peptide_sequence")["detected"].max()
        pep_found = int(pep_groups.sum())
        pep_total = len(pep_groups)
    else:
        pep_found, pep_total = 0, 0
    if pep_total > 0:
        k2.metric(
            "Peptides", f"{pep_found}/{pep_total}",
            delta=f"{100 * pep_found / pep_total:.0f}%", delta_color="off",
            help="Distinct peptides with at least one detected precursor in the latest run.",
        )
    else:
        k2.metric("Peptides", "—")

    # Precursor/transition row count from the original run_metrics — useful
    # to see at a glance how many rows came back from Skyline but not the
    # right thing to read as "QC recovery".
    tf, te = latest.get("targets_found"), latest.get("targets_expected")
    if pd.notna(tf) and pd.notna(te) and te:
        k3.metric(
            "Transitions", f"{int(tf)}/{int(te)}",
            delta=f"{100 * tf / te:.0f}%", delta_color="off",
            help="Total precursor/transition rows in the report. For per-peptide recovery, see Peptides.",
        )
    else:
        k3.metric("Transitions", "—")
    me = latest.get("median_mass_error_ppm")
    k4.metric("Mass error", f"{me:.2f} ppm" if pd.notna(me) else "—")
    rs = latest.get("median_rt_shift")
    k5.metric("RT shift", f"{rs:.3f} min" if pd.notna(rs) else "—")
    n_runs = df["raw_file_name"].nunique()
    n_with = df[df["targets_found"].fillna(0) > 0]["raw_file_name"].nunique()
    k6.metric("Runs", f"{n_runs}", delta=f"{n_with} active", delta_color="off")

    # ─── Digest-efficiency KPI (only when miss-cleavage class is present) ─
    # Surfaces the 0miss/(0miss+1miss) ratio for the latest run as a separate
    # metric — useful for trypsin digestion sanity in QC A workflows.
    digest_rows = latest_rows[
        latest_rows.get("peptide_class_purpose", pd.Series(dtype=str)) == "digest_efficiency"
    ] if "peptide_class_purpose" in latest_rows.columns else pd.DataFrame()
    if not digest_rows.empty:
        # Heuristic: shorter peptide sequence is the 0-miss form.
        digest_sorted = digest_rows.sort_values(
            "peptide_sequence", key=lambda s: s.str.len()
        )
        if len(digest_sorted) >= 2:
            zero_area = digest_sorted.iloc[0].get("peak_area") or 0.0
            one_area = digest_sorted.iloc[1].get("peak_area") or 0.0
            total = (zero_area or 0) + (one_area or 0)
            if total > 0:
                ratio = zero_area / total
                de_col, _ = st.columns([1.2, 4.8])
                de_col.metric(
                    "Digest efficiency",
                    f"{ratio * 100:.1f}%",
                    help="0miss / (0miss + 1miss) peak area for the configured miss-cleavage pair in the latest run.",
                )

    # ─── View tabs: full scorecard vs Panorama-style compact view ─────────
    # The full view keeps the multi-panel grid for at-a-glance triage; the
    # compact view drops to a single big LJ panel with a metric selector,
    # closer to Panorama / our existing QC app — no scrolling required.
    tab_full, tab_compact = st.tabs(["Scorecard", "Compact (single metric)"])

    with tab_full:
        st.markdown(
            '<div class="qc-section">Scorecard — recent runs × metrics, % peptides ±2σ</div>',
            unsafe_allow_html=True,
        )
        scorecard = build_scorecard(
            df, time_col=time_col, metric_defs=metric_defs,
            baseline_mode=baseline_window_mode, baseline_n=baseline_window_n,
            max_runs=10,
        )
        st.plotly_chart(scorecard, use_container_width=True)

        st.markdown(
            '<div class="qc-section">Levey-Jennings — peptides grouped by retention-time bin</div>',
            unsafe_allow_html=True,
        )
        if not metric_defs:
            st.info("No plottable metrics.")
        else:
            n_metrics = len([c for c, _, _ in metric_defs if c in df.columns])
            n_cols = 2 if n_metrics > 1 else 1
            n_rows = (n_metrics + n_cols - 1) // n_cols
            grid_height = max(560, n_rows * 280 + 60)
            fig = build_all_metrics_grid(
                df, metric_defs=metric_defs, time_col=time_col,
                baseline_mode=baseline_window_mode, baseline_n=baseline_window_n,
                height=grid_height, n_cols=n_cols, value_mode=value_mode,
            )
            st.plotly_chart(fig, use_container_width=True)

        # Instrument response reference (uses unfiltered df so SSC0 ref stays
        # available even if the operator filtered the main view).
        response_fig = build_control_type_response(df_unfiltered)
        if response_fig is not None:
            st.markdown(
                '<div class="qc-section">Instrument response — median peak area by control type</div>',
                unsafe_allow_html=True,
            )
            st.caption(
                "SSC0 is the reference (clean 50 ng on Evotip). QC B should track SSC0 — "
                "divergence indicates digestion / Evotip variability. QC A is loaded ~6× and "
                "should appear visibly taller than SSC0."
            )
            st.plotly_chart(response_fig, use_container_width=True)

    with tab_compact:
        # Single-figure layout — one big LJ panel, metric chosen via dropdown.
        # Mirrors Panorama / our current QC app's UX; no scrolling, focused on
        # one signal at a time. Inherits the same value-mode toggle from the
        # sidebar (z / raw / log) and the same baseline / filter selections.
        if not metric_defs:
            st.info("No plottable metrics.")
        else:
            metric_choices = [
                (col, lbl) for col, lbl, _ in metric_defs if col in df.columns
            ]
            labels = [lbl for _, lbl in metric_choices]
            chosen_label = st.selectbox(
                "Metric", labels,
                key="compact_metric",
                help="Switches the chart below to focus on one QC metric at full size.",
            )
            chosen_col = next(c for c, lbl in metric_choices if lbl == chosen_label)
            big_fig = build_grouped_lj(
                df, metric_col=chosen_col, metric_label=chosen_label,
                time_col=time_col,
                baseline_mode=baseline_window_mode, baseline_n=baseline_window_n,
                height=620, value_mode=value_mode,
            )
            st.plotly_chart(big_fig, use_container_width=True)

    if auto_refresh:
        import time
        time.sleep(refresh_secs)
        st.rerun()


if __name__ == "__main__":
    main()
