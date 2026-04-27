# MD QC Agent — Python Port Build Plan

**Source:** Rust `mdqc-agent` v0.9.17 (~12k LOC) at `../src/`
**Target:** Python 3.11+, Windows-first, single deployable artifact
**Approach:** Lean port — web-based wizard, NSSM-wrapped service, token-first auth. See "Hard parts" below.

---

## 1. Goals & non-goals

### Goals
- Behavioural parity with the Rust agent for the **core data path**: watch → finalize → classify → extract → spool → upload.
- Same `config.toml` schema, same `C:\ProgramData\MassDynamics\QC\` layout, same payload JSON schema (so the existing cloud ingest and Streamlit dashboard work unchanged).
- Same CLI surface (`mdqc doctor`, `mdqc status`, `mdqc classify`, `mdqc run --foreground`, etc.).
- Single-file Windows installer (.exe via Inno Setup) that drops in a PyInstaller-built `mdqc.exe` + bundled assets + NSSM service wrapper.
- Cross-platform development on macOS/Linux; Windows-only deployment.

### Non-goals (v1)
- **No 1:1 GUI port.** The eframe wizard is replaced by a local FastAPI app opened in the browser.
- **No native Windows service code.** NSSM wraps `mdqc.exe run --service-mode`. Recovery + auto-start are configured by the installer, not by the binary.
- **No mTLS via the Windows certificate store** in v1. Bearer-token auth only. mTLS is a v1.1 task that may use `pywin32` + `certutil` to export a PFX at startup.
- No baseline cloud sync (the Rust code stubs this too — keep parity).
- No auto-update implemented in-process. `update_checker.rs` becomes "show toast if newer GitHub release exists"; actual update happens via re-running the installer.

---

## 2. Architecture decisions for the hard parts

| Hard problem | Rust approach | Python decision | Why |
|---|---|---|---|
| GUI wizard (5 screens) | `eframe`/`egui` immediate-mode | **FastAPI + HTMX served on `127.0.0.1:<random>`**, opened in default browser by tray menu. | Reuses team's existing Python web skills. Same machinery serves the diagnostics/status pages. No PyInstaller pain from Qt. The Streamlit dashboard already lives separately — don't conflate it with the wizard. |
| Windows service | `windows-service` crate, registers `MassDynamicsQC` service | **NSSM wraps `mdqc.exe run --service-mode`** as a headless background service. The tray runs as a **separate per-user process** (see § 2.5 Process model). `mdqc.exe run --service-mode` handles `SIGTERM` (NSSM sends it on stop). | Removes 600+ lines of Rust scaffolding. NSSM is BSD-licensed, single binary. Recovery (`AppExit Restart`, throttle) is set in NSSM config. The service-vs-tray split is mandatory because services run in Session 0 and can't display tray icons or toasts. |
| mTLS w/ Windows cert store | `reqwest` native-tls + CryptoAPI export via PowerShell | **v1: token auth only, with a hard fail-fast for cert-configured deployments.** If `[cloud] certificate_thumbprint` is set, the agent refuses to start with a clear error pointing at v1.1. v1.1 implementation: PowerShell `Export-PfxCertificate` → `cryptography.hazmat.primitives.serialization.pkcs12` → temp PEM files → `httpx` SSL context. See AGENT_NOTES § Future for the corrected snippet. | A silent fall-through to local-only mode would break every cert-configured site. Defer the cert work, but make the deferral visible. |
| System tray | `tray-icon` + `winit` event loop | **`pystray`** (PIL backend). Menu items: Open Dashboard, Open Wizard, Open Logs Folder, Open Spool Folder, Run Diagnostics, Pause/Resume, Quit. | `pystray` is the only viable cross-platform-ish option. Runs in its own thread, talks to the core via an `asyncio.Queue` bridged with `loop.call_soon_threadsafe`. |
| Toast notifications | `winrt-notification` | **`winsdk`** (modern, MSIX-aware). Fall back to log-only if AUMID isn't registered. | `win10toast` is unmaintained. `winsdk` is Microsoft's official Python projection of Windows Runtime APIs. |
| File watching | `notify` (ReadDirectoryChangesW) + manual polling fallback | **`watchdog`** with the `WindowsApiObserver` for local paths, **`PollingObserver`** for UNC paths, both gated by a `is_unc(path)` check. | `watchdog` is the de-facto Python equivalent. Polling fallback is a one-line constructor swap. |
| Config | `serde` + `toml` | **`tomllib`** (stdlib, 3.11+) → **`pydantic-settings`** for validation and defaults. | Pydantic gives validation + good error messages for free. No external TOML dep needed. |
| HTTP + retry | `reqwest` + `backoff` | **`httpx`** + **`tenacity`** with explicit `wait_chain` matching the Rust schedule (not exponential — see AGENT_NOTES). | `httpx` supports both sync and async, has good proxy + cert support. `tenacity` allows the exact 0/30/120/600/3600 schedule with jitter. |
| Subprocess | `tokio::process` | **`asyncio.create_subprocess_exec`** + **`psutil`** for setting `BELOW_NORMAL_PRIORITY_CLASS` post-spawn. | Don't use `creationflags=` for priority — see AGENT_NOTES.md, this is the same trap the Rust code documents. |
| Crash reporting | Custom panic hook → MessageBoxW + GitHub URL | **`sys.excepthook`** + **`faulthandler`** for native crashes; same MessageBoxW via `ctypes`. | Same UX, simpler implementation. `faulthandler` covers C-extension SIGSEGVs. |
| Packaging | `cargo build --release` → `mdqc.exe` (~10 MB) | **PyInstaller `--onefile`** → `mdqc.exe` (~50 MB), wrapped by Inno Setup. NSSM bundled in. | The existing `installer/` directory is Inno Setup — reuse it almost verbatim. Just point at the PyInstaller output. |

---

## 2.5 Process model

The agent runs as **two cooperating processes** on every Windows install. This is non-negotiable: Windows services run in Session 0, which has no GUI access — a single-process design cannot show a tray icon, raise toasts, or open a browser as the user.

```
                     ┌──────────────────────────────────────────────┐
                     │  Session 0 (NSSM-managed Windows service)    │
                     │                                              │
                     │   mdqc.exe run --service-mode                │
                     │   ├── watcher                                │
                     │   ├── extractor (SkylineCmd subprocess)      │
                     │   ├── spool                                  │
                     │   ├── uploader                               │
                     │   └── FastAPI on 127.0.0.1:<random>          │
                     │       ├── /api/*       (control + status)   │
                     │       ├── /events      (SSE event stream)    │
                     │       ├── /wizard      (first-run UI)        │
                     │       ├── /dashboard   (status + activity)   │
                     │       ├── /diagnostics (mdqc doctor view)    │
                     │       └── /failed      (failed-files mgr)    │
                     └──────────────────────────────────────────────┘
                                          ▲
                                          │  HTTP loopback + token
                                          │  (port + token written to
                                          │   %PROGRAMDATA%\…\runtime.json
                                          │   readable only by service
                                          │   account + Administrators)
                                          │
                     ┌──────────────────────────────────────────────┐
                     │  User session (per-user, autostart at login) │
                     │                                              │
                     │   mdqc.exe tray                              │
                     │   ├── pystray icon + menu                    │
                     │   ├── toast publisher (winsdk)               │
                     │   ├── browser launcher                       │
                     │   └── SSE client → /events                   │
                     └──────────────────────────────────────────────┘
```

### Responsibilities

| Concern | Service | Tray |
|---|---|---|
| File watching, extraction, upload | ✅ | — |
| Persistent state (spool, registries) | ✅ | — |
| HTTP API + Web UI (wizard, dashboard, diagnostics) | ✅ | — |
| Tray icon + context menu | — | ✅ |
| Toast notifications | — | ✅ (subscribes to `/events`) |
| Browser launch ("Open Wizard", "Open Dashboard") | — | ✅ |
| Cross-process command relay (e.g. menu "Pause" → service) | — | ✅ (via `/api`) |

### IPC contract

- **Transport:** HTTP/1.1 over TCP loopback. The service binds `127.0.0.1` on an ephemeral port chosen at startup.
- **Discovery:** the service writes `%PROGRAMDATA%\MassDynamics\QC\runtime.json` containing `{port, token, pid, started_at}` atomically (`.tmp` + `os.replace`). The tray polls this file at startup with a 30s timeout, then connects.
- **Auth:** every request must carry `X-MDQC-Token: <token>`. Token is 32 bytes from `secrets.token_urlsafe`. Rotated on every service start.
- **Same scheme for the browser:** when the tray opens the wizard, it appends `?token=<token>` to the URL. The FastAPI middleware accepts the token from header *or* query string; on first page load, the page sets a session cookie and the query parameter is dropped from the URL.
- **Events:** `/events` is a Server-Sent Events stream. Event types: `extraction_started`, `extraction_completed`, `extraction_failed`, `upload_succeeded`, `upload_failed`, `update_available`, `paused`, `resumed`. The tray maps events to toasts (with batching — see AGENT_NOTES § Notifications).
- **Control endpoints:** `POST /api/pause`, `POST /api/resume`, `POST /api/reprocess`, `POST /api/failed/retry`, `GET /api/status`, etc. CLI commands like `mdqc status` are thin clients of this API.

### CLI commands the tray spawns or proxies

- `mdqc tray` — launches the tray (long-running). Started at login by the installer adding to `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.
- `mdqc run --service-mode` — the headless service entry point. NSSM-managed.
- `mdqc run --foreground` — dev-mode equivalent: runs both service work AND tray UI in one process for fast iteration on macOS/Linux.

### Bootstrap sequence after install

1. Installer registers NSSM service → service starts, writes `runtime.json`, listens on FastAPI but watcher is idle (no config yet).
2. Installer adds `mdqc tray` to user `Run` key → on next login, tray starts, reads `runtime.json`, connects.
3. If config missing: tray opens browser to `/wizard` automatically. Otherwise, tray sits in the system tray.
4. User completes wizard → service writes `config.toml` → service hot-reloads → watcher starts.

---

## 3. Project layout

```
python-port/
├── pyproject.toml
├── README.md
├── PLAN.md
├── AGENT_NOTES.md
├── src/
│   └── mdqc/
│       ├── __init__.py
│       ├── __main__.py             # python -m mdqc → cli.app
│       ├── cli/
│       │   ├── __init__.py         # typer app
│       │   ├── run.py              # mdqc run [--foreground|--service-mode]
│       │   ├── tray.py             # mdqc tray (per-user UI process)
│       │   ├── doctor.py
│       │   ├── status.py           # thin client of service /api/status
│       │   ├── classify.py
│       │   ├── failed.py
│       │   ├── config_cmd.py       # mdqc config validate/show
│       │   ├── baseline.py
│       │   └── reprocess.py
│       ├── ipc/
│       │   ├── __init__.py         # client + server helpers
│       │   ├── runtime.py          # read/write %PROGRAMDATA%/runtime.json
│       │   └── client.py           # httpx-based wrapper used by tray + CLI
│       ├── config/
│       │   ├── __init__.py         # Config (pydantic model) + load()
│       │   ├── paths.py            # data_dir, log_dir, spool_dir, ...
│       │   └── defaults.py         # all magic numbers in one place
│       ├── watcher/
│       │   ├── __init__.py         # Watcher orchestrator
│       │   ├── observer.py         # watchdog wrapper + UNC detection
│       │   ├── finalizer.py        # state machine
│       │   ├── vendor.py           # per-vendor stability checks
│       │   └── registry.py         # processed-files JSON store
│       ├── classifier.py           # ~280 LOC, single file
│       ├── extractor/
│       │   ├── __init__.py         # ExtractionResult dataclass
│       │   ├── skyline.py          # discovery + invocation
│       │   └── report.py           # CSV → metrics
│       ├── metrics.py              # numpy-based scoring
│       ├── baseline.py             # in-memory cache (parity with Rust stub)
│       ├── spool/
│       │   ├── __init__.py         # Spool class
│       │   ├── store.py            # atomic write, state transitions
│       │   └── prune.py            # size + age policies
│       ├── uploader.py             # httpx + tenacity
│       ├── notifications.py        # winsdk wrapper, no-op on non-Windows
│       ├── tray.py                 # pystray, Windows-only entry point
│       ├── webui/                  # FastAPI app
│       │   ├── __init__.py
│       │   ├── server.py           # uvicorn launcher (random port)
│       │   ├── wizard.py           # /wizard endpoints
│       │   ├── dashboard.py        # /dashboard (status, queue, recent)
│       │   ├── diagnostics.py      # /doctor (web view of CLI doctor)
│       │   ├── failed.py           # /failed list + retry
│       │   ├── logs.py             # /logs tail (SSE)
│       │   ├── templates/          # Jinja2 templates (HTMX)
│       │   └── static/             # CSS, MD logo
│       ├── service/
│       │   ├── __init__.py
│       │   └── lifecycle.py        # SIGTERM handling, graceful shutdown
│       ├── crash.py                # excepthook + MessageBox
│       ├── failed_files.py
│       ├── activity_log.py
│       ├── update_checker.py
│       ├── types.py                # ControlType, WellPosition, Vendor enums
│       └── log.py                  # structlog setup
├── assets/                         # Symlink or copy of ../assets at build time
│   ├── QC_Method.sky
│   ├── MD_QC_Report.skyr
│   └── icon.png
├── installer/
│   ├── mdqc.iss                    # Inno Setup script (adapted from ../installer)
│   └── nssm.exe                    # bundled NSSM binary
├── tests/
│   ├── unit/
│   ├── integration/
│   │   └── test_skyline_e2e.py     # marked @pytest.mark.skyline, opt-in
│   └── fixtures/
│       ├── sample.csv              # Skyline report fixtures
│       └── classifier_corpus.txt   # filenames → expected classification
└── scripts/
    ├── build.py                    # PyInstaller invocation
    └── package.py                  # Inno Setup invocation
```

---

## 4. Tech stack

See `pyproject.toml` for pinned versions. Headlines:

- **Runtime:** Python 3.11+ (need `tomllib`, `ExceptionGroup`, async improvements)
- **CLI:** `typer` (built on `click`, gives free `--help` + completion)
- **Config:** `pydantic` v2 + `pydantic-settings`, `tomllib` (stdlib)
- **Async runtime:** `asyncio` (stdlib); avoid `trio`/`anyio` unless we hit asyncio-specific pain
- **HTTP:** `httpx` (sync + async) + `tenacity` (retry policies)
- **File watching:** `watchdog`
- **Process control:** `psutil` (priority classes, child tracking)
- **Logging:** `structlog` (JSON output) + `python-json-logger`
- **Web UI:** `fastapi` + `uvicorn` + `jinja2` + HTMX (vendored, no npm)
- **Tray:** `pystray` + `pillow`
- **Windows-specific:** `pywin32`, `winsdk`, `winreg` (stdlib)
- **Testing:** `pytest`, `pytest-asyncio`, `pytest-httpx`, `freezegun`, `respx`
- **Packaging:** `pyinstaller`

---

## 5. Phased build plan

Each phase is a vertical slice. The agent must work end-to-end at the end of every phase, even if some features are stubbed.

### Phase 0 — Foundations (3–4 days)
- `pyproject.toml`, virtual env, `pre-commit` (ruff, mypy, pytest)
- `mdqc.config` — Pydantic models for the full TOML schema, `load()` from `MDQC_CONFIG` env or default path
- `mdqc.config.paths` — replicate Rust path resolution exactly (`%PROGRAMDATA%\MassDynamics\QC\…`)
- `mdqc.config.defaults` — every magic number in one module, sourced from AGENT_NOTES.md
- `mdqc.types` — enums for `ControlType`, `Vendor`, `FinalizationState`, `Confidence`, `Source`; serde-compatible JSON encoding
- `mdqc.log` — structlog config, JSON to file + human to console
- `mdqc.cli` — `typer` skeleton with all 12 subcommands as no-ops
- **Acceptance:** `mdqc --help`, `mdqc config validate` (parses fixture configs), `mdqc version` all work.

### Phase 1 — Classifier + Watcher (1 week)
- `mdqc.classifier` — direct port of regex order, well-position rules, confidence scoring. **Read AGENT_NOTES § Classifier.**
- `mdqc.watcher.observer` — `watchdog` wrapper, UNC detection via path prefix + `GetDriveTypeW` (ctypes)
- `mdqc.watcher.finalizer` — DETECTED → STABILIZING → READY → PROCESSING → DONE/FAILED state machine
- `mdqc.watcher.vendor` — per-vendor stability checks (Bruker `analysis.tdf` + lock files, Waters `_FUNC001.DAT`, Thermo non-share open)
- `mdqc.watcher.registry` — JSON-backed processed-files set with FIFO eviction at 10k
- `mdqc.cli.classify` — wire up the `mdqc classify <path>` command
- **Acceptance:** `mdqc classify` matches Rust output for the entire `tests/fixtures/classifier_corpus.txt`. Watcher integration test with `tempfile` + simulated raw files passes. Bruker .d folder with lock file is correctly held in STABILIZING.

### Phase 2 — Skyline Extractor (1 week)
- `mdqc.extractor.skyline` — discovery (config path → registry → known paths → PATH), invocation with timeout, **priority set post-spawn via psutil** (see AGENT_NOTES § Extractor)
- `mdqc.extractor.report` — CSV parser with case-insensitive column normalization, alias map, extra-metric passthrough
- `mdqc.extractor.__init__` — `ExtractionResult` dataclass mirroring Rust struct, run_id (uuid4) generation, template hash (SHA256) calculation
- ClickOnce path detection → fail loudly with the same error message as Rust
- **Acceptance:** Given a fixture Skyline CSV report, parser produces identical `target_metrics` JSON to the Rust version. Subprocess timeout terminates and marks Failed cleanly.

### Phase 3 — Spool + Uploader (1 week)
- `mdqc.spool.store` — atomic write (`.tmp` → `os.replace`), state directory transitions, correlation ID format `{agent_id}-{YYYYMMDDhhmmss}-{8 hex}`
- `mdqc.spool.prune` — size cap (1 GB), age cap (30 days), completed retention (10)
- Crash recovery: on startup, move everything in `uploading/` back to `pending/`
- `mdqc.uploader` — `httpx` POST with **`tenacity.wait_chain` of 4 inter-retry waits** matching Rust schedule (see AGENT_NOTES § Uploader — Tenacity off-by-one trap). 401/403 → no retry. Bearer token auth.
- **Cert-configured-but-unsupported guard:** if `[cloud] certificate_thumbprint` is set, refuse to start with a clear error referencing v1.1. Do not silently fall through to local-only mode.
- Local-only mode only when **both** `api_token` and `certificate_thumbprint` are unset.
- **Acceptance:**
  - Kill -9 in the middle of `enqueue()` leaves either the temp file or the final file, never both.
  - End-to-end test with `respx` mock cloud succeeds + fails + recovers.
  - **Timing test using `freezegun`:** record sleep durations across all 5 attempts; assert intervals fall within the four ranges (20–40, 90–150, 480–720, 3000–4200 seconds). This is the canary for the Tenacity off-by-one trap.
  - Startup test: config with `certificate_thumbprint` set and no `api_token` exits with non-zero code and a clear message.

### Phase 4 — Service lifecycle + IPC + Tray + Notifications (1.5 weeks)
- `mdqc.service.lifecycle` — `signal.signal(SIGTERM, ...)` triggers `asyncio` graceful shutdown; drains in-flight uploads with a 30s timeout
- `mdqc.cli.run` — `--foreground` for dev, `--service-mode` for NSSM (same code path, only logging destination differs)
- `mdqc.ipc.runtime` — atomic write/read of `runtime.json` (port + token + pid); tray polls with timeout
- `mdqc.ipc.client` — httpx-based client used by both tray and CLI (`mdqc status` etc. become thin clients of the service API)
- `mdqc.cli.tray` — `pystray` icon + menu in its own process. Connects to service via IPC. Subscribes to `/events` SSE stream.
- `mdqc.notifications` — `winsdk` toast wrapper running **inside the tray process only** (services can't raise toasts visibly). AUMID `MassDynamics.QCAgent` (must match installer-created shortcut). Silent for routine events, sound for errors. Batches with a 30s window (see AGENT_NOTES).
- `mdqc.crash` — `sys.excepthook` + `faulthandler`; writes report to `crashes/`, shows MessageBoxW (tray only — service-side crashes go to log + Windows Event Log), opens GitHub issue URL with truncated body
- **Acceptance:**
  - Service starts, writes `runtime.json`, accepts authenticated requests; rejects requests with bad/missing token (401).
  - Tray reads `runtime.json`, connects, shows icon, opens browser to wizard URL on click.
  - Killing tray does not affect service; killing service causes tray to show a "service stopped" indicator within 5s.
  - Toast appears for `extraction_failed` event delivered via SSE.
  - SIGTERM to service during processing leaves spool in a recoverable state.

### Phase 5 — Web UI (Wizard + Dashboard + Diagnostics) (2 weeks)
- `mdqc.webui.server` — uvicorn on `127.0.0.1:<ephemeral>` **hosted by the service** (not the tray). Token-authenticated via `X-MDQC-Token` header or `?token=` query string. Token is the same one written to `runtime.json` and consumed by IPC.
- `mdqc.webui.wizard` — 5 screens matching Rust wizard: Vendor → Instrument & Path → Skyline → Template → Output
- `mdqc.webui.diagnostics` — `mdqc doctor` rendered as HTML
- `mdqc.webui.dashboard` — queue status, recent activity, baseline summary
- `mdqc.webui.failed` — list + retry/clear per file
- `mdqc.webui.logs` — SSE-tailed `mdqc.log`
- HTMX for all interactivity. No build step. Vendor `htmx.min.js` into `static/`.
- **Acceptance:** First-run wizard writes a valid `config.toml` and starts the watcher. All five screens render without JS errors.

### Phase 6 — Packaging + Installer (1 week)
- `scripts/build.py` — PyInstaller spec, includes `assets/`, hidden imports for `winsdk` + `pystray` + `watchdog.observers.winapi`
- Adapt `../installer/mdqc.iss` to point at PyInstaller output, bundle `nssm.exe`, register service via NSSM commands in `[Run]` section
- AUMID Start Menu shortcut for toast notifications
- Installer adds `mdqc tray` to `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` so the tray autostarts at login
- Code-signing hook (out of scope for MVP; document where to add it)
- **Packaging smoke test:** post-build, run a script that imports `win32file`, `winsdk.windows.ui.notifications`, `pystray._win32`, `watchdog.observers.winapi`, and `cryptography.hazmat.primitives.serialization.pkcs12` from inside the frozen `mdqc.exe` (e.g. via `mdqc selfcheck`). CI fails the build if any import raises. This is the canary for the "Windows extras missed" packaging trap.
- **Acceptance:** Fresh Windows VM: `mdqc-setup-py-vX.Y.Z.exe` → boot → service starts → on next login, tray icon appears → wizard runs → drop a fixture file → upload completes.

### Phase 7 — Hardening + parity tests (ongoing)
- Side-by-side comparison harness: feed the same input files to Rust + Python agents, diff the resulting payloads
- Soak test: 1k files across 24h on a Windows VM
- mTLS support (deferred from v1)
- Real cloud staging endpoint test
- Performance: profile startup time, memory baseline, idle CPU

**Total estimated effort:** ~6–8 weeks of focused work for a single competent engineer. ~10–12 weeks accounting for integration friction, code review, and the inevitable Windows surprises.

---

## 6. Testing strategy

| Tier | Tooling | What it covers |
|---|---|---|
| Unit | `pytest` | Pure functions (classifier, metrics, config validation, CSV parsing) |
| Integration | `pytest` + `tempfile` + `respx` | Watcher → finalizer state transitions, spool atomicity, uploader retry chain |
| Parity | Custom harness | Run Rust + Python agents on identical fixtures, diff payloads |
| End-to-end | Windows VM in CI (`windows-latest` runner) | Full install → wizard → process file → upload |
| Smoke (manual) | Real instrument PC | Final acceptance before each release |

**Test pyramid:** ~70% unit, ~25% integration, ~5% E2E. The watcher and spool need especially heavy integration coverage — they're where state corruption hides.

**Fixture corpus:** Build `tests/fixtures/classifier_corpus.txt` with at least 200 filenames covering: each control type × each vendor × edge cases (mixed case, unusual delimiters, missing well, ambiguous names). Use this as the parity contract.

---

## 7. Packaging & release

- **Versioning:** Match Rust agent's version scheme. Parallel Rust + Python versions during transition (e.g. `1.0.0-rust` and `1.0.0-py`).
- **Release artefacts:** `mdqc-py-setup-vX.Y.Z.exe` (Inno Setup wrapping PyInstaller output + NSSM)
- **CI:** GitHub Actions on `windows-latest` for build + test + package; macOS/Linux runners for unit tests only
- **Code signing:** Same EV cert as Rust agent. Sign both `mdqc.exe` and the installer.
- **Auto-update:** Same approach as Rust — check GitHub releases once per 24h, toast if newer; do not self-update.

---

## 8. Migration & rollout strategy

1. **Build the Python agent in parallel** — do not delete the Rust code.
2. **Internal dogfood (1 site, 1 month)** — install on an MD test instrument, run alongside Rust agent (different config/spool paths), compare uploads daily.
3. **Customer beta (3–5 sites, 2 months)** — opt-in. Customers can switch back to Rust at any time; both agents read/write the same config schema.
4. **General availability** — once parity test has zero diffs for 4 consecutive weeks across the beta cohort.
5. **Sunset Rust** — only after every customer is on Python and the cloud team confirms no Rust-version payloads have arrived for 30 days.

The two implementations writing the same payload schema is the linchpin. Don't change the schema during the port — file any schema changes as a separate cross-language project after the migration completes.

---

## 9. Open questions (for Andrew / engineering)

- [ ] **How many existing customer sites use `certificate_thumbprint` mTLS today?** If non-zero, v1 must include mTLS — the fail-fast guard is then a *temporary* gate during development, not a shipping behaviour. If zero, the deferral is fine.
- [ ] Confirm Bearer token auth is acceptable for v1 (assuming above answer is "zero cert sites" or "we will migrate them first").
- [ ] Confirm NSSM is OK with the team's Windows IT review — some shops prohibit third-party service wrappers. If not, we need pure `pywin32` service code (adds ~1 week).
- [ ] Does the cloud ingest endpoint already accept payloads with `agent_version` containing a `-py` suffix, or do we need a flag on the request?
- [ ] Streamlit dashboard lives in `../streamlit-qc/`. Does it stay separate, or fold into the FastAPI web UI? (Recommendation: keep separate. Different audiences, different deployment lifecycles.)
- [ ] Code-signing certificate access for CI — same as Rust? Need to confirm with whoever holds the EV cert.
- [ ] **Wizard file picker UX:** the Rust wizard uses native `rfd` file dialogs to pick the Skyline path and template path. A browser-based wizard cannot show a native picker (security model). Options: (a) text input + "Detect" button that scans known locations; (b) a list of likely paths from the registry; (c) a small native helper invoked via IPC. Recommendation: (a) + (b) for v1.

---

## 10. Definition of done (v1)

- All Phase 0–6 acceptance criteria pass in CI
- Parity harness shows zero diffs across the fixture corpus
- 24h soak test on a Windows VM with synthetic file generator: zero crashes, zero spool corruption, all payloads uploaded
- One real customer site running for ≥2 weeks with no critical issues
- AGENT_NOTES.md is updated with anything learned during the build that wasn't already captured
