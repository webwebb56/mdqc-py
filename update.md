# mdqc → MD platform handoff pack

**Audience:** Peppe + the MD platform team (and the Claude agents they delegate to).
**Author:** Andrew Webb.
**Date:** 2026-05-27.
**Status:** mdqc is at v0.4.0, running daily on an Evosep Astral instrument PC. Working well enough that conversations have shifted from "make the local agent work" to "wire it to the MD platform and turn it into a System Suitability product." This document is the bundle Peppe needs to start that platform work without me having to be in every conversation.

> The first thing Peppe asked for is the **mdqc payload schema** — the contract between the local agent and anything that consumes it downstream. That's §6 below, and it's stable. Everything else in this doc exists to give the surrounding context that schema lives inside.
>
> **Real example payloads are committed in [`docs/examples/`](docs/examples/)** — a real success-case payload (sanitized), a synthetic v0.4.0+ payload showing the peptide-class fields, and a real failure-case payload. See [`docs/examples/README.md`](docs/examples/README.md) for the index and a suggested server-side ingest validation rule set.

---

## Table of contents

1. [Project context](#1-project-context) — why mdqc exists, who we're collaborating with
2. [Current state of mdqc](#2-current-state-of-mdqc) — what's built, how it's deployed
3. [Stoyan's pilot feedback from 2026-05-26](#3-stoyans-pilot-feedback-from-2026-05-26) — captured from the call transcript
4. [Roadmap — next mdqc releases](#4-roadmap--next-mdqc-releases) — what we'll ship locally before/while the platform work spins up
5. [The deeper instrument-tracking vision](#5-the-deeper-instrument-tracking-vision) — installation baselines, transmission diagnostics, peptide subclasses
6. [Payload schema](#6-payload-schema-the-data-contract) — **the contract Peppe needs**
7. [Architecture — mdqc → API → MD app](#7-architecture--mdqc--api--md-app) — proposed cut between local agent and platform
8. [Repo layout](#8-repo-layout) — where to find what in `mdqc-py`
9. [Open questions](#9-open-questions) — things to settle with Stoyan, Dorte, Nikolai before locking the platform API

---

## 1. Project context

**Evosep** sells liquid-chromatography systems and is launching a **digestion kit + automation solution** (a liquid handler that takes a lysate and produces Evotips ready for MS). The kit is launching at ASMS 2026; actual customer sales are Q3. Pre-launch window is ~3 months.

The kit ships with two QC sample types:

- **QC A** — process control. 1 μg lysate digest, ~300 ng loaded on Evotip. Tests the digestion + Evotip handling pipeline.
- **QC B** — sustainability/handling control. 50 ng on Evotip, no digestion variability. Same Evotip handling as QC A but bypassing digestion.

And:

- **SSC₀** — system suitability standard. 50 ng on Evotip, factory-defined standard peptide mix. The "instrument-optimal" reference. Run at installation to establish the baseline for that physical instrument.

**The science behind the comparison Stoyan keeps describing:** if QC B starts dropping → it's an LC-MS issue (because QC A would also drop, since LC-MS performance affects every injection). If QC B stays consistent but QC A drops → it's a digestion / Evotip handling issue (since the LC-MS is fine).

**MD's role:** software. The system suitability dashboard customers see. Evosep is happy for MD to offer it free to their customers as a hook ("register an account, see your QC data, optimize together"). The pitch to customers is exactly what Stoyan articulated on the call: lab managers can run **six instruments instead of one** because they trust the system enough to monitor passively.

**Where mdqc fits in the bigger picture:**

```
┌──────────────────────────────────┐    ┌──────────────────────────┐
│  Instrument PC                   │    │  MD platform / app       │
│                                  │    │                          │
│  raw file → mdqc agent → spool/  │ →  │  ingest → DB → UI        │
│              (Skyline subproc)   │    │                          │
└──────────────────────────────────┘    └──────────────────────────┘
                                       ↑
                                       this arrow is what
                                       Peppe is building
```

The left side (mdqc agent on the instrument PC) is functional and stable. The right side (MD platform ingestion + multi-instrument dashboard) is what this hand-off enables.

---

## 2. Current state of mdqc

### What it does

A small Python service (~6k LoC) that sits on a mass spec PC and watches an acquisition folder. For every new raw file:

1. **Detects** stable files (60 s default size/mtime quiescence window) — vendor-aware so Bruker `.d` directories, Waters `.raw` directories, Thermo `.raw` files and Sciex `.wiff` pairs all work
2. **Classifies** the file by filename: SSC₀ / QC_A / QC_B / BLANK / SAMPLE, with regex extraction of well position, plate ID, instrument ID, and **SPD** (Evosep samples-per-day)
3. **Extracts** QC metrics by running real Skyline (`SkylineCmd.exe`) on a per-extraction temp copy of the template — strips any `<*_results>` blocks that Skyline saves into the .sky when the GUI is used for sanity checks (a footgun that bit Stoyan three emails into the pilot)
4. **Parses** Skyline's CSV report into a structured `TargetMetric` list, with a configurable column→canonical-metric mapping in `config.toml` so operators can change their `.skyr` without losing the canonical KPIs
5. **Annotates** each peptide with a class (`recovery` / `digest_efficiency` / `oxidation` / `alkylation` / `custom`) via configurable rules matched against the Skyline `Protein` column
6. **Spools** results as JSON payloads on local disk (`spool/pending/` → `spool/completed/`). Schema-versioned. Atomic writes. The full schema is §6
7. **Visualizes** everything in a Streamlit dashboard (local-only loopback) with control-type / SPD / peptide-class / instrument filters, a scorecard heatmap, a Levey-Jennings grid, a single-figure "Compact" view, and a QC A/B vs SSC₀ bar chart

### Deployment model today

- Single agent per instrument PC, foreground process started by the operator
- Two transports for distribution:
  - **`pip install` from the GitHub repo** (works if there's a Python on the PC)
  - **Bundled `.exe`** built by CI (PyInstaller, no Python needed on the target). Doesn't ship the Streamlit dashboard — that's the "operator runs from source" path
- Data stays on the instrument PC. There's a (cloud-upload-capable) `Uploader` component wired but not used in the pilot
- Local web UI on `127.0.0.1:<random port>?token=<32-char hex>` for operator control (pause / resume / settings / activity log)

### Release pace

Eight releases since first delivery (~3 weeks). Tag history:

| Tag | Why |
|---|---|
| v0.1.x | Initial drop; Skyline discovery + basic extract |
| v0.2.1 | Skyline 26.x exits with code 2 on success — was treating as failure |
| v0.2.3 | Removed 500-char cap on captured stdout (was hiding the real Skyline error) |
| v0.2.4-5 | Strip `<measured_results>` + all `<*_results>` blocks from the template at copy time |
| v0.2.6 | Sum split MS1 + Fragment area columns (DIA reports) |
| v0.3.0 | Dashboard QoL: control-type dropdown bug, SPD filter, run exclusion, y-axis toggle, configurable column mapping |
| v0.4.0 | Peptide-class system, digest-efficiency KPI, QC vs SSC₀ bar chart, Compact view tab |

### Where the agent is solid

- The local watch + classify + extract loop. Hasn't crashed for over a week on Stoyan's Astral
- The Skyline interop (the painful part — exit codes, embedded results, column mapping)
- The payload schema (six minor versions in but the shape has been stable since v0.2.0)

### Where the dashboard still needs work

These are §3 — Stoyan's feedback. Most are small Streamlit issues that MD platform's UI engineers will fix once when porting away from Streamlit, so I don't want to over-invest locally.

---

## 3. Stoyan's pilot feedback from 2026-05-26

Transcribed and de-duplicated from a 47-minute call. Verbatim quotes in `"…"`.

### 3.1 The "only shows the latest 10 files" bug (real bug, persistent)

> "I cannot see more than the last 10 files. So it's nice to So that's a little bit irritating."
>
> "It just keeps this latest imported stuff."

After ~10 minutes of running, the dashboard shows only files from the latest batch (whichever control type was imported last). The control-type dropdown narrows. This was nominally addressed in v0.3.0 by always offering the canonical SSC₀/QC_A/QC_B/BLANK in the dropdown, but the behaviour Stoyan is describing on this call sounds like the dataframe itself is being narrowed — possibly by the existing scorecard's `max_runs=10` default — and definitely warrants another look. **Action: dashboard now defaults to a time-range scroller across "all" data and shows the last N runs only when explicitly chosen.** (We agreed on the call this is closer to how Stoyan's internal Evosep tool works.)

### 3.2 Installation baseline + reference-from-installation

This is the big architectural ask. Three sub-points:

- **At installation, run 15–30 SSC₀ samples.** That set defines the per-instrument baseline. Median per-peptide peak area is the reference; everything subsequent is a delta from it.
- **QC A and QC B are tracked as ratios against that SSC₀ baseline**, not as absolute z-scores. Expected ratio: QC B ≈ 1.0 × SSC₀ (same loading, same handling). QC A ≈ 6 × SSC₀ (300 ng vs 50 ng on column).
- **Baselines should be resettable.** When the customer cleans the instrument or changes a column, they redo SSC₀ and start a new baseline. Multiple baselines should be retained in time so the customer can compare "before clean" vs "after clean".

> "the baseline is actually something that is set on that particular system because we don't know how that mass spec is performing. We just know upon installation this is its performance and then we can compare QC A and QCB to that performance."

> "at each major instrument cleaning service you probably want to reset a baseline."

> "you want to be able to visualize the baselines in time. but perhaps you can then pick which baseline you want to be the delta from."

**This is where the platform comes in.** mdqc's local payloads aren't the right place to manage "the active baseline for this instrument" — that's user-curated state that should live in the platform's database with explicit baseline-set / baseline-reset events.

> **Superseded 2026-07-24** — see `docs/PLAN_2026-07-24.md` §5.2. The call landed the other way: baselines are recorded and owned **locally, per instrument, in each mdqc install** (not platform-owned), then linked into every payload via `baseline_context`/`comparison_metrics`. A "Gold Standards" web UI page (agent-local, shipped v0.5.5) lets the engineer review recorded SSC0 runs and pick which are representative per (instrument, SPD); the resulting baseline is stored under `%PROGRAMDATA%\MassDynamics\QC\gold_standards\`. The platform still receives each baseline as its own uploaded record for provenance/re-referencing — see PLAN §5.3 — but does not own the active-baseline state.

### 3.3 The Evosep automation metadata file

The digestion liquid handler emits a CSV per batch with **column info, reagent lot numbers, every step of the prep**. Customer-grade traceability data. Stoyan wants this linked to the QC payloads so that when a QC drift is detected, the customer can see "what changed in the sample prep between Run 47 and Run 48."

> "what is the output of the automation solution this metadata file and how do you get it into yours? you don't have to pick out all the metrics but pick out the ones that are relevant."

> "we can do something with MDQC where it's watching the raw file folder it can watch some sample Q folder"

Stoyan hasn't seen the format yet — his colleagues are still building it. He'll share the schema when he gets it. **For now this is an architecture decision to capture, not code to write.**

### 3.4 MS1-only QC mode

> "I was thinking because our diagnostic peptides are such high intensity, we pick the highest intensity, most robust guys that are always there, MS1 should work fine."

> "you have to rely on retention time as being quite important to make sure we're picking up the right features."

Rationale: DIA isolation windows and number/width settings vary per lab. mdqc can't extract reliable MS2 fragment data across customers without per-lab library tuning. So **drop MS2 entirely** and use MS1 quantitation against the diagnostic peptides (which Evosep specifically picked for being the brightest, most robust peaks).

The risk I raised on the call: with Skyline as the backend and MS1-only, misclassification of features by retention time alone is real. Mitigation Stoyan agreed with: use **isotope dot product** (already emitted), **mass accuracy** (already emitted), and a per-SPD **expected retention-time window** as the gating signals.

### 3.5 Per-peptide, per-SPD expected retention-time windows

This is the visualization Stoyan showed me from Evosep's internal Python tool (he'll send screenshots; I've seen the live demo).

Each diagnostic peptide has:
- An expected retention time **per SPD setting** (200 SPD vs 500 SPD vs whatever)
- A **green window** around it indicating "in spec"
- Color shifts when the user changes LC or column (so drift due to consumable changes is visible)

The QC signal is: how close to the centre of the expected window are we, and are we trending toward the edge.

> "the score and the way we should flag and feed it back to the user is when you're getting really close to the edge or if you've fallen outside of the default window that might be a flag for you guys to actually say, 'Hang on a second.'"

> "you might need an engineer to come and see it because your attention time's out by X% or something."

Implementation note: we need a config block per **(column, method)** — Evosep dictates these — that lists `(peptide, expected_rt, window_width)` triples. mdqc already has the data (the `protein_name` column + peptide sequence + retention time); it's just a matter of joining to the spec.

### 3.6 Smaller dashboard polish

- **"I changed the .skyr and now nothing imports"** — Stoyan thought this was unresolved. On the call I confirmed it's fixed by v0.3.0 column-overrides and dynamic column discovery. He'll re-test
- **The Compact tab is the right primary view** for non-expert customers. The Scorecard/grid view is for power users
- **Stoyan only wants to expose ~10 columns** to customers, not the full Skyline default. Lock the default `.skyr` and document the columns; allow operators to extend via config but not contract
- **Two peptides visible in one of the Compact charts when there should be more.** Small bug in the grouping; minor

### 3.7 Customer-friendly framing

The dashboard's primary information delivery is the **traffic-light status banner**. Stoyan's quote:

> "the traffic light system is key to understanding is it good or bad and then from there you can look into the data further."

> "simplicity first and then it's like yes or no works or not and then why does it not work I agree with you we should work towards that why and see"

This is **the design principle for the platform UI.** Default page should be: traffic light + one or two big numbers. Detail panels are click-through, not always-on.

---

## 4. Roadmap — next mdqc releases

These are the locally-implementable items that don't require platform-side work. The deeper stuff (baselines, multi-instrument aggregation, metadata file ingestion) is platform-side and is described in §5 and §7.

### v0.4.1 — dashboard polish (1-2 days)

- Fix the "only last 10 files" issue: dashboard defaults to a time-range scroller, ditches the implicit `max_runs=10` slice that's been confusing Stoyan
- Default sort: most recent first, top-to-bottom
- Two-peptide-only bug in Compact view
- "Hide complex views" preference (Stoyan: "have the dashboard and these guys [the detailed peptide tables] maybe don't need on the first page")

### v0.5.0 — installation baseline (local-only stand-in) (2-3 days)

A **local** stand-in for the platform-side baseline-management feature. Lets Stoyan exercise the workflow before the platform is live.

- New CLI: `mdqc baseline set [--label "Install 2026-05-30"]` — snapshots the last N SSC₀ runs into a `baselines/<id>.json` file under the data dir
- New CLI: `mdqc baseline list` / `mdqc baseline activate <id>`
- Dashboard reads the active baseline and renders ratio panels for QC A and QC B vs that baseline
- Multiple baselines retained; dashboard lets the user pick which to compare against
- This is throw-away code once the platform owns baseline state, but it lets us prove the workflow end-to-end with Stoyan

### v0.6.0 — peptide expected-RT windows (3-4 days)

- New config section: `[[expected_rt_windows]]` with `(column, method, peptide_sequence, expected_rt, window_seconds)` tuples
- New `TargetMetric` field: `rt_in_window: bool`, `rt_distance_from_centre: float | None`
- Dashboard renders the green window around each peptide's RT timeline (this is the Evosep internal-tool visual Stoyan showed us)
- Out-of-window count surfaces as a new run-level metric and feeds the traffic-light banner

### v0.7.0 — Evosep metadata file ingestion (2-3 days, **gated on Stoyan sharing the schema**)

- Add a second watch path: `[[instruments]] sample_queue_path = "..."`
- New module `mdqc.metadata.evosep_csv` parses the file, indexes by sample name / position
- `RunClassification.metadata` populated with `column_lot`, `reagent_lots`, `prep_date`, etc.
- Surfaced in the payload schema; rendered as a tooltip on each run in the dashboard

### v0.8.0 — MS1-only QC mode (3-4 days, scientific-validation gated)

- New config: `[skyline] quantitation_mode = "ms1" | "fragment" | "auto"`
- Ships an MS1-only `.skyr` template alongside the existing fragment one (smaller, simpler)
- Strips MS2/fragment columns from the default report when in MS1 mode
- Documentation update on when to use which

### Possibly v0.9 / later — DIA-NN parallel backend (gated on scientific conversation)

Stoyan's tool uses DIA-NN for the run-level `precursors_identified` signal (§5.5). If we decide to mirror that on the agent rather than computing it server-side from a different pipeline, the work is:

- Detect DIA-NN install on the instrument PC (similar to Skyline detection)
- Shell out with a predicted-library + workflow-specific missed-cleavage setting
- Emit results into the same payload schema with `extraction.backend = "diann"` instead of `"skyline"`
- Schema-level: no changes; the existing payload supports backend as an enum

Whether to do this is a scientific question, not an engineering one — depends on whether MD agrees DIA-NN is the right precursor-ID engine, or whether we'd source the same signal a different way.

### Out of scope for local mdqc

- Multi-instrument dashboard (this is platform; see §7)
- Event log (platform; see §5.4a and §7.2). The agent already emits the timestamps that events join against — no agent-side changes required
- Sample-batch / reagent metadata storage (platform; the join sits server-side because the metadata file is published per-batch, not per-run)
- User accounts, instrument fleet management, retention policy (platform)
- The customer-facing "free tier" experience (platform)
- Sample-prep blame-attribution (QC B vs QC A drift comparison) — better surfaced once the platform aggregates across instruments and timepoints

---

## 5. The deeper instrument-tracking vision

Beyond what's in the roadmap above, the call surfaced a much richer **instrument-state monitoring** ambition that we should keep in view as we design the platform. Posting here so it doesn't get lost.

### 5.1 Diagnostic peptide subclasses (already in v0.4.0)

The framework: each diagnostic peptide is annotated with a `purpose`:

- `recovery` — counts toward the per-run target recovery KPI (the K562 "non-reactive targets")
- `digest_efficiency` — paired peptides where ratio = `0miss / (0miss + 1miss)` measures tryptic digestion completeness. **Excluded from recovery** (otherwise a missing 1-miss peptide artificially drags recovery down)
- `oxidation` — Met-containing reporters; relative MS1 intensity vs unmodified equivalent indicates oxidation rate
- `alkylation` — Cys-containing reporters; presence/absence of carbamidomethyl shifted form indicates alkylation efficiency
- `custom` — passthrough for ad-hoc classes the customer adds

The pattern scales. The platform should treat `peptide_class_purpose` as a first-class enum and let the UI group / aggregate / threshold differently per class.

### 5.2 Transmission diagnostics (not yet built — high-value future work)

From the call, near the end:

> "for a bunch of these standard peptides the signal response between the MS1 and the MS2 the transmission efficiency gives you information about how dirty the quad is and how dirty the collision cell entrance is."

> "on the Q exactive instruments the fusion the tribrids and all of these same sorts of things they all have their little quirk but they have their diagnostic tells in the data and we could extract these things and just visualize them as a heat map as well to actually say look your signal is degrading but it's actually the mass spec it's not Evotip"

The concrete signal: per diagnostic peptide, **MS1 intensity vs MS2 transmitted intensity**. Drift in that ratio reflects instrument optics state independently of sample prep. Visualised as a heatmap (peptides × time, coloured by the ratio), it tells the customer **why** a signal is dropping, not just **that** it's dropping.

This is the "explain why it's broken, not just that it's broken" feature that Stoyan and I both agreed is the differentiator vs other QC tools. **It's also the natural place where Evosep gets value from us beyond just rebranding the streamllit app.**

Effort to add: per-peptide MS1 + MS2 columns are already in Skyline's report — we just need to compute the ratio, store it in `extra_metrics`, and render it as a heatmap on the dashboard / platform. Per-instrument calibration (what's a "normal" ratio for a clean Astral) is the harder part and probably needs a small reference dataset from Evosep.

### 5.3 Installation-baseline + drift-from-installation

Covered above (§3.2 / §4 v0.5.0). The platform-side implementation needs:

- A `Baseline` entity per instrument with: `id`, `instrument_id`, `created_at`, `label`, `n_samples`, `per_peptide_medians: dict[str, float]`, `notes: str`
- Multiple active baselines per instrument allowed; user selects one as "the reference"
- UI to capture metadata: "this baseline was set after a column change," "after a quad clean," etc.

### 5.4a Event annotation system (already exists in Stoyan's tool)

This is the single biggest piece I underestimated. Stoyan's internal tool has a full **event log** wired into every chart. The screenshots show:

**Six event types, all CRUD-managed via in-app forms:**

- `Instrumentation Change`
- `Instrument Downtime`
- `Reagent Change`
- `Technician Change`
- `Calibration`
- `Column Change`

**Event record fields:**

```
event_id          (auto)
event_type        (enum, the six above)
event_date        (date)
clock             (HH:MM)
event_description (free text)
ms_machine        (FK to instrument list)
initials          (audit trail — who logged it)
```

**Rendered on the time series as:**

- Coloured vertical bars at the affected timepoint(s) — visible in screenshot 1 as the orange bars + the green hover tooltip "Type: Instrument Downtime / ID: 46 / Initials: ab"
- A ▼ marker for point-in-time events
- Faint red vertical highlights in the diagnostic-peptides panel at the same timepoints, so the event context propagates across charts

**Why this matters for the platform:** the event log is what turns "we saw a drift on 2026-02-09" into "we saw a drift on 2026-02-09 *because the column was changed that morning*." It's the layer that makes the QC dashboard actionable for a non-expert customer — the same drift means very different things depending on whether the customer just did maintenance, swapped a reagent lot, or had unexplained downtime.

**Implementation note:** this is **platform-side, not local-agent-side.** Events span instruments and operators, want to be edited from any browser, want to be linked to multiple runs not one — none of which fits the per-instrument-PC mdqc agent. The platform API needs:

```
POST   /v1/qc/events                 # create
GET    /v1/qc/events?instrument=...  # list
PATCH  /v1/qc/events/{id}            # update
DELETE /v1/qc/events/{id}            # soft-delete
```

The agent's contribution: the **timestamp of every payload** (`run.acquisition_time`) is the join key the event log relates against. No agent-side changes required to support this.

### 5.4 Cross-control-type ratio (the QC A : QC B : SSC₀ diagnostic)

The blame-attribution Stoyan keeps describing:

```
QC B drops + QC A drops   →  LC-MS issue
QC B stable + QC A drops  →  Sample prep / Evotip handling
QC B drops + QC A stable  →  Anomalous, investigate; rare
```

v0.4.0 has the bar chart showing absolute medians per control type — that's the visual ingredient. What's missing is the **temporal cross-correlation**: time-series of (QC A median / SSC₀ baseline) and (QC B median / SSC₀ baseline) plotted together, with a derived signal "blame likelihood" that flips between LC-MS / sample-prep / both.

This belongs on the platform (it's a cross-run analysis that needs aggregation), not on the local agent.

### 5.5 Feature inventory from Stoyan's internal tool

Stoyan shared screenshots of the Evosep internal QC tool ("Live Diann Data" + "Live Diagnostic Peptides" + Event forms) on 2026-05-26. This is the **proven concept** we're effectively rebuilding into the MD platform — Evosep already uses it daily, and Stoyan's vision is for mdqc + the MD platform to replace it (or at least be its supported successor for Evosep customers).

Captured here so Peppe can scope the platform target accurately:

**Top-level data scope**

- Aggregates over **many instruments and LC machines** in one view. The chart in screenshot 1 spans 2025-07-22 → 2026-05-21 (~10 months) across two LC machines (`s00572`, `s00038`) — that's the cross-instrument, cross-time depth the MD platform needs to support
- Identity is **multi-part**: `(ms_machine, lc_machine_id)` — they treat the LC system as a separately-tracked entity from the MS. We should follow suit in our data model: instrument identity is a tuple, not a string
- A two-level data hierarchy: **DIA-NN run-level identification count** (top panel) + **per-peptide RT / area / etc.** (bottom panel). The two are linked — clicking an event marker on the top chart highlights the same timepoint on the bottom chart

**Sidebar filters Stoyan considers essential** (left column in screenshot 2)

| Filter | Notes |
|---|---|
| Data Focus: MS Machine / Column / LC Machine | Switches what's being grouped on; same data, different lens |
| Date Range | Open-ended start + end pickers |
| MS Machine | Single-select (astral in shot) |
| SPD | Numeric (200 in shot) |
| Sample Type | "hela" — the reference matrix |
| Sample Batch | "commercial 1" — which kit batch was used |
| Reagent batch id | All reagent traceability |
| Load (ng) | 50.0 |
| Workflow | "maintenance" — see DIA-NN library note below |
| Automation Method | All |
| Evosep Type | "ena" |

This is the **experimental context dimensions** the platform needs to ingest, store, and surface as filters. Several of these come from Evosep's automation metadata file (§3.3 / §4 v0.7.0) — not the raw MS data. So the ingestion pipeline needs to join QC payloads to metadata records on `(instrument_id, acquisition_time, well_position)` or similar.

**DIA-NN as a parallel / alternative quantitation engine**

> "Live DIA-NN results are generated using a human predicted spectral library (1 missed cleavage for maintenance workflows, 2 for all others)."

The top panel uses **DIA-NN**, not Skyline. The metric is `precursors_identified` — a single number per run summarizing "how much could the search engine pick out of the raw file." DIA-NN is well-suited because it's automated, library-free for the spectral side (predicted library), and gives one robust signal per run.

Stoyan's framing suggests **DIA-NN is the right backend for the high-level "is it broken?" identification-count signal**, and **Skyline is the right backend for per-peptide diagnostic detail.** They complement each other.

**Implication for mdqc:** the current Skyline-only backend covers the bottom panel (per-peptide diagnostics) but not the top panel (DIA-NN precursor counts). To match Stoyan's tool we'd need to add a DIA-NN backend alongside the Skyline one — same template + raw → different metric → same payload schema with a `backend: "diann"` discriminator.

**Per-chart controls visible in the screenshots**

- **Y-axis toggles** (per panel, independent):
  - `Y-axis in %` — express values as a percentage of the median (or a baseline)
  - `Y-axis median normalization` — divide by median; the median trace plots as 1.0
  - `Y-axis log transformation` — log10 axis
- **Median annotation** — inline text at top-left of chart: "precursors_identified Median = 66152.50". A small but high-signal UI touch — gives the operator the anchor value without needing to look at the data
- **Per-peptide chart**:
  - `Remove Low Confidence Peptides (<0.95 Isotope Dot Product)` toggle — filter out rows where identification quality is poor before computing any rollup. Hardcoded threshold at 0.95 in their tool; we should make this configurable
  - `Only Show The Diagnostic Peptides` toggle — distinguishes the diagnostic peptide subclass (mdqc's `peptide_class_purpose: "recovery"` set) from everything else in the raw report
- **Skyline Options chip selector** — a radio-button row to swap the y-axis variable of the diagnostic peptides chart. The options visible: `isotope_dot_product`, `library_dot_product`, `total_area_ms1`, `total_area_fragment`, `rt` (selected), `fwhm`, `total_background_ms1`, `total_background_fragment`, `signal_to_noise_ms1`, `signal_to_noise_ms2`, `tailing`. **All of these are already in our payload's `target_metrics` schema** (some as canonical fields, some in `extra_metrics`) — the platform UI just needs to enumerate them as variables
- **Expected-RT bands on per-peptide traces** — the green semi-transparent bands behind each peptide's RT trace. This is the "expected RT window per peptide × SPD × column" concept from §3.5 made visible. Out-of-band events are immediately readable

**What's intentionally missing from Stoyan's tool that we add**

- **Peptide classes** — Stoyan's tool treats all diagnostic peptides as one cohort. mdqc v0.4.0 distinguishes recovery / digest-efficiency / oxidation / alkylation, which lets us surface digest efficiency as its own KPI instead of conflating it with chromatographic recovery. Stoyan was explicitly enthusiastic about this addition on the call
- **QC A / QC B / SSC₀ blame attribution** — Stoyan's tool doesn't break down "is the LC-MS drifting or is the sample prep drifting." Our QC-A-vs-QC-B-vs-SSC₀ ratio (§5.4) and the upcoming installation-baseline-anchored panels (§4 v0.5.0) are net-new

So the platform target is **Stoyan's tool +**: same depth, plus the structured peptide-class system, plus the cross-control-type diagnostics, plus the customer-facing simplification (traffic light first, complexity tucked behind tabs).

---

## 6. Payload schema — the data contract

**This is the headline output of the local agent.** Every successful extraction writes one JSON file to `spool/pending/<run_id>_payload.json` and (after retention rules) moves it to `spool/completed/`. The schema is version-tagged via the `schema_version` field.

Current `schema_version`: **"1.1"** (in `mdqc/config/defaults.py`).

### 6.1 Top-level shape

```jsonc
{
  "schema_version":   "1.0",
  "payload_id":       "<uuid v4>",
  "correlation_id":   "<agent_id>-<yyyymmddHHMMSS>-<8 hex>",
  "agent_id":         "evosep_pilot_001",
  "agent_version":    "0.4.0",
  "timestamp":        "2026-05-26T13:42:51.117482+00:00",

  "run":                  { /* see §6.2 */ },
  "extraction":           { /* see §6.3 */ },
  "run_metrics":          { /* see §6.4 */ },
  "target_metrics":       [ /* see §6.5 */ ],

  "baseline_context":     null,  // reserved
  "comparison_metrics":   null   // reserved
}
```

Top-level invariants:

- `payload_id` is per-payload; `correlation_id` is per-extraction (same value if the payload is uploaded multiple times)
- All timestamps are ISO-8601 with explicit timezone (always UTC for ones mdqc generates; `acquisition_time` is whatever the raw file's mtime is)
- Fields ending in `_path` carry filesystem paths (not URLs). Fields starting with `_` are Python debug aids and **not part of the cross-language contract** — the platform should ignore them
- Null is meaningful: it means "value was not available at extraction time", not "value is zero"

### 6.2 `run` object — file + classification metadata

```jsonc
{
  "run_id":                    "<uuid v4>",
  "raw_file_name":             "2026-05-26_astral_p087_200spd_k562_50ng_QCB_d1_exp137554id.raw",
  "raw_file_hash":             "<sha256 hex, first 50 MB only>",
  "acquisition_time":          "2026-05-26T13:38:00.000000+00:00",   // raw file mtime
  "instrument_id":             "Astral",
  "vendor":                    "thermo",                              // thermo|bruker|sciex|waters|agilent
  "control_type":              "QC_B",                                // SSC0|QC_A|QC_B|BLANK|SAMPLE
  "well_position":             "A1",                                  // null if not extracted
  "plate_id":                  "P087",                                // null if not extracted
  "spd":                       200,                                   // null if not extracted
  "dilution_pct":              75,                                    // null unless the filename marks one
  "classification_confidence": "HIGH",                                // HIGH|MEDIUM|LOW
  "classification_source":     "FILENAME",                            // FILENAME|METADATA|POSITION|DEFAULT
  "method_name":               null,                                  // reserved for Evosep metadata file
  "column_info":               null                                   // reserved for Evosep metadata file
}
```

**`dilution_pct` (added v0.5.6)** — integer 1-100, parsed from filename markers
like `QCB_75perc` / `50pct` / `25%`. `null` for a neat control, which is the
normal case for routine QC. This is the independent variable in a
threshold-calibration dilution series: Evosep's stress test runs QC B at
100/75/50% × 200/300/500 SPD × 8 replicates, and grouping the response curve
by this field is what turns that run into green/yellow/red thresholds (§5.4).
Before v0.5.6 all three dilutions were indistinguishable in the payload —
every one classified simply as `QC_B` — so the platform had to re-parse the
filename to tell them apart.

`method_name` and `column_info` are placeholders for the Evosep automation metadata (§3.3 / §4 v0.7.0). When that schema is finalised they get populated; existing payloads have nulls there.

### 6.3 `extraction` object — pipeline metadata

```jsonc
{
  "backend":            "skyline",
  "backend_version":    "26.1.0.057",                                 // parsed from SkylineCmd --version
  "template_name":      "k562_diagnostic_peptides.sky",
  "template_hash":      "<sha256 hex>",                               // for change-detection
  "extraction_time_ms": 27974,
  "status":             "SUCCESS",                                    // SUCCESS|FAILED|SKIPPED
  "error_message":      null                                          // populated when status != SUCCESS
}
```

When `status == "FAILED"`, `error_message` contains the **full** Skyline stdout (no truncation; see v0.2.3). When `status == "SKIPPED"`, the file was classified as `SAMPLE` (not a QC type) and the extractor short-circuited.

### 6.4 `run_metrics` object — aggregated KPIs

```jsonc
{
  "targets_found":          47,
  "targets_expected":       48,
  "target_recovery_pct":    97.92,
  "median_rt_shift":        0.012,            // minutes; null if no RT data
  "median_mass_error_ppm":  -0.21,            // ppm; null if no mass-error data
  "chromatography_score":   null,             // reserved
  "digest_efficiency_pct":  82.84             // cleaved/(cleaved+miss-cleaved) %, or null
}
```

`digest_efficiency_pct` (added v0.5.0) is the miss-cleavage-pair digestion
efficiency as a percentage 0–100 — `cleaved / (cleaved + miss-cleaved)` peak
area, where the cleaved (shorter) peptide is the numerator. `null` when no
`digest_efficiency`-class peptides are configured/present. Colour banding
(Stoyan's >80 green / 70–80 yellow / <60 red) is a **display** decision for the
platform; the agent emits only the number.

**Important:** `targets_found / targets_expected` is computed **after** peptide-class filtering. Peptides whose class has `exclude_from_recovery: true` (or whose purpose is `digest_efficiency`) are excluded from both numerator and denominator. This is why we surface them in the dashboard as `Peptides: 47/48` rather than `Transitions: 407/407` (the row count).

`chromatography_score` is currently unused; reserved for a future composite "chromatography health" signal derived from RT consistency + peak width + symmetry.

### 6.5 `target_metrics` array — per-peptide rows

One entry per (peptide, precursor, charge state) row from the Skyline CSV. **This is the bulk of the payload** — ~407 entries for the current K562 template, on the order of 50–100 for the trimmed MS1-only template Stoyan is planning.

```jsonc
{
  "target_id":              "<sha1 hex, 16 chars>",                   // peptide + mz hash
  "peptide_sequence":       "PVSSAASVYAGAGGSGSR",
  "precursor_mz":           892.4287,
  "precursor_charge":       null,                                     // currently null; surfaced via extra_metrics
  "retention_time":         2.22,                                     // minutes
  "rt_expected":            null,                                     // populated when expected RT spec ships (v0.6)
  "rt_delta":               null,                                     // retention_time - rt_expected (auto)
  "peak_area":              7625766.0,                                // sum of MS1 + Fragment when DIA-split
  "peak_height":            118283712.0,
  "peak_width_fwhm":        0.0546,                                   // minutes
  "peak_symmetry":          null,
  "mass_error_ppm":         -1.06,
  "isotope_dot_product":    0.9897,
  "library_dot_product":    0.9395,
  "detected":               true,                                     // peak_area > 0

  "protein_name":           "Non_reactive_Targets",                   // Skyline Protein column
  "peptide_class":          "Non-reactive targets",                   // resolved via config
  "peptide_class_purpose":  "recovery",                               // recovery|digest_efficiency|oxidation|alkylation|custom

  "extra_metrics": {
    "Precursor Charge":           2,
    "Total Area MS1":             332169248.0,
    "Total Area Fragment":        191861184.0,
    "Total Background MS1":       781558.2,
    "Total Background Fragment":  0.0
    // any other numeric column from the .skyr that isn't a canonical metric
  }
}
```

`extra_metrics` is a **passthrough dict** — anything numeric in the Skyline CSV that doesn't map to a canonical field lands here. The platform should preserve this verbatim, since operators add columns to their `.skyr` for instrument-specific signals that we don't (and shouldn't) know about up front.

### 6.6 Failed-payload shape

When `extraction.status == "FAILED"`, the payload **still gets written** to the spool. It carries `run`, `extraction` (with `error_message`), and empty `target_metrics: []`, `run_metrics: {}`. The platform should ingest these too — the dashboard surfaces failed runs distinctly from successful ones so operators can debug.

### 6.7 Schema evolution policy

- Adding optional fields is non-breaking; old payloads remain ingestable
- Removing or renaming fields requires a `schema_version` bump (string-compared, not semver-parsed)
- The platform should accept any `schema_version ≤ <its own>` and refuse newer ones with a clear "agent version too new for this server" error
- All current shipping payloads are `schema_version: "1.1"`

---

## 7. Architecture — mdqc → API → MD app

Proposed cut between the local agent and the platform.

### 7.1 What the agent already has

mdqc has an `Uploader` component (`src/mdqc/uploader.py`) that POSTs payloads to a configurable endpoint with `tenacity` retry. It's not wired in the pilot because there's no server to talk to — but the surface is already there. Configurable via `[cloud]` block in `config.toml`:

```toml
[cloud]
endpoint            = "https://api.massdynamics.com/qc/ingest"
api_token           = "<bearer>"
upload_concurrency  = 2
retry_max_attempts  = 8
```

If the endpoint is unreachable, payloads stay in `spool/pending/` and retry when connectivity returns.

### 7.2 Proposed platform-side API

Minimal viable surface to start. Everything else can be derived from these.

```
# QC payloads (from the local agent)
POST   /v1/qc/payloads               # body = one mdqc QcPayload JSON (§6)
GET    /v1/qc/payloads               # paginated list, filterable by instrument / date / control_type / SPD / sample_type
GET    /v1/qc/payloads/{id}          # single payload

# Installation baselines
POST   /v1/qc/baselines              # capture current SSC0 set as a baseline
GET    /v1/qc/baselines              # list (per instrument)
PATCH  /v1/qc/baselines/{id}         # rename, mark as active reference

# Event log (from Stoyan's tool, §5.4a)
POST   /v1/qc/events                 # create
GET    /v1/qc/events                 # list, filterable by instrument / date range / event_type
PATCH  /v1/qc/events/{id}            # update
DELETE /v1/qc/events/{id}            # soft-delete

# Experimental metadata (from Evosep automation CSV, §3.3 + §5.5)
POST   /v1/qc/sample-batches         # ingest a batch metadata record
GET    /v1/qc/sample-batches         # list

# Instrument registry
GET    /v1/qc/instruments            # registered (ms_machine, lc_machine_id) pairs + last-seen
```

Auth: bearer token per instrument (rotateable) for `POST /payloads`. Token gets baked into `config.toml` at install time; agent uses it in the `Authorization` header. **Event-log and baseline endpoints want user-level auth**, not instrument-level — they're operator actions, not agent actions, and need an audit trail (the `initials` field on event records is already this).

### 7.3 Data flow

```
┌─────────────────────────────────────────────────────────────┐
│  Instrument PC                                              │
│                                                             │
│   raw file detected                                         │
│        ↓                                                    │
│   classify (control_type, SPD, well, plate)                 │
│        ↓                                                    │
│   strip template, run Skyline, parse CSV                    │
│        ↓                                                    │
│   QcPayload JSON                                            │
│        ↓                                                    │
│   spool/pending/<id>.json   ─────  Streamlit dashboard ──┐  │
│        ↓                          (local-only, optional)  │  │
│   Uploader (HTTPS POST)                                  │  │
│        ↓                                                  │  │
└────────┼──────────────────────────────────────────────────┘  │
         ↓                                                     │
┌─────────────────────────────────────────────────────────────┐│
│  MD platform                                                ││
│                                                             ││
│   /v1/qc/payloads ──→ ingest worker ──→ Postgres            ││
│                                ↓                            ││
│                       baseline calculator                   ││
│                                ↓                            ││
│                       per-instrument dashboard ─────────────┘│
│                                                              │
│   /v1/qc/baselines (set / list / activate)                  │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

### 7.4 What the platform owns that the agent doesn't

These are the deliberate "the platform side handles this, the agent doesn't try to" decisions:

- **Multi-instrument aggregation.** Each agent only knows about its one instrument. Cross-instrument views, fleet-wide drift comparisons, the customer's "all my instruments at a glance" page — all platform
- **Baseline state.** The platform is the system of record for "what is the active reference baseline for this instrument." Agent reads this back from the API (the dashboard renders relative-to-baseline; the relative computation happens on the platform, the agent just renders)
- **Event log** (§5.4a). Operator-logged events about column changes, calibrations, instrument downtime, reagent changes etc. Stored on the platform, joined to QC payloads by timestamp, rendered as overlays on time-series charts. This is what makes the dashboard actionable rather than merely descriptive
- **Sample-batch / reagent-lot metadata.** Ingested from the Evosep automation CSV (§3.3). Joined to QC payloads on `(instrument_id, acquisition_time)` or similar. Surfaces as sidebar filters and as tooltip metadata on individual runs
- **User accounts, auth, multi-tenancy.** Agent has no concept of users; everything is per-instrument bearer token. Platform handles all human identity (including the `initials` audit trail on event records)
- **Long-term retention policy.** Agent currently keeps the last N completed payloads on disk (default 300, configurable). Platform retains indefinitely (or per customer retention contract)
- **Cross-temporal analysis.** The QC-A-vs-QC-B-vs-SSC₀ "blame attribution" signal (§5.4) is best computed across many runs from the platform side, not on the agent

### 7.5 What stays on the agent

- All Skyline interop. Skyline runs **locally** on the instrument PC — it needs file access to the raw files, doesn't make sense to ship multi-GB raw files to a server
- The watcher, classifier, extractor — the "convert raw + template into a payload" flow
- The local web UI for operator control (pause, resume, settings, last-N-runs activity log)
- The bundled `.exe` distribution channel (operators who can't or won't install Python)

The Streamlit dashboard is the **interesting cut**. It works today but it's not where we want customers to live long-term — that's the MD app. My recommendation: leave the Streamlit dashboard as the "operator runs from source for debugging" path and stop investing in it heavily. Put all dashboard-feature work on the platform side, and let the local dashboard atrophy gracefully to a basic "is the agent running, what was the last run, here's the activity log" status page.

---

## 8. Repo layout

```
mdqc-py/
├── pyproject.toml                  # current version, dependencies
├── README.md                       # release badge, install paths (pip + .exe)
├── update.md                       # ← you are here
├── .github/workflows/ci.yml        # tests + lint + PyInstaller build + GitHub release
├── installer/                      # NSSM service registration, Inno Setup recipe
├── assets/
│   ├── MD_QC_Report.skyr           # bundled Skyline report definition
│   └── icon.png
├── docs/
│   ├── EVOSEP_PROTOTYPE_SETUP.md   # operator-facing setup guide
│   ├── PLAN.md                     # architecture decisions
│   └── AGENT_NOTES.md              # contributor gotchas per module
├── src/mdqc/
│   ├── __init__.py                 # __version__ (dynamic from installed metadata)
│   ├── __main__.py                 # `python -m mdqc` entry
│   ├── types.py                    # ControlType, TargetMetric, RunMetrics, QcPayload  ← §6
│   ├── classifier.py               # filename → ControlType + well + plate + SPD
│   ├── peptide_classes.py          # protein_name → class + purpose; digest-efficiency
│   ├── activity_log.py             # rolling recent-runs log for the operator UI
│   ├── uploader.py                 # tenacity-driven cloud upload  ← §7.1
│   ├── crash.py                    # crash-reporter / issue URL
│   ├── update_checker.py           # daily GitHub releases poll
│   ├── config/
│   │   ├── schema.py               # pydantic Config / SkylineConfig / WatcherConfig / etc.
│   │   ├── defaults.py             # PAYLOAD_SCHEMA_VERSION lives here
│   │   ├── paths.py                # data dir resolution; MDQC_DATA_DIR override
│   │   └── load.py                 # config.toml reader
│   ├── watcher/                    # filesystem events, stability window, finalizer
│   │   ├── observer.py             # watchdog wrapper
│   │   ├── finalizer.py            # 60s quiescence window per vendor
│   │   ├── registry.py             # ProcessedRegistry (don't re-extract done files)
│   │   └── vendor.py               # Bruker .d / Waters .raw / Thermo / Sciex detection
│   ├── extractor/                  # Skyline subprocess + CSV parser
│   │   ├── __init__.py             # Extractor.extract() orchestration
│   │   ├── skyline.py              # SkylineCmd discovery + subprocess; template strip
│   │   └── report.py               # CSV alias map + column-override resolution
│   ├── spool/                      # durable on-disk queue (atomic state transitions)
│   │   ├── store.py                # Spool, enqueue, prune  ← payload write site
│   │   └── recovery.py             # uploading/ → pending/ on startup
│   ├── service/
│   │   ├── lifecycle.py            # asyncio main loop, signal handling, FastAPI mount
│   │   └── agent_id.py             # per-install agent-ID generation
│   ├── webui/                      # in-agent control web UI (FastAPI + HTMX)
│   │   ├── dashboard.py            # status page, KPIs, recent activity
│   │   ├── settings.py             # config editor
│   │   ├── failed.py               # failed-runs retry UI
│   │   ├── wizard.py               # first-run setup
│   │   └── templates/, static/     # Jinja2 templates + CSS/JS
│   ├── plots/
│   │   └── app.py                  # Streamlit dashboard (1400 LOC; the bit that goes away long-term)
│   ├── ipc/                        # runtime.json + loopback HTTP for cross-process control
│   └── cli/                        # typer subcommands
│       ├── run.py                  # `mdqc run --foreground`
│       ├── doctor.py               # diagnostics
│       ├── status.py
│       ├── config_cmd.py
│       ├── failed.py               # failed-files admin
│       ├── reprocess.py
│       └── classify.py             # dry-run classification of a filename
├── tests/
│   ├── unit/                       # 370+ unit tests
│   ├── integration/                # e2e smoke tests
│   └── fixtures/                   # sample CSVs, .raw stubs
└── scripts/
    └── build.py                    # PyInstaller driver for the .exe
```

### Key files for the platform team to know about

| File | Why it matters |
|---|---|
| [src/mdqc/types.py](src/mdqc/types.py) | Defines `QcPayload`, `TargetMetric`, `RunMetrics`, `RunClassification`. Source of truth for §6 |
| [src/mdqc/spool/store.py](src/mdqc/spool/store.py) | Where payloads are constructed and serialized. Lines 137–220 are the canonical "build the payload" code path |
| [src/mdqc/config/defaults.py](src/mdqc/config/defaults.py) | `PAYLOAD_SCHEMA_VERSION` constant |
| [src/mdqc/uploader.py](src/mdqc/uploader.py) | Existing client-side upload code with retry. Endpoint is already configurable; just needs an endpoint to exist |
| [assets/MD_QC_Report.skyr](assets/MD_QC_Report.skyr) | The bundled Skyline report definition — the columns we extract |

---

## 9. Open questions

Things that aren't decided yet and need a conversation. Listed by who needs to weigh in.

### For Stoyan / Dorte / Nikolai

1. **Metadata file schema.** What's in the Evosep automation CSV, and what subset do we ingest? (§3.3 + §5.5) — *Stoyan to send when his team finalises the format*
2. **Expected RT windows per peptide × SPD × column.** We need a structured spec to load. Format: a JSON/CSV table of `(column_part_no, method, peptide_sequence, expected_rt_seconds, window_width_seconds)` — *Stoyan said he can pull these from existing internal QC docs*
3. **QC A : SSC₀ expected ratio.** Stoyan said "roughly 6×" but wants to nail down the calibration empirically. Plan: collect 20+ runs of each at install and define the per-method expected ratio
4. **Reset semantics for installation baseline.** Does "reset" archive the old baseline (keep history) or replace? Recommendation: archive — multiple baselines per instrument with timestamps and labels. Stoyan agreed on the call
5. **DIA-NN backend** (§5.5). Stoyan's tool uses DIA-NN for the run-level `precursors_identified` metric. Do we add a DIA-NN backend to mdqc to match, or do we treat the DIA-NN-style "broad ID count" signal as platform-side only (computed from a different pipeline)? *Conversation needed with Jeppe + Stoyan*
6. **Event-log taxonomy.** Stoyan's tool has six event types (Instrumentation Change, Instrument Downtime, Reagent Change, Technician Change, Calibration, Column Change). Are these the right six for MD-platform customers, or do we adjust? Recommendation: ship with these six, allow customer-extensible
7. **Beta tester list.** We have shared customers that could potentially trial the end-to-end pipeline. *Dorte has a list*

### For Peppe / MD platform

1. **API auth model.** Per-instrument bearer token? mTLS? Both? Recommendation: bearer for v1 (simple to provision), mTLS as an option for customers with stricter posture. Note (§7.2) that event-log + baseline endpoints want **user-level auth**, not instrument-level — operator actions need an audit trail
2. **Payload versioning policy on the server side.** The agent emits `schema_version`; what's the server's tolerance? Recommendation: accept v1.0 and v1.x; explicit migration when bumping to v2.0
3. **Storage backend.** Postgres is the obvious default; the `extra_metrics` dict argues for JSONB on that column rather than fully relational. Event log + sample-batch metadata are first-class entities with FK relationships to payloads
4. **Where does baseline-set/activate live in the UI?** Customer-facing setting, or operator-facing? Stoyan's vision: customer-facing, with explicit "I cleaned the instrument" / "I changed the column" prompts that trigger a new baseline capture. These prompts **and** the event-log entry should fire together — a baseline reset is itself an event
5. **Instrument identity is a tuple, not a string** (§5.5). Stoyan's tool tracks `(ms_machine, lc_machine_id)` as separate fields. Our `RunClassification.instrument_id` is a single string today — the platform's instrument registry should normalise to the tuple form, and the agent should be extended in a later release to populate `lc_machine_id` separately. Won't affect ingest of current v1.0 payloads, but worth designing the schema for now
6. **Filter dimensions to ingest as first-class metadata** (§5.5). Stoyan's tool surfaces these as left-sidebar filters and the platform's database needs columns for them: `sample_type`, `sample_batch`, `reagent_batch_id`, `load_ng`, `workflow` ("maintenance" vs "production"), `automation_method`, `evosep_type`. Most come from the Evosep automation CSV (§3.3) rather than the QC payload — so the ingestion pipeline needs to do the join
7. **Multi-instrument fleet view.** What does "all instruments at a glance" look like? Worth a design conversation with Stoyan and Nikolai early. Stoyan's tool shows multi-LC-machine bars in a single chart (screenshot 1) coloured by `lc_machine_id` — that's one pattern
8. **What we offer free vs paid.** Stoyan suggested the basic system-suitability dashboard could be a free Evosep-customer perk. Worth a commercial discussion before we lock the surface area

### For Andrew

1. **Productionization push.** Stoyan was explicit: "I need the motivation. I need you guys kind of going, 'Yeah, Mass Dynamics, we would love this to have it in app.'" — *To raise with the MD team this week*
2. **ASMS conversations with Dorte + Nikolai.** Use the conference to lock the commercial framing and the customer-experience story. Stoyan can't be there but will be back online after
3. **The fragment-vs-MS1 conversation.** Stoyan strongly leaning MS1-only for QC. The scientific tradeoff is real (less specificity → higher misclassification risk). Worth a separate call with Jeppe / scientific team

---

## Appendix A — Example end-to-end payload

A real payload from Stoyan's pilot, abbreviated for readability:

```jsonc
{
  "schema_version": "1.0",
  "payload_id":     "8e8f7939-f821-4e0c-acda-bdebc1c551db",
  "correlation_id": "evosep_pilot_001-20260526133839-1ab2c3d4",
  "agent_id":       "evosep_pilot_001",
  "agent_version":  "0.4.0",
  "timestamp":      "2026-05-26T13:38:40.117482+00:00",

  "run": {
    "run_id": "28d9bf3c-3c35-4cae-ae97-44746f8ba7a2",
    "raw_file_name": "2026-05-26_astral_p087_200spd_k562_50ng_QCB_d1_exp137554id.raw",
    "raw_file_hash": "a3b1c9d2e8f4b7c6d5a8e3f1b9c4d7e2f5a8b3c6d9e4f1a7b2c5d8e3f6a9b2c5",
    "acquisition_time": "2026-05-26T13:38:00+00:00",
    "instrument_id":  "Astral",
    "vendor":         "thermo",
    "control_type":   "QC_B",
    "well_position":  null,
    "plate_id":       "P087",
    "spd":            200,
    "dilution_pct":   null,
    "classification_confidence": "HIGH",
    "classification_source":     "FILENAME",
    "method_name":    null,
    "column_info":    null
  },

  "extraction": {
    "backend":            "skyline",
    "backend_version":    "26.1.0.057",
    "template_name":      "202060421_P087_M4_WP8_Exp4_8_3c_K562_DiagnosticPeps.sky",
    "template_hash":      "6f4b8c1e2a9d3b7e5c8a1b4d9e2f5a8c3d6e9f2a5b8c1d4e7f0a3b6c9d2e5f8a1",
    "extraction_time_ms": 27974,
    "status":             "SUCCESS",
    "error_message":      null
  },

  "run_metrics": {
    "targets_found":         47,
    "targets_expected":      48,
    "target_recovery_pct":   97.92,
    "median_rt_shift":       0.012,
    "median_mass_error_ppm": -0.21,
    "chromatography_score":  null
  },

  "target_metrics": [
    {
      "target_id":           "a3b1c9d2e8f4b7c6",
      "peptide_sequence":    "PVSSAASVYAGAGGSGSR",
      "precursor_mz":        892.4287,
      "precursor_charge":    null,
      "retention_time":      2.22,
      "rt_expected":         null,
      "rt_delta":            null,
      "peak_area":           7625766.0,
      "peak_height":         118283712.0,
      "peak_width_fwhm":     0.0546,
      "peak_symmetry":       null,
      "mass_error_ppm":      -1.06,
      "isotope_dot_product": 0.9897,
      "library_dot_product": 0.9395,
      "detected":            true,
      "protein_name":        "Non_reactive_Targets",
      "peptide_class":       "Non-reactive targets",
      "peptide_class_purpose": "recovery",
      "extra_metrics": {
        "Precursor Charge":          2,
        "Total Area MS1":            332169248.0,
        "Total Area Fragment":       191861184.0,
        "Total Background MS1":      781558.2,
        "Total Background Fragment": 0.0
      }
    }
    // ... 406 more entries ...
  ]
}
```

---

## Appendix B — Glossary

| Term | Meaning |
|---|---|
| **mdqc** | The Python agent in this repo. "Mass Dynamics QC" |
| **MD platform** | The cloud product mdqc payloads will feed into |
| **SkylineCmd** | The command-line interface to Skyline. mdqc shells out to this |
| **.skyr** | Skyline's report-definition file (XML; defines which columns the CSV export contains) |
| **.sky** | Skyline document file (XML; the template defining target peptides + libraries + settings) |
| **.blib** | Skyline spectral library |
| **SSC₀** | System suitability standard zero — the instrument-optimal reference, 50 ng on Evotip |
| **QC A** | Process control — full digestion + Evotip handling pipeline. 1 μg digest, 300 ng on column |
| **QC B** | Sustainability/handling control — 50 ng on Evotip without digestion variability |
| **SPD** | Samples per day. Evosep chromatography speed setting (200, 300, 500, etc.) |
| **DIA** | Data-independent acquisition. Astral's primary mode |
| **Evotip** | Evosep's disposable trap column |
| **K562** | Cell line used as the source of Evosep's diagnostic peptide set |
| **Peptide class** | An mdqc concept (v0.4.0+). Maps `Protein` column → purpose (recovery / digest_efficiency / oxidation / alkylation / custom) |
| **Installation baseline** | A snapshot of per-peptide median signal from a set of SSC₀ runs taken when the instrument was installed (or cleaned). The reference everything subsequent is measured against |
