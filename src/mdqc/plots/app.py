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

from mdqc.config.paths import spool_dir  # noqa: E402

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

# Metadata columns that should not be treated as metrics
_META_COLS = {
    "timestamp", "acquisition_time", "instrument_id", "raw_file_name",
    "control_type", "method_name", "column_info", "target_id",
    "protein_name", "peptide_sequence", "precursor_mz", "precursor_charge",
    "detected", "targets_found", "targets_expected", "median_rt_shift",
    "median_mass_error_ppm",
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
        method_name = run.get("method_name")
        column_info = run.get("column_info")

        run_metrics = payload.get("run_metrics", {})

        for target in payload.get("target_metrics", []):
            rec = {
                "timestamp": timestamp,
                "acquisition_time": acquisition_time,
                "instrument_id": instrument_id,
                "raw_file_name": raw_file_name,
                "control_type": control_type,
                "method_name": method_name,
                "column_info": column_info,
                "target_id": target.get("target_id", ""),
                "protein_name": target.get("protein_name", ""),
                "peptide_sequence": target.get("peptide_sequence", ""),
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
        if (z_curr > 2 and z_prev < -2) or (z_curr < -2 and z_prev > 2):
            if abs(z_curr - z_prev) > 4:
                if labels[i] != "1-3s":
                    labels[i] = "R-4s"
                if labels[i - 1] != "1-3s":
                    labels[i - 1] = "R-4s"

        # 2-2s: 2 consecutive points > 2 SD on the same side
        if abs(z_curr) > 2 and abs(z_prev) > 2:
            if (z_curr > 0) == (z_prev > 0):
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

    target_ids = sorted(df["target_id"].unique())
    colors = _color_palette(len(target_ids))
    color_map = dict(zip(target_ids, colors))

    for row_idx, (col_name, _label, _is_log) in enumerate(metric_defs, start=1):
        if col_name not in df.columns:
            continue
        sub = df.dropna(subset=[col_name])
        if sub.empty:
            continue

        for tid in target_ids:
            tdf = sub[sub["target_id"] == tid]
            if tdf.empty:
                continue
            fig.add_trace(
                go.Scatter(
                    x=tdf["acquisition_time"],
                    y=tdf[col_name],
                    mode="lines+markers",
                    name=tid,
                    legendgroup=tid,
                    showlegend=(row_idx == 1),
                    marker=dict(size=4),
                    line=dict(color=color_map[tid], width=1.5),
                    hovertemplate=(
                        "<b>%{customdata[0]}</b><br>"
                        "%{customdata[1]}<br>"
                        "Value: %{y:.4g}<extra></extra>"
                    ),
                    customdata=list(zip(tdf["raw_file_name"], tdf["peptide_sequence"])),
                ),
                row=row_idx,
                col=1,
            )

        if col_name == "peak_area" and log_area:
            fig.update_yaxes(type="log", row=row_idx, col=1)

    fig.update_layout(
        height=280 * n_metrics,
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.01,
            xanchor="left",
            x=0,
            font=dict(size=10),
        ),
        margin=dict(l=60, r=20, t=80, b=40),
    )
    fig.update_xaxes(title_text="Acquisition Time", row=n_metrics, col=1)
    return fig


# ---------------------------------------------------------------------------
# Plotting — Levey-Jennings
# ---------------------------------------------------------------------------
def build_lj_figure(
    df: pd.DataFrame,
    metric_defs: list[tuple[str, str, bool]],
    baseline_mode: str = "All runs",
    baseline_n: int = 20,
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

    target_ids = sorted(df["target_id"].unique())
    colors = _color_palette(len(target_ids))
    color_map = dict(zip(target_ids, colors))

    legend_shown: set[str] = set()

    for row_idx, (col_name, _label, _is_log) in enumerate(metric_defs, start=1):
        if col_name not in df.columns:
            continue
        sub = df.dropna(subset=[col_name])
        if sub.empty:
            continue

        for tid in target_ids:
            tdf = sub[sub["target_id"] == tid].copy()
            if tdf.empty:
                continue

            vals = tdf[col_name]

            if baseline_mode == "Last N runs":
                baseline_vals = vals.iloc[-baseline_n:]
            elif baseline_mode == "Last N days":
                cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=baseline_n)
                baseline_mask = tdf["acquisition_time"] >= cutoff
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
                        x=pts["acquisition_time"],
                        y=pts["_z"],
                        mode="markers",
                        name=legend_name,
                        legendgroup=tid if status == "ok" else status,
                        showlegend=show_legend,
                        marker=dict(
                            size=style["size"],
                            symbol=style["symbol"],
                            color=style["color"] if status != "ok" else color_map[tid],
                            line=dict(width=1, color="#333"),
                        ),
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
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.01,
            xanchor="left",
            x=0,
            font=dict(size=10),
        ),
        margin=dict(l=60, r=20, t=80, b=40),
    )
    fig.update_xaxes(title_text="Acquisition Time", row=n_metrics, col=1)
    return fig


