# MD QC Agent — Evosep Prototype Setup

This guide walks an operator through installing and running the
Mass Dynamics QC Agent prototype on an instrument acquisition PC.
Estimated setup time: **20–30 minutes** end to end.

---

## 1. What you'll have when you're done

- A background **agent** that watches your Astral raw-file output folder,
  invokes Skyline on every new `.raw`, and writes a JSON payload of QC
  metrics per run.
- A web **dashboard** at `http://localhost:8501` showing:
  - Status banner (ok / watch / fail)
  - KPI tiles (latest run, target recovery, mass error, RT shift, run count)
  - Scorecard heatmap of recent runs × metrics
  - Levey-Jennings grid (one panel per metric, peptides grouped by RT)

The agent runs locally — payloads stay on disk in
`C:\ProgramData\MassDynamics\QC\spool\completed\`. No cloud upload is
required for the prototype.

---

## 2. Prerequisites

### Hardware / OS
- **Windows 10 or 11** (the agent and dashboard are Windows-tested; macOS works for the dashboard alone)
- ~**2 GB free disk** (spectral libraries ~210 MB, spool grows ~5 KB per payload)
- Local administrator rights for the first install only

### Software
- **Skyline (64-bit)** — daily or release build. The agent auto-detects it at:
  - `C:\Program Files\Skyline\SkylineCmd.exe`
  - `C:\Program Files (x86)\Skyline\SkylineCmd.exe`
  - via Windows registry (Apache / ProteoWizard / Skyline keys)
  - via `PATH`

  **If your Skyline is installed elsewhere** (per-user folder, custom install
  location), set the path explicitly in `config.toml`:
  ```toml
  [skyline]
  path = "C:\\Custom\\Path\\To\\SkylineCmd.exe"
  ```

  **ClickOnce installs are not supported** — these have paths containing
  `\apps\2.0\`. Use the regular installer from <https://skyline.ms>.

- **Python 3.11 or 3.12** — install from <https://www.python.org/downloads>
  with the "Add Python to PATH" option ticked
- **Git** (only if cloning from GitHub) — <https://git-scm.com/download/win>

### Verifying Skyline is detected (recommended before first run)
```powershell
python -c "from mdqc.extractor.skyline import find_skyline; p = find_skyline(); print(p or 'NOT FOUND - install Skyline or set [skyline].path in config.toml')"
```
Should print the resolved path. If it prints `NOT FOUND`, fix that before
starting the agent — extraction will fail on every file otherwise.

### Files Evosep should provide (or that ship in the prototype)
- A populated Skyline document directory containing:
  - `<template>.sky` — the QC method (5- or 48-peptide K562/HeLa)
  - `Zoom200SPD200ngAstral_*.blib`
  - `Zoom300SPD50ngAstral_*.blib`
  - `Zoom500SPD200ngAstral_*.blib`
- `MD_QC_Report.skyr` — the report definition (ships with the agent)

---

## 3. Install the agent

### Option A — from a release ZIP (recommended for non-technical users)
1. Download the prototype bundle (`mdqc-prototype-vX.Y.Z.zip`) from
   the link Mass Dynamics provided.
2. Unzip into `C:\mdqc-prototype\`.
3. Open **PowerShell** in that folder and run:
   ```powershell
   pip install --user .
   ```

### Option B — from the GitHub repo (for developers)
```powershell
git clone https://github.com/webwebb56/mdqc-py.git
cd mdqc-py
pip install --user -e .[plots]
```

Either way you should now be able to run:
```powershell
python -m mdqc --version
```
and see the version string.

---

## 4. One-time configuration

The agent stores its data under `C:\ProgramData\MassDynamics\QC\`.
Three things to set up there:

### 4a. The Skyline method folder
Create `C:\ProgramData\MassDynamics\QC\methods\` if it doesn't exist,
and copy the following files into it:

| File | Purpose |
|------|---------|
| `QC_Method.sky`                       | Skyline document with target peptides |
| `Zoom200SPD200ngAstral_6328.blib`     | Spectral library for Whisper 200 SPD |
| `Zoom300SPD50ngAstral_6329_6323.blib` | Spectral library for Whisper 300 SPD |
| `Zoom500SPD200ngAstral_6321.blib`     | Spectral library for Whisper 500 SPD |
| `MD_QC_Report.skyr`                   | Report column definition |

> **Important.** The `.blib` files MUST live in the same folder as
> `QC_Method.sky` — Skyline resolves library paths relative to the
> document file's directory.

### 4b. The agent config — `config.toml`
Create `C:\ProgramData\MassDynamics\QC\config.toml` with this content,
adjusting the lines marked **`<-- EDIT`**:

```toml
[agent]
agent_id = "evosep_pilot_001"
log_level = "info"

