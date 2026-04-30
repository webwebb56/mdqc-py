"""Streamlit QC plots app — prototype.

Reads completed payload JSONs from the spool and renders trend charts
for the key run-level QC metrics.

Run with:
    streamlit run src/mdqc/plots/app.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

# Ensure the package is importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mdqc.config.paths import spool_completed  # noqa: E402

st.set_page_config(
    page_title="MD QC Plots",
    page_icon="🔬",
    layout="wide",
)

# ── Data loading ─────────────────────────────────────────────────────────────

@st.cache_data(ttl=30)
def load_payloads() -> pd.DataFrame:
    completed = spool_completed()
    rows = []
    for p in completed.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        run = data.get("run", {})
        rm = data.get("run_metrics", {})
        rows.append({
            "file": run.get("raw_file_name", p.stem),
            "instrument_id": run.get("instrument_id", "unknown"),
            "control_type": run.get("control_type", "SAMPLE"),
            "acquisition_time": _parse_dt(run.get("acquisition_time")),
            "target_recovery_pct": rm.get("target_recovery_pct"),
            "targets_found": rm.get("targets_found"),
            "targets_expected": rm.get("targets_expected"),
            "median_rt_shift": rm.get("median_rt_shift"),
            "median_mass_error_ppm": rm.get("median_mass_error_ppm"),
            "chromatography_score": rm.get("chromatography_score"),
            "_payload_file": str(p),
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["acquisition_time"] = pd.to_datetime(df["acquisition_time"], utc=True, errors="coerce")
    df.sort_values("acquisition_time", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


@st.cache_data(ttl=30)
def load_target_metrics(payload_file: str) -> pd.DataFrame:
    try:
        data = json.loads(Path(payload_file).read_text(encoding="utf-8"))
    except Exception:
        return pd.DataFrame()
    targets = data.get("target_metrics", [])
    if not targets:
        return pd.DataFrame()
    return pd.DataFrame(targets)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


# ── Sidebar ───────────────────────────────────────────────────────────────────

st.sidebar.title("MD QC Plots")
st.sidebar.caption("Prototype — reads from local spool")

if st.sidebar.button("🔄 Refresh data"):
    st.cache_data.clear()

df_all = load_payloads()

if df_all.empty:
    st.info("No completed QC runs found yet. The spool is empty.")
    st.caption(f"Looking in: `{spool_completed()}`")
    st.stop()

instruments = sorted(df_all["instrument_id"].dropna().unique())
selected_instruments = st.sidebar.multiselect(
    "Instrument", instruments, default=instruments
)

control_types = sorted(df_all["control_type"].dropna().unique())
selected_types = st.sidebar.multiselect(
    "Control type", control_types, default=control_types
)

df = df_all[
    df_all["instrument_id"].isin(selected_instruments)
    & df_all["control_type"].isin(selected_types)
].copy()

st.sidebar.markdown("---")
st.sidebar.metric("Runs shown", len(df))
st.sidebar.metric("Total runs", len(df_all))

# ── Main content ──────────────────────────────────────────────────────────────

st.title("QC Trend Dashboard")

if df.empty:
    st.warning("No runs match the current filters.")
    st.stop()

# ── Row 1: Recovery + RT shift ────────────────────────────────────────────────

col1, col2 = st.columns(2)

with col1:
    st.subheader("Target Recovery %")
    fig = px.line(
        df,
        x="acquisition_time",
        y="target_recovery_pct",
        color="instrument_id",
        markers=True,
        labels={"acquisition_time": "Acquisition time", "target_recovery_pct": "Recovery %"},
    )
    fig.add_hline(y=80, line_dash="dash", line_color="orange", annotation_text="80% threshold")
    fig.update_layout(yaxis_range=[0, 105], margin=dict(t=20))
    st.plotly_chart(fig, use_container_width=True)

with col2:
    st.subheader("Median RT Shift (min)")
    fig = px.line(
        df,
        x="acquisition_time",
        y="median_rt_shift",
        color="instrument_id",
        markers=True,
        labels={"acquisition_time": "Acquisition time", "median_rt_shift": "RT shift (min)"},
    )
    fig.add_hline(y=0, line_dash="dot", line_color="grey")
    fig.update_layout(margin=dict(t=20))
    st.plotly_chart(fig, use_container_width=True)

# ── Row 2: Mass error + Chromatography score ──────────────────────────────────

col3, col4 = st.columns(2)

with col3:
    st.subheader("Median Mass Error (ppm)")
    fig = px.line(
        df,
        x="acquisition_time",
        y="median_mass_error_ppm",
        color="instrument_id",
        markers=True,
        labels={"acquisition_time": "Acquisition time", "median_mass_error_ppm": "Mass error (ppm)"},
    )
    fig.add_hline(y=0, line_dash="dot", line_color="grey")
    fig.add_hrect(y0=-5, y1=5, fillcolor="green", opacity=0.05, annotation_text="±5 ppm")
    fig.update_layout(margin=dict(t=20))
    st.plotly_chart(fig, use_container_width=True)

with col4:
    st.subheader("Chromatography Score")
    fig = px.line(
        df,
        x="acquisition_time",
        y="chromatography_score",
        color="instrument_id",
        markers=True,
        labels={"acquisition_time": "Acquisition time", "chromatography_score": "Score (0–1)"},
    )
    fig.add_hline(y=0.7, line_dash="dash", line_color="orange", annotation_text="0.7 threshold")
    fig.update_layout(yaxis_range=[0, 1.05], margin=dict(t=20))
    st.plotly_chart(fig, use_container_width=True)

# ── Run table ─────────────────────────────────────────────────────────────────

with st.expander("Run summary table", expanded=False):
    display_cols = [
        "acquisition_time", "instrument_id", "control_type", "file",
        "target_recovery_pct", "targets_found", "targets_expected",
        "median_rt_shift", "median_mass_error_ppm", "chromatography_score",
    ]
    st.dataframe(
        df[[c for c in display_cols if c in df.columns]],
        use_container_width=True,
        hide_index=True,
    )

# ── Per-run peptide detail ────────────────────────────────────────────────────

st.markdown("---")
st.subheader("Per-run peptide detail")

run_options = df[["file", "_payload_file", "acquisition_time"]].copy()
run_options["label"] = run_options.apply(
    lambda r: f"{r['file']}  ({r['acquisition_time'].strftime('%Y-%m-%d %H:%M') if pd.notna(r['acquisition_time']) else 'unknown'})",
    axis=1,
)
selected_label = st.selectbox("Select a run", run_options["label"].tolist())
selected_row = run_options[run_options["label"] == selected_label].iloc[0]
df_targets = load_target_metrics(selected_row["_payload_file"])

if df_targets.empty:
    st.info("No per-peptide data in this payload.")
else:
    t_col1, t_col2 = st.columns(2)

    with t_col1:
        if "peak_area" in df_targets.columns and "peptide_sequence" in df_targets.columns:
            st.markdown("**Peak areas**")
            fig = px.bar(
                df_targets.dropna(subset=["peak_area"]).sort_values("peak_area", ascending=False),
                x="peptide_sequence",
                y="peak_area",
                labels={"peptide_sequence": "Peptide", "peak_area": "Peak area"},
            )
            fig.update_layout(xaxis_tickangle=-45, margin=dict(t=10))
            st.plotly_chart(fig, use_container_width=True)

    with t_col2:
        if "mass_error_ppm" in df_targets.columns and "peptide_sequence" in df_targets.columns:
            st.markdown("**Mass error (ppm)**")
            fig = px.scatter(
                df_targets.dropna(subset=["mass_error_ppm"]),
                x="peptide_sequence",
                y="mass_error_ppm",
                color="detected" if "detected" in df_targets.columns else None,
                labels={"peptide_sequence": "Peptide", "mass_error_ppm": "Mass error (ppm)"},
            )
            fig.add_hline(y=0, line_dash="dot", line_color="grey")
            fig.update_layout(xaxis_tickangle=-45, margin=dict(t=10))
            st.plotly_chart(fig, use_container_width=True)

    with st.expander("Peptide metrics table", expanded=False):
        st.dataframe(df_targets, use_container_width=True, hide_index=True)