def _add_control_lines(fig: go.Figure, row: int) -> None:
    fig.add_hline(y=0, row=row, col=1,
                  line=dict(color="#1f77b4", width=1.5, dash="solid"), opacity=0.6)
    for mult in (-1, 1):
        fig.add_hline(y=mult, row=row, col=1,
                      line=dict(color="#2ca02c", width=1, dash="dash"), opacity=0.5)
    for mult in (-2, 2):
        fig.add_hline(y=mult, row=row, col=1,
                      line=dict(color="#f0ad4e", width=1.2, dash="dash"), opacity=0.6)
    for mult in (-3, 3):
        fig.add_hline(y=mult, row=row, col=1,
                      line=dict(color="#d9534f", width=1.5, dash="dash"), opacity=0.7)


def _color_palette(n: int) -> list[str]:
    palette = px.colors.qualitative.Dark24 + px.colors.qualitative.Light24
    return [palette[i % len(palette)] for i in range(n)]


# ---------------------------------------------------------------------------
# Streamlit app
# ---------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(page_title="QC Dashboard", layout="wide")
    st.title("QC Metrics Dashboard")

    with st.sidebar:
        st.header("Settings")
        default_folder = _cli_args.folder or str(spool_dir())
        folder = st.text_input("Payload folder", value=default_folder)

        chart_mode = st.radio("Chart mode", ["Raw Values", "Levey-Jennings"], index=0)

        log_area = False
        if chart_mode == "Raw Values":
            log_area = st.checkbox("Log scale for Peak Area", value=False)

        baseline_window_mode = "All runs"
        baseline_window_n = 20
        if chart_mode == "Levey-Jennings":
            baseline_window_mode = st.selectbox(
                "Baseline window", ["All runs", "Last N runs", "Last N days"]
            )
            if baseline_window_mode == "Last N runs":
                baseline_window_n = st.number_input(
                    "Number of runs", min_value=3, max_value=500, value=20
                )
            elif baseline_window_mode == "Last N days":
                baseline_window_n = st.number_input(
                    "Number of days", min_value=1, max_value=365, value=30
                )

        auto_refresh = st.toggle("Auto-refresh", value=False)
        refresh_secs = st.slider("Refresh interval (s)", min_value=10, max_value=300, value=60)
        if auto_refresh:
            st.info(f"Refreshing every {refresh_secs}s")

    manifest = load_manifest(folder)
    if manifest:
        with st.sidebar.expander("Template Info"):
            st.write(f"**Template:** {manifest.get('template_name', '—')}")
            st.write(f"**Instrument:** {manifest.get('instrument_id', '—')}")
            targets = manifest.get("targets", [])
            st.write(f"**Expected targets:** {len(targets)}")
            extras = manifest.get("extra_metrics", [])
            if extras:
                st.write(f"**Extra metrics:** {', '.join(extras)}")

    df = load_payloads(folder)

    if df.empty:
        st.warning(
            f"No `*_payload.json` files found in `{folder}`. "
            "Check that the path is correct and that the agent has processed at least one file."
        )
        if auto_refresh:
            import time
            time.sleep(refresh_secs)
            st.rerun()
        return

    col1, col2 = st.columns(2)
    with col1:
        instruments = sorted(df["instrument_id"].unique())
        selected_instrument = st.selectbox("Instrument", options=["All"] + instruments)
    with col2:
        control_types = sorted(df["control_type"].dropna().unique())
        selected_control = st.selectbox("Control Type", options=["All"] + list(control_types))

    if selected_instrument != "All":
        df = df[df["instrument_id"] == selected_instrument]
    if selected_control != "All":
        df = df[df["control_type"] == selected_control]

    if df.empty:
        st.info("No data matches the selected filters.")
        return

    latest = df.sort_values("acquisition_time").iloc[-1]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Latest File", latest.get("raw_file_name", "—"))
    targets_found = latest.get("targets_found")
    targets_expected = latest.get("targets_expected")
    if targets_found is not None and targets_expected is not None:
        c2.metric("Targets", f"{int(targets_found)}/{int(targets_expected)}")
    else:
        c2.metric("Targets", "—")
    median_rt = latest.get("median_rt_shift")
    c3.metric("Median RT Shift", f"{median_rt:.3f} min" if pd.notna(median_rt) else "—")
    median_me = latest.get("median_mass_error_ppm")
    c4.metric("Median Mass Error", f"{median_me:.2f} ppm" if pd.notna(median_me) else "—")

    metric_defs = discover_metrics(df)
    if not metric_defs:
        st.info("No plottable metrics found in the data.")
        return

    if chart_mode == "Raw Values":
        fig = build_figure(df, log_area=log_area, metric_defs=metric_defs)
    else:
        fig = build_lj_figure(
            df,
            metric_defs=metric_defs,
            baseline_mode=baseline_window_mode,
            baseline_n=baseline_window_n,
        )
    st.plotly_chart(fig, use_container_width=True)

    if chart_mode == "Levey-Jennings":
        with st.expander("Westgard Rules Legend"):
            st.markdown(
                "| Rule | Meaning | Action |\n"
                "|------|---------|--------|\n"
                "| **1-2s** | Single point > 2 SD from mean | Warning |\n"
                "| **1-3s** | Single point > 3 SD from mean | Reject |\n"
                "| **2-2s** | 2 consecutive points > 2 SD, same side | Reject |\n"
                "| **R-4s** | 2 consecutive points spanning > 4 SD | Reject |\n"
                "\n"
                "Green circles = OK · Yellow diamonds = Warning · Red triangles = Reject"
            )

    if auto_refresh:
        import time
        time.sleep(refresh_secs)
        st.rerun()


if __name__ == "__main__":
    main()