# [cloud]: push payloads to the MD platform. Leave the whole section out (or
# omit api_token) to run local-only — payloads stay on disk in spool/completed.
# With a token set, MDQC POSTs each payload to POST /api/evosep_qcs as
# {"filename": "<id>_payload.json", "blob": <payload>}; unreachable uploads
# stay in spool/pending and retry.
#
# Easiest path: leave this section out of config.toml entirely and set it
# from the Settings page instead (Web UI → Settings → Cloud) — pick
# Development or Production from the dropdown, paste your token, save,
# restart the agent. That's the only thing a fresh install needs to start
# pushing automatically.
#
# Or set it here directly:
# [cloud]
# endpoint  = "https://dev.massdynamics.com/api/evosep_qcs"    # dev (default) — live-verified
# # endpoint = "https://app.massdynamics.com/api/evosep_qcs"   # production — use once confirmed live
# api_token = "<your API token from Account Details>"          # <-- EDIT

[skyline]
timeout_seconds = 900
process_priority = "below_normal"
# report_skyr_path: which Skyline report (.skyr) defines the CSV columns.
#   "auto" (default) uses the bundled MD_QC_Report.skyr in the methods folder.
#   Point it at your own .skyr to change the exported metrics without renaming:
# report_skyr_path = "C:\\ProgramData\\MassDynamics\\QC\\methods\\MD_QC_Report_20260723.skyr"
# collapse_transitions_to_peptides: fold Skyline's per-transition rows down to
#   one row per peptide (default true). Leave on for the diagnostic-peptide QC.
# collapse_transitions_to_peptides = true

[watcher]
use_filesystem_events = true
scan_interval_seconds = 30
stability_window_seconds = 60      # waits 60s of no file-size change
stabilization_timeout_seconds = 600

[spool]
max_pending_mb = 1000
max_age_days = 30
completed_retention_count = 300    # keep last 300 payloads on disk

[[instruments]]
id = "Astral_001"                                            # <-- EDIT (your label)
vendor = "thermo"
watch_path = "D:\\Acquisition\\QC\\raw"                       # <-- EDIT (where Astral writes .raw files)
file_pattern = "*.raw"
template = "QC_Method.sky"
method_name = "Whisper200SPD"                                 # <-- EDIT (free text)
column_info = "EV3837"                                        # <-- EDIT (your column ID)

# Peptide classes — map the Skyline Protein column to a purpose. The
# miss-cleavage pair drives the digest-efficiency % in the payload and is
# excluded from target recovery (the 1-miss peptide is often below LOQ, so
# counting it would drag recovery down). Match the exact Protein names in
# your .skyr.
[[peptide_classes]]
protein_name = "Non_reactive_Targets"
label = "Non-reactive targets"
purpose = "recovery"

