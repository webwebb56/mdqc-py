# MD QC Agent — installation and operation

This guide walks an operator through installing and running the
Mass Dynamics QC Agent on an instrument acquisition PC.
Estimated setup time: **20–30 minutes** end to end.

---

## 1. What you'll have when you're done

- A background **agent** that watches your raw-file output folder,
  invokes Skyline on every new run, and writes a JSON payload of QC
  metrics per run.
- The agent's own **web UI**, opened from the tray icon or the link printed
  at `service_started`:
  - Dashboard — service state, upload queue, configured instruments and
    the most recent extractions
  - Gold standards — record the baseline each run is scored against
  - Settings, Diagnostics, Failed files, Logs

The agent runs locally — payloads stay on disk in
`C:\ProgramData\MassDynamics\QC\spool\completed\`. Cloud upload is
optional: without a token the agent is a complete local QC monitor.

---

## 2. Prerequisites

### Hardware / OS
- **Windows 10 or 11** (the agent is Windows-tested)
- ~**2 GB free disk** (spectral libraries ~210 MB, spool grows ~5 KB per payload)
- No administrator rights needed with the setup program (§3, Option A), unless
  you install for all users

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

- **Python 3.11 or newer** — *only for the install script or a source
  install* (§3, Options B and C); the setup program carries its own. Install
  from <https://www.python.org/downloads> with "Add Python to PATH" ticked.
  `py -0` lists what a machine already has; any 3.11+ entry will do.
- **Git** (only if cloning from GitHub) — <https://git-scm.com/download/win>

### Verifying Skyline is detected (recommended before first run)
Once MDQC is installed, *Start menu > Mass Dynamics QC > MD QC Diagnostics*
shows whether Skyline was found. Or, after the install script:
```powershell
C:\MassDynamics\mdqc\Scripts\python -c "from mdqc.extractor.skyline import find_skyline; p = find_skyline(); print(p or 'NOT FOUND - install Skyline or set [skyline].path in config.toml')"
```
Should print the resolved path. If it prints `NOT FOUND`, fix that before
starting the agent — extraction will fail on every file otherwise.

### Files you provide (or that ship with the agent)
- A populated Skyline document directory containing:
  - `<template>.sky` — the QC method (5- or 48-peptide K562/HeLa)
  - `Zoom200SPD200ngAstral_*.blib`
  - `Zoom300SPD50ngAstral_*.blib`
  - `Zoom500SPD200ngAstral_*.blib`
- `MD_QC_Report.skyr` — the report definition (ships with the agent)

---

## 3. Install the agent

### Option A — the setup program (recommended; no Python or PowerShell)

Download **MDQC-Setup-`<version>`.exe** from the
[latest release](https://github.com/MassDynamics/md-qc/releases/latest), copy it to
the instrument PC (a USB stick is fine) and double-click it. It carries its own
Python, so nothing else needs installing apart from Skyline, and the PC needs no
internet.

- By default it installs for the current user, with no administrator rights.
  *Install for all users* (admin) is offered on the first page.
- It starts the agent with its tray icon and opens the dashboard. The agent
  then starts at every login.
- Running a newer setup program upgrades in place, keeping your settings.
- Remove it from *Settings > Apps > MD QC Agent*. Configuration and results
  in `C:\ProgramData\MassDynamics\QC` are kept.
- For IT rollouts: `MDQC-Setup-<version>.exe /VERYSILENT /CURRENTUSER` (or
  `/ALLUSERS`).

Until the setup program is code-signed, Windows may show "Windows protected
your PC": choose *More info > Run anyway*.

The commands in the rest of this guide use the install script's location,
`C:\MassDynamics\mdqc\Scripts\mdqc`. After the setup program, use
`mdqc.cmd` in its folder instead — for a per-user install,
`%LOCALAPPDATA%\Programs\Mass Dynamics\MDQC\mdqc.cmd` (e.g.
`...\mdqc.cmd doctor`).

### Option B — the install script (PowerShell, uses an installed Python)

With Python 3.11 or newer installed (§2), download the script for the release
and run it:

```powershell
Invoke-WebRequest -UseBasicParsing https://raw.githubusercontent.com/MassDynamics/md-qc/v1.2.0/scripts/install.ps1 -OutFile install.ps1
powershell -ExecutionPolicy Bypass -File install.ps1
```

Run it as the Windows user who uses the instrument PC. It:

- finds a suitable Python and creates or upgrades the environment at
  `C:\MassDynamics\mdqc`, with the release and the tray icon
- checks the installed version and runs `mdqc doctor`
- creates an empty `config.toml` if there is none, so the agent can start and
  be set up from the dashboard's Settings page (an existing one is never
  touched)
- makes the agent and its tray icon **start at login, with no window**, and
  adds a *Mass Dynamics QC* Start-menu folder: **MD QC Dashboard**, **MD QC
  Agent** (starts it again if it was stopped) and **MD QC Diagnostics**
- starts everything straight away

After that nobody needs PowerShell: the tray icon's menu opens the dashboard,
settings and diagnostics, and shows Windows notifications. Re-running the
script upgrades in place: it stops the running agent first (letting it finish
the file in hand), upgrades, and starts it again.

Options:

| Option | What it does |
|---|---|
| `-Service -NssmPath <nssm.exe>` | *Advanced, for IT teams.* Run the agent as a Windows service instead, so it starts with Windows before anyone logs in. Needs an elevated PowerShell and [NSSM](https://nssm.cc). The tray icon still starts at login. Starting at login (the default) is the tested, supported setup. |
| `-Version <x.y.z>` | Install another release. |
| `-Python <path\to\python.exe>` | Use this interpreter instead of the newest 3.11+ the `py` launcher finds. |
| `-NoStart` | Install and register, but start nothing now. |
| `-Uninstall` | Stop the agent and remove the environment, startup entry, shortcuts and service. Configuration and results in `C:\ProgramData\MassDynamics\QC` are kept. |

Only one agent runs per PC: a second one, from a shortcut or another
installation, exits straight away rather than processing every file twice.

The same install by hand (no startup entry or shortcuts; start it as in §5):

```powershell
py -3 -m venv C:\MassDynamics\mdqc
C:\MassDynamics\mdqc\Scripts\pip install "mdqc[tray] @ https://github.com/MassDynamics/md-qc/archive/refs/tags/v1.2.0.tar.gz"
```

Calling the environment's `pip` and `mdqc` by full path means you never have to
activate it, and so never have to change PowerShell's execution policy. If you
would rather activate it, run `C:\MassDynamics\mdqc\Scripts\Activate.ps1` first
and then use plain `pip` and `mdqc`.

The agent is not pinned to that location — any folder the service account can
read works. Keep it off a network drive.

### Option C — from source (for developers)
```powershell
git clone https://github.com/MassDynamics/md-qc.git
cd md-qc
py -3 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
```

Either way you should now be able to run:
```powershell
C:\MassDynamics\mdqc\Scripts\mdqc version
```
and see `mdqc 1.2.0`.

How the setup program in Option A is built, tested and signed is in
[`installer/README.md`](../installer/README.md).

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
agent_id = "qc_agent_001"
log_level = "info"

# [cloud]: push payloads to the MD platform. Leave the whole section out (or
# omit api_token) to run local-only — payloads stay on disk in spool/completed.
# With a token set, the agent POSTs each payload to the configured endpoint as
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
# endpoint  = "https://app.massdynamics.com/api/evosep_qcs"    # production (default)
# # endpoint = "https://dev.massdynamics.com/api/evosep_qcs"   # development
# api_token = "<your API token from Account Details>"          # <-- EDIT

[skyline]
timeout_seconds = 900
process_priority = "below_normal"
# report_skyr_path: which Skyline report (.skyr) defines the CSV columns.
#   "auto" (default) uses the bundled MD_QC_Report.skyr in the methods folder.
#   Point it at your own .skyr to change the exported metrics without renaming.
#   Give yours its own name: the agent restores its bundled copy over
#   methods\MD_QC_Report.skyr whenever the bundled one is newer, so edits
#   saved under that name are lost on upgrade.
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
# method_name and lc_column are reserved: the payload carries them, but the
# agent has no source for either yet, so both are always null.

# Peptide classes — map the Skyline Protein column to a purpose. With none
# configured, the agent uses the standard QC panel's: Non_reactive_Targets
# (recovery) and the miss-cleavage pair (digest efficiency, which needs this
# rule to be calculated at all) under both spellings found in templates,
# Miss-cleavage_pair and Miss-clevage_pair. Configure your own only for a different panel;
# protein_name must match the Protein column, so copy it from the .skyr export.
# [[peptide_classes]]
# protein_name = "My_Targets"                               # <-- EDIT
# label = "My targets"
# purpose = "recovery"

# Per-SPD thresholds — override any [qc_thresholds] value at one SPD. A 500 SPD
# gradient is under half the length of a 200 SPD one, so a retention-time
# tolerance that suits 200 SPD is too loose there. Anything not listed is
# inherited.
# [qc_thresholds_by_spd.500]
# rt_deviation_pct_max = 1.0

# Strict naming — only a QC type at the very start of the name (PCD96_…,
# SSC_…, SSCBASE_…) counts, and wells never imply one. Use it once automated
# sample prep names the files, so a customer sample can't be taken for a QC run.
# classification_mode = "strict"
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

### 4d. Name your QC files

The agent decides what a run is from its filename. Put one of these markers
in the name, separated from the rest by `_`, `-`, `.` or a space:

| Marker | What it is | Reported as |
|---|---|---|
| `PCD96`, `PCD384` | Process Control Digest, from a 96- or 384-well kit | `PCD`, with `kit_format` 96 or 384 |
| `SSC` | System Suitability Control | `SSC` |
| `SSC_GOLD` | A run you intend as a gold standard | `SSC0` |
| `BLANK` | Blank injection | `BLANK` |

PCD96 and PCD384 are the same control and are treated identically; the kit
size is recorded so the two can still be told apart. Any other file is
treated as an experimental sample. Earlier names — `QCA`, `QC_A`, `PC`,
`QCB`, `QC_B`, `SSC0` — are still recognised, so existing batches don't need
renaming. Check a name before relying on it:

```powershell
C:\MassDynamics\mdqc\Scripts\mdqc classify PCD96_2026-09-23_200SPD_S00572_r01.raw
```

It uses your `config.toml` rules as the agent does, and prints the type,
kit, SPD and LC serial it found.

When automated sample prep writes the file names, set
`classification_mode = "strict"` (§4b). Then only the first part of a name
decides its type — `SSCBASE` for
gold-standard runs — and customers must not start a sample name with a QC type.

## 5. Run the agent

Installed with the script (§3, Option A), the agent is already running and
starts by itself at every login. Open the dashboard from the tray icon or
*Start menu > Mass Dynamics QC > MD QC Dashboard*. If it was stopped,
*MD QC Agent* in the same folder starts it again, or from PowerShell:
```powershell
C:\MassDynamics\mdqc\Scripts\mdqc start
```

To watch it work instead — for troubleshooting — stop it and run it in a
PowerShell window:
```powershell
C:\MassDynamics\mdqc\Scripts\mdqc run --foreground
```
You should see a startup line ending with `service_started` and the web UI
address. Closing that window stops the agent.

The agent will now process any new `.raw` file dropped into the
configured `watch_path`. Each file is matched against the stability
window (default 60 s of no size change) before Skyline runs.

---

## 6. Record a gold standard baseline

Open the agent's web UI (tray icon, or the link printed at
`service_started`). A run is only judged against a baseline you have
recorded, so this step is what turns raw measurements into verdicts.

Run your SSC0 set — 15-20 injections per SPD, at install or after a column
change — then open **Gold standards**, tick the runs that look
representative, and **Save baseline**. The page shades any run or peptide
that deviates from the currently-ticked set, so an equilibration outlier is
visible before you save.

The baseline is stored locally per (instrument, SPD) under
`gold_standards\` (see §11). From then on, every PCD / SSC payload
carries `baseline_context` and `comparison_metrics`: per-peptide ratios
against that baseline plus a peak-area verdict.

**Uploads wait for the gold standard.** With cloud upload configured, an
instrument's runs at an SPD are held on the PC — not uploaded — until a
baseline exists for it, and again whenever gold-standard runs (`SSCGOLD`, or
the older `SSC0`) arrive, until they are saved as the new baseline. Every page
shows a red banner while anything is held. Saving the baseline re-scores the
held runs against it and uploads them, so nothing reaches the platform compared
against the wrong reference. Two buttons on the Gold standards page cover the
exceptions: **Pause uploads for a new baseline**, for gold-standard runs that
weren't marked as such, and **Upload as they are**, to release held runs
without a new baseline. To turn the hold off entirely, set
`hold_until_gold_standard = false` under `[cloud]`.

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
| Log shows `skyr_column_not_found`, and Diagnostics says runs "used the file mtime" | The report's Acquired Time column path doesn't exist for the report's row source. `Results!*.Value.PeptideResult.ResultFile.AcquiredTime` works in a precursor-level report like the one that ships; a peptide-level report needs a different path. Skyline writes `#COLUMN … NOT FOUND#` instead of failing | Add **Acquired Time** with Skyline's report editor rather than pasting a path — Skyline then writes the one that fits. Each payload's `acquisition_time_source` says `instrument` once it works |
| Agent log says `MD_QC_Report does not exist` | `MD_QC_Report.skyr` missing from `methods/` | Copy it in. It ships with the agent under `src/mdqc/methods/`. |
| Skyline error: `file is being used by another process` | Concurrent Skyline runs writing to the same `.skyd` cache | Already handled in code via per-extraction temp dir + library hardlinking. If you still see this, kill all stuck `SkylineCmd.exe` processes via Task Manager and restart the agent. |
| `instrument_id` shows as filename prefix instead of your config ID | The watch path differs from `instrument.watch_path` after `.resolve()` (mapped/UNC drive issue) | The agent has a single-instrument fallback; if you have one instrument configured, this works automatically. For multi-instrument setups use UNC paths consistently. |
| Empty Library Dot Product panel | Skyline didn't compute library scores for these peaks (insufficient fragments or no library match) | The dashboard now auto-hides empty metrics — refresh to reflow |