[[peptide_classes]]
protein_name = "Miss-clevage_pair"                           # <-- EDIT (your exact Protein name)
label = "Miss-cleavage pair"
purpose = "digest_efficiency"
exclude_from_recovery = true
```

> **File encoding matters.** Save as **plain UTF-8 (no BOM)**. Notepad's
> default "UTF-8" adds a BOM that breaks TOML parsing. Use VS Code,
> Notepad++, or PowerShell `[System.IO.File]::WriteAllText(...)` to be sure.

### 4c. Verify the config
```powershell
python -c "import tomllib; tomllib.loads(open(r'C:\ProgramData\MassDynamics\QC\config.toml', encoding='utf-8').read()); print('Config OK')"
```
Should print `Config OK`. If it errors, fix the file before continuing.

---

## 5. Run the agent

In a PowerShell window:
```powershell
python -m mdqc run --foreground
```
You should see a startup line ending with `service_started`. Leave this
window open — closing it stops the agent. (For an unattended install,
register it as a Windows Service; see `installer\README.md`.)

The agent will now process any new `.raw` file dropped into the
configured `watch_path`. Each file is matched against the stability
window (default 60 s of no size change) before Skyline runs.

---

## 6. Run the dashboard

In a **second** PowerShell window:
```powershell
streamlit run C:\path\to\mdqc-py\src\mdqc\plots\app.py
```
(Substitute the path where you installed the package — if you used
Option A above, it's `C:\mdqc-prototype\src\mdqc\plots\app.py`.)

A browser tab should open at `http://localhost:8501`.

If it doesn't open automatically, navigate there manually.

This Streamlit dashboard is a separate, read-only trend view. The agent's own
web UI (Dashboard / Settings / Gold standards / Diagnostics / Failed / Logs —
opened via the tray icon or the link printed at `service_started`) is where
you record a **gold standard baseline**: run your SSC0 set (15-20 injections
per SPD, at install or after a column change), open **Gold standards**, tick
the runs that look representative — the page shades any run/peptide that
deviates from the currently-ticked set so an equilibration-run outlier is
visible before you save — then **Save baseline**. That baseline is stored
locally per (instrument, SPD) under `gold_standards\` (see §11); it is not
yet used to compute the ratios embedded in QC A / QC B payloads (that wiring
is still open — see `docs/PLAN_2026-07-24.md` §3.9).

---

## 7. Verify the pipeline end-to-end

1. **Copy one `.raw` file** into your `watch_path`.
2. Wait **~90 seconds** (60 s stability window + ~30 s Skyline extraction
   for a 200 MB Astral DIA file).
3. Refresh the dashboard. You should see:
   - **Latest** tile updates with the file name
   - **Targets** tile shows e.g. `48/48`
   - **Mass error** tile shows e.g. `-3.30 ppm`
   - The **scorecard** heatmap adds a row
   - The **LJ grid** populates once you have ≥2 runs (z-scores need a baseline)

If `Targets` reads `0/48`: the Skyline method targets aren't matching
the acquisition. See *Troubleshooting* below.

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Dashboard shows "No `*_payload.json` files found" and the log has repeated `processed_callback_failed: SkylineNotFound: SkylineCmd.exe not found` | Skyline isn't installed at any default location and `[skyline].path` isn't set in config | Install Skyline at default path, OR add `[skyline] path = "C:\\path\\to\\SkylineCmd.exe"` to `config.toml`. Verify with the `find_skyline` snippet in §2 |
| `0/48 targets found` on every run | Skyline template doesn't match acquisition method | Open `QC_Method.sky` in Skyline GUI, drag in one raw file, confirm peaks ARE found there. If not, the libraries are wrong-method (e.g., 200 SPD libraries vs 500 SPD acquisition) |
| Agent log says `MD_QC_Report does not exist` | `MD_QC_Report.skyr` missing from `methods/` | Copy it in. It ships with the agent under `src/mdqc/methods/`. |
| Skyline error: `file is being used by another process` | Concurrent Skyline runs writing to the same `.skyd` cache | Already handled in code via per-extraction temp dir + library hardlinking. If you still see this, kill all stuck `SkylineCmd.exe` processes via Task Manager and restart the agent. |
| Dashboard tiles never update | Streamlit cache TTL is 30 s — interact with any widget to force refresh | Or enable **Auto-refresh** in the sidebar |
| `instrument_id` shows as filename prefix instead of your config ID | The watch path differs from `instrument.watch_path` after `.resolve()` (mapped/UNC drive issue) | The agent has a single-instrument fallback; if you have one instrument configured, this works automatically. For multi-instrument setups use UNC paths consistently. |
| Empty Library Dot Product panel | Skyline didn't compute library scores for these peaks (insufficient fragments or no library match) | The dashboard now auto-hides empty metrics — refresh to reflow |

### Where to look for logs
- **Agent**: `C:\ProgramData\MassDynamics\QC\logs\mdqc.log` (JSONL format)
- **Failed files**: `C:\ProgramData\MassDynamics\QC\failed_files.json`
- **Activity history**: `C:\ProgramData\MassDynamics\QC\activity_log.json`

### Stopping the agent cleanly
- **Ctrl+C** in the PowerShell window where it's running, OR
- **HTTP shutdown** (from any other shell):
  ```powershell
  python -c "import sys, httpx; sys.path.insert(0,'src'); from mdqc.ipc.runtime import RuntimeFile; from mdqc.config.paths import runtime_file; info = RuntimeFile(path=runtime_file()).read(); httpx.post(f'http://127.0.0.1:{info.port}/api/shutdown', cookies={'mdqc_session': info.token}, timeout=3)"
  ```

---

## 9. What to send back to Mass Dynamics

To debug or expand the prototype, the most useful artefacts are:
1. **`config.toml`** (redact instrument IDs if needed)
2. **`logs/mdqc.log`** (last ~200 lines around the issue)
3. **One example payload** from `spool/completed/` (the JSON contains a
   per-target metric breakdown — that's what tells us what Skyline
   actually found)
4. The Skyline document folder used (`.sky` + `.blib` files) if the
   issue is method-related

A simple `zip -r evosep_qc_logs.zip C:\ProgramData\MassDynamics\QC\logs\` plus the
`spool\completed` folder is enough.

---

## 10. Known limitations of this prototype

- **Single instrument per config.** Multi-instrument is supported by the
  schema but the dashboard's filter UX hasn't been stress-tested at scale.
- **No cloud upload.** Payloads stay local. A `[cloud]` section in
  `config.toml` is wired for v1.1.
- **No per-template auto-detection yet.** All raw files in a watch path
  use the same Skyline template — the multi-template + filename routing
  feature discussed with Evosep is on the roadmap.
- **Method/file mismatch detection is reactive.** The dashboard shows a
  diagnostic banner when `targets_found = 0`, but the agent doesn't
  yet refuse to extract a file whose acquisition method doesn't fit
  the template.
- **Spool retention** is a fixed count (`completed_retention_count`).
  No batch tagging or QC-vs-experiment separation yet.

These are all v1.1 candidates. Feedback from the Evosep pilot directly
shapes that roadmap.

---

## 11. Quick reference

| Path | What's there |
|------|--------------|
| `C:\ProgramData\MassDynamics\QC\config.toml`    | Agent config |
| `C:\ProgramData\MassDynamics\QC\methods\`       | Skyline template + libraries + report |
| `C:\ProgramData\MassDynamics\QC\spool\completed\` | Per-run JSON payloads |
| `C:\ProgramData\MassDynamics\QC\gold_standards\` | SSC0 run index + saved gold-standard baselines (Web UI → Gold standards) |
| `C:\ProgramData\MassDynamics\QC\logs\mdqc.log`  | Agent log (JSONL) |
| `C:\ProgramData\MassDynamics\QC\runtime.json`   | IPC port + token (auto-managed) |
| `http://localhost:8501`                          | Streamlit dashboard |

Questions or issues: contact engineering@massdynamics.com or open
a GitHub issue at <https://github.com/webwebb56/mdqc-py/issues>.