### Where to look for logs
- **Agent**: `C:\ProgramData\MassDynamics\QC\logs\mdqc.log` (JSONL format)
- **Failed files**: `C:\ProgramData\MassDynamics\QC\failed_files.json`
- **Activity history**: `C:\ProgramData\MassDynamics\QC\activity_log.json`

### Stopping the agent cleanly
- **Ctrl+C** in the PowerShell window, if it runs in one, OR
- from any PowerShell window, ask it to stop (it finishes the file in hand):
  ```powershell
  $info = Get-Content C:\ProgramData\MassDynamics\QC\runtime.json | ConvertFrom-Json
  Invoke-RestMethod -Method Post "http://127.0.0.1:$($info.port)/api/restart?token=$($info.token)"
  ```
  Run as a Windows service, it is restarted by the service manager; stop the
  service instead (`Stop-Service MassDynamicsQC`, elevated). Quitting the tray
  icon closes only the icon, not the agent.

---

## 9. What to send back to Mass Dynamics

To debug an installation, the most useful artefacts are:
1. **`config.toml`** (redact instrument IDs if needed)
2. **`logs/mdqc.log`** (last ~200 lines around the issue)
3. **One example payload** from `spool/completed/` (the JSON contains a
   per-target metric breakdown — that's what tells us what Skyline
   actually found)
4. The Skyline document folder used (`.sky` + `.blib` files) if the
   issue is method-related

A simple `zip -r qc_logs.zip C:\ProgramData\MassDynamics\QC\logs\` plus the
`spool\completed` folder is enough.

---

## 10. Known limitations

- **Multi-instrument filtering is untested at scale.** More than one
  instrument per config is supported by the schema and the UI, but the
  filter UX has not been stress-tested with many instruments.
- **No per-template auto-detection yet.** All raw files in a watch path
  use the same Skyline template; multi-template routing by filename is on
  the roadmap.
- **Method/file mismatch detection is reactive.** The dashboard shows a
  diagnostic banner when `targets_found = 0`, but the agent doesn't
  yet refuse to extract a file whose acquisition method doesn't fit
  the template.
- **Spool retention** is a fixed count (`completed_retention_count`).
  No batch tagging or QC-vs-experiment separation yet.

These are all v1.1 candidates.

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

Questions or issues: contact engineering@massdynamics.com or open
a GitHub issue at <https://github.com/MassDynamics/md-qc/issues>.
