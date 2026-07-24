# AGENT_NOTES.md — Read This Before Touching Any Module

Future agents working on the Python port: **read this file end-to-end before opening an editor**. Every gotcha here represents real scar tissue from the Rust implementation. Re-discovering them in Python will cost days each.

The Rust source in `../src/` is the behavioural contract. The spec at `../SPEC.md` is the design contract. This file is the *delta* — the things that aren't obvious from either.

---

## Cross-cutting principles

- **Do not change the payload JSON schema.** The cloud ingest and Streamlit dashboard depend on exact field names and types. If you think a field should change, file an issue against the cross-language schema instead of "fixing" it in Python.
- **Do not change the `config.toml` schema.** Customers may be running the Rust and Python agents side-by-side during migration; both must read the same file.
- **Do not change directory layout under `%PROGRAMDATA%\MassDynamics\QC\`.** Spool paths, log paths, methods directory — all of it is part of the contract.
- **Match Rust magic numbers exactly.** Every timeout, retry interval, queue limit, eviction count is documented below. Do not "round" or "tune" them. They were calibrated against real instrument behaviour.
- **Default to copying behaviour, not refactoring it.** A Pythonic rewrite of the finalizer "while we're here" will reintroduce bugs that the Rust state machine handles correctly.
- **Atomic writes are not optional.** Anywhere the Rust code writes `.tmp` then renames, the Python code must do the same with `os.replace()` (Windows-safe atomic rename).

---

## Watcher (`mdqc.watcher`)

### File detection

- **UNC paths must use the polling observer, not the WinAPI observer.** SMB/CIFS does not deliver `ReadDirectoryChangesW` notifications reliably. Detect via path prefix `\\` *and* `ctypes.windll.kernel32.GetDriveTypeW(path)` returning `4` (`DRIVE_REMOTE`). Either signal alone is insufficient — some mapped network drives don't have `\\` prefix; some local paths can look UNC-ish.
- **Filesystem events are hints, not truth.** Always re-verify via the finalization state machine. Never trigger extraction directly from an event.
- **The processed-files registry caps at 10,000 entries** with FIFO eviction by lexicographic sort. Assumes filenames are roughly date-prefixed. If you change the eviction policy, write a migration that loads the old format.
- **On startup, move everything in `spool/uploading/` back to `spool/pending/`.** This is crash recovery — assume the previous process died mid-upload.

### Finalization state machine

States: `DETECTED → STABILIZING → READY → PROCESSING → DONE | FAILED`. Once in DONE or FAILED, the file is removed from tracking; **the state machine is not re-entrant for the same path**.

| Timer | Default | What it controls |
|---|---|---|
| `stability_window_seconds` | **60** | File must show *no* size or mtime change for this many seconds before promoting from STABILIZING → READY |
| `stabilization_timeout_seconds` | **600** | Hard upper bound. If a file hasn't stabilized in 10 minutes, mark FAILED. Prevents indefinite hangs on stuck acquisitions. |
| Processing timeout | **1800** (30 min) | Once in PROCESSING, if `mark_done`/`mark_failed` isn't called, FAIL. Prevents zombie state if extractor hangs. |
| Bruker stability override | **90** | Bruker `.d` folders need a longer window — `timsdata.dll` keeps file handles open after acquisition appears done. |

### Vendor-specific finalization checks

| Vendor | Artifact | Stability check |
|---|---|---|
| Thermo | `.raw` file | Size + mtime stable, then attempt non-shared open |
| Bruker | `.d` directory | `analysis.tdf` present and stable, **AND** neither `analysis.tdf-journal` nor `analysis.tdf-lock` exists |
| Waters | `.raw` directory | `_FUNC001.DAT` present and stable |
| Sciex | `.wiff` + `.wiff.scan` | **Both files** stable (do not check just one) |
| Agilent | `.d` directory | `AcqData/` subdirectory complete |

### The "exclusive open" test

The Rust code uses Windows `CreateFileW` with `dwShareMode = 0` (`FILE_SHARE_NONE`) to verify no other handles exist. **In Python, this requires `pywin32` (`win32file.CreateFile`)**. The naive `open(path, 'rb')` test in CPython sets share mode to allow read+write+delete, which means it succeeds even while the instrument is still writing. **Do not skip this; it is the most common cause of corrupt extractions.**

For directories (Bruker `.d`, Waters `.raw`), open the vendor-specific key file inside (`analysis.tdf`, `_FUNC001.DAT`) — you can't open a directory exclusively.

On non-Windows (dev/test only), fall back to `open(path, 'rb')`. Tests should be marked `@pytest.mark.windows_only` for the real check.

---

## Classifier (`mdqc.classifier`)

- **Regex pattern order matters.** `SSC0` must be tried before `QCA` before `QCB` before `BLANK`. Some filenames contain multiple tokens (e.g. a sample with "QCA-rerun" in the name); first match wins.
- **Python's `\b` word boundary works at underscore boundaries — Rust's does not.** The Rust patterns use explicit delimiter classes `(?:^|[_\-\s.])`. Replicate this in Python anyway, because some real filenames have `..` or `.-` between tokens, and `\b` won't fire there either.
- **Tokens are case-insensitive.** Use `re.IGNORECASE`.
- **Well-position inference:**
  - `A1`, `A2` → `QC_A`
  - `A3`, `A4` → `QC_B`
  - All other positions → `SAMPLE` (do not infer)
  - Only used as a fallback if no filename token matches.
- **Confidence scoring:**
  - `HIGH`: filename token AND well position both detected
  - `MEDIUM`: filename token only OR well-position-inferred control type
  - `LOW`: position-only inference, or no detectable pattern
- **Plate ID regex:** `\b(plate[_-]?\w+|plt[_-]?\w+|P\d{2,})\b`, case-insensitive. Don't tighten this — customer naming conventions are wildly inconsistent.

**Build a parity test corpus** (`tests/fixtures/classifier_corpus.txt`) by running the Rust `mdqc classify` against ≥200 real filenames and recording the output. Any Python regex change must keep this corpus passing.

---

## Extractor (`mdqc.extractor`)

### SkylineCmd discovery

Discovery order (first match wins):
1. Explicit `[skyline] path` from config
2. Registry: `HKEY_LOCAL_MACHINE\SOFTWARE\Apache\Skyline\Path` (and `SkylineLauncherVersion`)
3. `C:\Program Files\Skyline\SkylineCmd.exe`
4. `C:\Program Files (x86)\Skyline\SkylineCmd.exe`
5. `PATH`

### ClickOnce trap

- **ClickOnce installs of Skyline cannot be invoked headlessly.** They live under `%LOCALAPPDATA%\Apps\2.0\<hash>\<hash>\` and Windows blocks direct exec from there.
- Detect via path string containing `\Apps\2.0\` and emit the same error as Rust: instruct user to install the MSI version.
- This is non-negotiable on Windows. Don't try to work around it.

### Subprocess invocation — the priority class trap

- **Do NOT use `subprocess.Popen(creationflags=BELOW_NORMAL_PRIORITY_CLASS)`.** This causes "OS error 50" (`ERROR_NOT_SUPPORTED`) on Windows because it replaces the inherited `EXTENDED_STARTUPINFO_PRESENT` flag. The Rust code documents this exact bug.
- **Set priority *after* spawn** using `psutil.Process(pid).nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)`. There is a small race window between spawn and nice() during which Skyline runs at normal priority — this is acceptable.

### Skyline command line

```
SkylineCmd.exe \
  --in="<template_path>" \
  --import-file="<raw_file_path>" \
  --report-name="MD_QC_Report" \
  --report-file="<output_csv_path>" \
  --report-format=csv
```

- Default timeout: **900s** (15 minutes). Configurable via `[skyline] timeout_seconds`.
- Working directory: spool work directory.
- Capture both stdout and stderr.
- **Skyline writes errors to STDOUT, not just stderr.** Check stdout first when classifying failures. The Rust code checks stdout, then stderr, then exit code.

### Report name resolution

- Look for a custom `.skyr` file alongside the template; if present, parse `<view name="...">` from XML to extract the report name.
- If absent, fall back to bundled default (`MD_QC_Report`).
- Don't hardcode "MD_QC_Report" — customers may rename it.

### CSV parsing resilience

- **Column name matching is case-insensitive AND ignores spaces/underscores.** `Total Area`, `TotalArea`, `total_area`, `SumArea` all map to `peak_area`.
- Each metric has multiple aliases. Build the alias map from the Rust source verbatim.
- **Unrecognized columns are passed through as `extra_metrics`** (dict in payload). Don't drop them silently.
- Non-numeric values in `extra_metrics` are dropped (logged at debug level).
- If `rt_delta` column is absent but both `retention_time` and `rt_expected` are present, compute `rt_delta = observed - expected`.

### Hashing

- Template hash (SHA-256) is computed over file contents for `.sky` files.
- For `.d` directories (Bruker, Agilent), hash is computed over **sorted (filename, size) pairs**, not file contents. This is fast and stable but not collision-free. Don't "improve" it without coordinating with the cloud team — the hash is part of the payload schema.

### Temp file cleanup

- Report CSV is written to spool work dir, parsed, then deleted.
- If deletion fails (file lock, permissions), log a warning but report success — the metrics are already in memory.

---

## Spool (`mdqc.spool`)

### Atomic writes

- Always write to `<dir>/.<filename>.tmp` first, then `os.replace()` to final path. **`os.rename()` is not guaranteed atomic on Windows when the destination exists** — `os.replace()` is. Use `os.replace()`.
- Crash recovery: on startup, scan `pending/` for `.tmp` files and delete them (incomplete writes).

### Correlation ID format

```
{agent_id}-{YYYYMMDDhhmmss}-{8 hex random}
```

- Timestamp is **local time**, formatted without a timezone marker. (This is a Rust quirk, but match it for parity.) DST transitions can produce duplicate IDs at the second granularity — the random suffix prevents collisions.
- `agent_id` is from config; resolve `"auto"` to a stable hardware-derived ID at first run and persist to config.

### State directory transitions

```
pending/   →  uploading/   →  completed/   (success)
                          ↘   failed/      (after retries exhausted)
```

- All transitions are filesystem renames. Never copy + delete (not atomic).
- On startup, **move everything in `uploading/` back to `pending/`** (crash recovery).
- Recovery does NOT touch `failed/` — those are intentionally there.

### Pruning policies

| Policy | Default | Behaviour |
|---|---|---|
| `max_pending_mb` | **1000** | Reject new payloads with `SpoolError::Full` if pending dir exceeds this size |
| `max_age_days` | **30** | Delete payloads older than this from `pending/` and `failed/` |
| `completed_retention_count` | **10** | Keep only the N most recent (by mtime) in `completed/` |

- Pruning runs on a schedule, not per-write. Once per hour is fine.
- The size check is a guardrail against runaway disk usage if cloud is down for a long time. Hitting it is a CRITICAL event — emit a Windows toast.

### Manifest

- `spool/manifest.json` describes template name, instrument ID, target list, and known/extra metrics. Used by the dashboard to discover available metrics.
- **Best-effort write.** Failure to write the manifest must NOT fail the enqueue.

---

## Uploader (`mdqc.uploader`)

### Retry schedule (CRITICAL — do not change)

5 attempts total. **4 inter-retry sleeps with jitter** (the first attempt is the initial call — there is no sleep before it):

| Sleep | Between attempts | Target | Jitter range |
|---|---|---|---|
| 1 | after attempt 1 fails, before attempt 2 | 30s | 20–40s (±10s) |
| 2 | after attempt 2 fails, before attempt 3 | 2 min | 90–150s (±30s) |
| 3 | after attempt 3 fails, before attempt 4 | 10 min | 480–720s (±2 min) |
| 4 | after attempt 4 fails, before attempt 5 | 1 h | 3000–4200s (±10 min) |

**Use `tenacity.wait_chain` with exactly 4 entries**, paired with `stop_after_attempt(5)`. Do NOT include a leading `(0, 0)` sleep — Tenacity uses `wait_chain[i]` as the wait between attempts `i` and `i+1`, not before attempt `i`. Including a `(0,0)` shifts every subsequent delay by one position and changes the schedule to (0s, 30s, 2m, 10m) with no 1-hour wait at all. This is the most likely bug in this module; see the timing test in Phase 3 acceptance.

**Do NOT use `wait_exponential`.** The schedule is not exponential — attempt 5 is much further out than exponential would predict. Customers and cloud-side dashboards expect this exact cadence.

```python
# Correct
from tenacity import retry, stop_after_attempt, wait_chain, wait_random
@retry(
    stop=stop_after_attempt(5),
    wait=wait_chain(
        wait_random(20, 40),     # before attempt 2
        wait_random(90, 150),    # before attempt 3
        wait_random(480, 720),   # before attempt 4
        wait_random(3000, 4200), # before attempt 5
    ),
    retry=retry_if_exception_type(TransientUploadError),
)
async def upload(payload): ...
```

### Error classification

| HTTP status | Treatment |
|---|---|
| 200 / 201 / 202 / 204 | Success → move to `completed/` |
| 401 / 403 | **Permanent** — no retry, move to `failed/`, alert |
| 408 / 429 / 5xx | Transient → retry per schedule |
| Connection error / timeout | Transient → retry per schedule |
| Other 4xx (400, 404, 422) | Permanent → no retry, move to `failed/` |

After 5 attempts exhausted: move to `failed/`, raise `UploadError::RetryExhausted(5)`, emit Windows event log entry + toast.

### Authentication

- **v1: Bearer token only.** `Authorization: Bearer <token>` header from `[cloud] api_token`.
- v1.1: mTLS via Windows cert store. See § Future for the corrected PFX-load approach.

### Auth-config decision matrix (CRITICAL)

The Rust agent supports both `api_token` and `certificate_thumbprint`. The Python port supports only `api_token` in v1. **Do not silently fall through** when a customer has cert auth configured — every cert-configured site would break invisibly.

| `api_token` | `certificate_thumbprint` | Behaviour |
|---|---|---|
| set | unset | Bearer auth — normal upload path |
| set | set | Bearer auth wins; log a warning that thumbprint is ignored in v1 |
| unset | set | **FAIL FAST at startup.** Exit code 78 (config error). Message must include: "mTLS via certificate_thumbprint is not yet implemented in the Python agent. Either set [cloud] api_token, or pin to the Rust agent until v1.1." |
| unset | unset | Local-only mode (see below) |

The fail-fast check runs in `mdqc.config.validate()` and is also surfaced by `mdqc doctor`. **Do not** check this only in the uploader — that produces a running-but-broken agent.

### Local-only mode

When neither `api_token` nor `certificate_thumbprint` is configured:
- The agent still spools and processes.
- Pending payloads are moved straight to `completed/` without an upload attempt.
- Log a single startup message: `Running in local-only mode (no cloud auth configured). Payloads will be retained locally only.`
- `mdqc doctor` reports this as a WARNING, not OK.
- This is intentional behaviour for air-gapped sites; it must be explicit (no auth configured), not accidental.

### HTTP client config

- Global timeout: **30s** (per request, including connect)
- Connect timeout: **10s**
- Proxy: respect `[cloud] proxy` config; default to system proxy via `httpx.Client(trust_env=True)`
- TLS: TLS 1.2+ minimum (httpx default is fine on modern Python)

### Idempotency

- The cloud deduplicates by `payload.run_id` (UUID v4 generated at extraction time).
- **Safe to retry the same payload bytes.** Do not regenerate the run_id on retry.

---

## Notifications (`mdqc.notifications`)

### Process placement (CRITICAL)

**Toasts must be raised inside the tray process, never the service.** Services run in Session 0 and toasts raised there are silently dropped. The service emits notification *intent* via the `/events` SSE stream; the tray subscribes and decides whether/how to surface the toast. See § Tray for the subscription model and batching rules.

If you find yourself importing `mdqc.notifications` inside `mdqc.watcher`, `mdqc.uploader`, or anything else service-side, **stop**. The service should publish an event; the tray decides on the toast.

### App User Model ID (AUMID)

- **Must be exactly `MassDynamics.QCAgent`.** This must match the AUMID set on the Start Menu shortcut by the installer. If it doesn't match, toasts either appear unbranded or don't appear at all.
- The shortcut is what registers the AUMID with Windows. **Without a shortcut, no toasts.** The installer must create one even for users who don't pin it.
- Test toast delivery in CI by checking `winsdk.windows.ui.notifications.NotificationSetting`.

### Toast policy

- Duration: always "Short" (~5s)
- **Silent** (no sound): file detected, processing started, upload queued
- **With sound**: extraction success, upload success, errors, update available
- No built-in throttling — caller (the agent core) is responsible. If a customer has 100 files queued, do not send 100 toasts. Batch with a 30s window.

### Non-Windows fallback

- `notify_*` functions are no-ops on non-Windows (log at info instead). Don't pull in `plyer` or other cross-platform libs — adds weight, customer doesn't run this on Linux.

---

## Crash reporting (`mdqc.crash`)

- **Install `sys.excepthook` AND `faulthandler.enable()` early** — before any imports that might crash. The order matters: install excepthook first so that import-time exceptions are captured.
- `faulthandler` covers C-extension SIGSEGVs (e.g. inside Pillow, lxml). `sys.excepthook` covers normal Python exceptions.
- Crash report path: `%PROGRAMDATA%\MassDynamics\QC\crashes\crash_<ISO8601>.txt`
- Show MessageBoxW via `ctypes.windll.user32.MessageBoxW` with the path to the crash report and a "Report on GitHub" button that opens a pre-filled issue URL.
- **Crash report body in the GitHub URL is truncated to 1500 chars** (URL length constraints). Full report stays on disk.
- **Use `urllib.parse.quote()` for URL encoding**, not the Rust code's hand-rolled encoder. The Rust version has bugs with high-bit chars; don't reproduce them.

---

## Failed files (`mdqc.failed_files`)

- Max history: **100 entries**, FIFO eviction by `failed_at` timestamp.
- Persisted to `%PROGRAMDATA%\MassDynamics\QC\failed_files.json` on every mutation.
- **Write failures are silently swallowed** (logged as warn, not error). This is intentional — if disk is full, we don't want to crash the agent on top of that.
- `increment_retry()` bumps a counter; eviction is still by timestamp, not retry count.
- Retry semantics: `mdqc failed retry <path>` re-enqueues to the watcher; `mdqc failed retry all` re-enqueues everything; `mdqc failed clear` empties the store.

---

## Config (`mdqc.config`)

### Validation rules
- Every instrument must have non-empty `id`, `watch_path`, `template`.
- `vendor` must be one of `thermo`, `bruker`, `sciex`, `waters`, `agilent`.
- `watch_path` is **not** validated for existence at config load — instruments may be configured before paths are mounted.
- `template` is **not** validated at load — checked at extraction time.
- **Bad config = fail to start**, no fallback. Print human-readable error pointing at the offending line.

### `agent_id = "auto"` resolution

- At first run, derive a hardware ID (e.g. SHA-256 of `wmic csproduct get UUID` output, truncated to 16 hex chars).
- **Persist back to `config.toml` as a real UUID** so subsequent runs are stable.
- Do NOT regenerate on every run.

### Defaults — keep all magic numbers in `mdqc.config.defaults`

```python
# mdqc/config/defaults.py
SKYLINE_TIMEOUT_S = 900
SCAN_INTERVAL_S = 30
STABILITY_WINDOW_S = 60
STABILIZATION_TIMEOUT_S = 600
PROCESSING_TIMEOUT_S = 1800
BRUKER_STABILITY_WINDOW_S = 90
MAX_PENDING_MB = 1000
MAX_AGE_DAYS = 30
COMPLETED_RETENTION_COUNT = 10
PROCESSED_REGISTRY_MAX = 10_000
FAILED_FILES_MAX = 100
ACTIVITY_LOG_MAX = 50
UPLOAD_TOTAL_ATTEMPTS = 5
# 4 inter-retry sleep ranges (min_seconds, max_seconds), one per gap between attempts.
# Index i is the sleep BEFORE attempt i+2 (i.e., after attempt i+1 fails).
# DO NOT prepend a (0, 0) — see AGENT_NOTES § Uploader for the Tenacity off-by-one trap.
UPLOAD_RETRY_SLEEPS = [
    (20, 40),       # before attempt 2: 30s ± 10s
    (90, 150),      # before attempt 3: 2m ± 30s
    (480, 720),     # before attempt 4: 10m ± 2m
    (3000, 4200),   # before attempt 5: 1h ± 10m
]
HTTP_TIMEOUT_S = 30
HTTP_CONNECT_TIMEOUT_S = 10
DEFAULT_ENDPOINT = "https://dev.massdynamics.com/api/evosep_qcs"
AUMID = "MassDynamics.QCAgent"
```

Do not scatter these constants across modules.

---

## Service & lifecycle (`mdqc.service`)

- NSSM sends `SIGTERM` (Windows: `CTRL_BREAK_EVENT` translated by NSSM) on stop. Catch with `signal.signal(signal.SIGTERM, handler)` and `signal.signal(signal.SIGINT, handler)` for foreground/CTRL+C.
- Graceful shutdown: cancel watcher, drain in-flight extractions (with 5min grace), flush spool writes, close HTTP client. **30s hard timeout total.**
- **Async signal handling caveat:** `signal.signal` handlers don't run inside a coroutine. Use `loop.add_signal_handler` on POSIX, fall back to `signal.signal` calling `loop.call_soon_threadsafe(stop_event.set)` on Windows.
- **NSSM recovery config** (set in installer): `AppExit Default Restart`, `AppRestartDelay 5000`, `AppThrottle 30000`. After 3 restart failures within 30s, NSSM gives up — same behaviour as the Rust service config.

---

## Tray (`mdqc.cli.tray`)

The tray is a **separate process** from the service. See `PLAN.md § 2.5 Process model` for the architecture and IPC contract. This section covers the tray process internals.

### Why a separate process

Windows services run in Session 0. Session 0 has no GUI access:
- Tray icons placed by Session 0 do not appear in the user's notification area.
- Toast notifications raised from Session 0 are silently dropped (`NotificationSetting.DisabledByGroupPolicy` or similar).
- `MessageBoxW` from Session 0 either pops up in an invisible session or is ignored.

Running `pystray` "inside the service" only works with `Type SERVICE_INTERACTIVE_PROCESS`, which Microsoft has effectively deprecated and which IT admins increasingly block. **Do not go down this path.** The user-session process is the only correct answer.

### IPC client

- The tray reads `%PROGRAMDATA%\MassDynamics\QC\runtime.json` to find the service's port and token.
- Poll for the file at startup with a 30s timeout (service might still be starting). If it never appears, show a tray icon in "service unavailable" state.
- Watch the file for changes (mtime poll every 5s) — the service rotates its token on every restart.
- All HTTP requests carry `X-MDQC-Token: <token>`. On 401, re-read `runtime.json` and retry once.

### Event subscription (toasts)

- The tray subscribes to `GET /events` (Server-Sent Events) over the IPC connection.
- Reconnect with exponential backoff (1s, 2s, 4s, … capped at 30s) if the stream drops.
- **Toast batching:** if more than 3 events arrive within a 30s window, collapse them into a single summary toast ("5 files processed" instead of 5 separate toasts). This protects the user when a backlog drains.
- Severity → sound mapping is in this process, not the service.

### `pystray` mechanics

- **`pystray` runs its event loop in the calling thread.** Run it in the main thread of the tray process; run the SSE subscriber in a daemon thread; bridge between them with `queue.Queue` and `pystray.Icon.notify()` doesn't exist — toasts are delivered via `winsdk` directly.
- **Tray menu items must not block.** "Open Wizard", "Open Dashboard" → spawn `webbrowser.open()` in a thread.
- Menu items that mutate state ("Pause", "Resume", "Retry failed") → `httpx` POST to `/api/...`, fire-and-forget, then refresh icon state from `/api/status`.
- Icon path: PyInstaller bundles `assets/icon.png`; resolve via `sys._MEIPASS` when frozen, project path when not.

### Browser launch

- "Open Wizard" / "Open Dashboard" tray items: build URL as `http://127.0.0.1:<port>/wizard?token=<token>`, then `webbrowser.open(url)`.
- The wizard's first page reads the token from query string, sets a session cookie, then `history.replaceState` to drop the token from the URL bar.
- Without this, the URL with token sits in browser history — a low-impact but real leak.

---

## Web UI (`mdqc.webui`)

- Bind to **`127.0.0.1` only**, never `0.0.0.0`. This is local-only by design.
- Use an ephemeral port (`port=0`), then write the actual port + a single-use auth token to a file the tray reads when launching the browser. **Do not skip the token** — other localhost processes can hit it otherwise.
- HTMX is sufficient for all interactivity. Do not introduce a JS build step (npm, vite) — PyInstaller doesn't bundle node and we don't want a second toolchain.
- Long-running endpoints (file upload, diagnostics) must use SSE or background tasks — uvicorn workers are limited.
- **The web UI is NOT the dashboard.** The Streamlit dashboard at `../streamlit-qc/` stays separate. Web UI = wizard + status + diagnostics + log viewer + failed-files manager. Dashboard = trend charts.

---

## Packaging (`scripts/build.py`, `installer/`)

### PyInstaller hidden imports

These are NOT auto-detected and must be in the spec:
- `winsdk.windows.ui.notifications`
- `winsdk.windows.data.xml.dom`
- `pystray._win32`
- `watchdog.observers.winapi`
- `watchdog.observers.read_directory_changes`
- `httpx._transports.default`
- Any `pydantic` v2 plugin

### Icon and asset bundling
- `assets/icon.png` — needed by tray
- `assets/QC_Method.sky` — bundled default Skyline template
- `assets/MD_QC_Report.skyr` — bundled report definition

These must be in the PyInstaller `datas` list AND copied to `%PROGRAMDATA%\MassDynamics\QC\methods\` on first run by `mdqc.config.paths.ensure_bundled_assets()` (mirrors Rust behaviour).

### NSSM commands in installer

```
nssm install MassDynamicsQC "C:\Program Files\MassDynamics\QC\mdqc.exe" run --service-mode
nssm set MassDynamicsQC AppDirectory "C:\Program Files\MassDynamics\QC"
nssm set MassDynamicsQC DisplayName "Mass Dynamics QC Agent"
nssm set MassDynamicsQC Start SERVICE_DELAYED_AUTO_START
nssm set MassDynamicsQC AppExit Default Restart
nssm set MassDynamicsQC AppRestartDelay 5000
nssm set MassDynamicsQC AppThrottle 30000
nssm set MassDynamicsQC AppStdout "C:\ProgramData\MassDynamics\QC\logs\service-stdout.log"
nssm set MassDynamicsQC AppStderr "C:\ProgramData\MassDynamics\QC\logs\service-stderr.log"
nssm start MassDynamicsQC
```

---

## Future work (deferred from v1)

### mTLS via Windows cert store

The Rust approach: PowerShell `-EncodedCommand` (base64 UTF-16LE) calling `Export-PfxCertificate` with a random 32-char password to a temp PFX, then loaded into `reqwest::Identity`, then temp file deleted.

**Important:** Python's stdlib `ssl.SSLContext.load_cert_chain()` does **NOT** accept PKCS#12 / PFX files — it requires PEM. The PFX must be split into a cert PEM and a key PEM first, using the `cryptography` package.

Recommended Python implementation:
```python
import secrets, subprocess, tempfile, ssl, base64, os
from pathlib import Path
from cryptography.hazmat.primitives.serialization import (
    pkcs12, Encoding, PrivateFormat, NoEncryption, BestAvailableEncryption
)

def build_mtls_context(thumbprint: str, certs_dir: Path) -> ssl.SSLContext:
    thumbprint = thumbprint.replace(" ", "").upper()
    if len(thumbprint) != 40 or not all(c in "0123456789ABCDEF" for c in thumbprint):
        raise ValueError("certificate_thumbprint must be 40 hex chars")

    certs_dir.mkdir(parents=True, exist_ok=True)
    # Service-account-only ACL is set at install time; do not chmod here.

    pfx_path = certs_dir / f"{thumbprint}.pfx"
    pfx_password = secrets.token_urlsafe(24)

    ps_script = (
        f"$cert = Get-Item Cert:\\LocalMachine\\My\\{thumbprint};"
        f"$pwd = ConvertTo-SecureString -String '{pfx_password}' -Force -AsPlainText;"
        f"Export-PfxCertificate -Cert $cert -FilePath '{pfx_path}' -Password $pwd | Out-Null"
    )
    encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode()
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"PFX export failed: {result.stderr.strip()}")

    try:
        # PFX → in-memory key + cert + chain
        pfx_bytes = pfx_path.read_bytes()
        private_key, certificate, additional_certs = pkcs12.load_key_and_certificates(
            pfx_bytes, pfx_password.encode()
        )
        if private_key is None or certificate is None:
            raise RuntimeError("PFX missing private key or certificate")

        # Write PEMs into the same private dir; SSLContext needs file paths, not bytes.
        cert_pem_path = certs_dir / f"{thumbprint}.cert.pem"
        key_pem_path = certs_dir / f"{thumbprint}.key.pem"
        cert_chain = certificate.public_bytes(Encoding.PEM)
        for ca in additional_certs or ():
            cert_chain += ca.public_bytes(Encoding.PEM)
        cert_pem_path.write_bytes(cert_chain)
        key_pem_path.write_bytes(
            private_key.private_bytes(
                encoding=Encoding.PEM,
                format=PrivateFormat.PKCS8,
                encryption_algorithm=NoEncryption(),
            )
        )

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=str(cert_pem_path), keyfile=str(key_pem_path))
        return ctx
    finally:
        # Always delete the PFX; PEMs stay on disk in the locked-down dir for reload.
        try:
            pfx_path.unlink()
        except FileNotFoundError:
            pass
```

Then pass to httpx: `httpx.Client(verify=ctx)`.

Gotchas:
- **Thumbprint validation:** must be 40 hex chars, no spaces, case-insensitive. Strip + upper before use.
- **PFX password is never persisted.** It's a per-export secret, lives only in the local variable.
- **Non-exportable keys:** the Rust agent silently re-exports because the cert is marked exportable at issuance. If a customer has a CA that issues non-exportable keys, this PowerShell call fails. Document this in `mdqc doctor`'s cert check.
- **Certs directory ACL:** must be set by the installer, not at runtime. Service account read-only; everyone else denied. Don't try to set ACLs from Python — `pywin32` security descriptors are subtle and easy to get wrong.
- **Key file persists on disk.** Unlike the Rust version (which keeps the PFX in memory), this approach leaves a PEM key on disk. The directory ACL is the protection. If that's unacceptable, alternatives:
  - **`win32` SChannel directly:** keep the cert handle in the Windows cert store and never export. Requires `pywin32` SChannel APIs and a custom httpx transport. ~2 weeks of work and effectively unmaintained territory.
  - **Encrypted key file:** use `BestAvailableEncryption(per_run_password)` and pass `password=` to `load_cert_chain`. Same risk profile as PFX — password lives in process memory.
  - Recommend deferring the no-disk-key approach to a v1.2 hardening pass and shipping the locked-down PEM approach in v1.1.
- **Certificate rotation:** delete the PEM files when thumbprint changes in config; re-export on next upload. Cheap. Don't try to "watch" the cert store.

### Native pywin32 service (alternative to NSSM)

Only if Windows IT prohibits NSSM. Rough sketch:
```python
import win32serviceutil, win32service, servicemanager
class MDQCService(win32serviceutil.ServiceFramework):
    _svc_name_ = "MassDynamicsQC"
    _svc_display_name_ = "Mass Dynamics QC Agent"
    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        # signal asyncio loop to stop
    def SvcDoRun(self):
        # bootstrap asyncio, run agent
```

Costs ~1 week to implement and test. Service installation needs `pywin32_postinstall`. Recovery actions still need to be set via `sc.exe failure` from the installer.

---

## Things that LOOK like bugs but aren't

- **Watcher logs "File detected" twice for the same file.** Normal — once from the WinAPI event, once from the periodic scan fallback. The processed-files registry deduplicates downstream. Don't add extra logging suppression.
- **Spool `pending/` count fluctuates rapidly.** Files move through pending → uploading → completed in milliseconds when the cloud is healthy. The dashboard's status display will jitter; that's fine.
- **`mdqc doctor` says "Skyline version: unknown" but extraction works.** Skyline writes its version banner to stdout *or* stderr depending on the version; the parser is liberal but occasionally misses. Not a real failure.
- **Toast appears "from" `python.exe` instead of "Mass Dynamics QC".** Means the AUMID isn't registered — the Start Menu shortcut is missing or has the wrong AUMID. Re-run installer.
- **Bruker `.d` folder takes ~3 minutes to start processing after the instrument finishes.** Expected — 60s for the lock files to clear + 90s stability window.

---

## Things that ARE bugs (open issues to fix during port)

These are bugs in the Rust implementation that should NOT be reproduced in Python:

- `crash.rs` URL encoding is hand-rolled and can produce invalid GitHub URLs for crash reports with high-bit characters. **Use `urllib.parse.quote()` in Python.**
- `failed_files.rs` eviction by timestamp can be non-deterministic when timestamps collide on fast systems. **Use a monotonic insertion counter as a tiebreaker.**
- `update_checker.rs` doesn't honor the `If-Modified-Since` header, so it occasionally hits GitHub rate limits in heavily-restarted environments. **Send `If-Modified-Since` in the Python version.**
- `correlation_id` uses local time without TZ marker, which can produce duplicate-looking IDs across DST transitions. **Use UTC ISO8601 in Python and document the format change** — the random suffix already prevents real collisions, so this is a readability fix only.

If anything else looks wrong, **flag it before "fixing"** — it might be intentional scar tissue.

---

## When in doubt

1. Read the Rust source for the same module.
2. Read the relevant section of `../SPEC.md`.
3. If still unclear, search this file for the module name.
4. If still unclear, ask before changing behaviour. Default to matching Rust.

---

## Review log

Findings raised against the original plan, what changed, and where. Future agents should append rather than rewrite.

### 2026-04-26 — initial review (REVIEW_FINDINGS.md)

| # | Finding | Resolution |
|---|---|---|
| 1 | Token-only v1 silently breaks cert-configured customer sites | Added Auth-config decision matrix in § Uploader. Cert-configured-without-token now exits with code 78. PLAN open question raised: "how many sites use cert auth today" — answer determines whether v1.1 mTLS becomes a v1 blocker. |
| 2 | Tray-in-service is not a viable path; needs separate process + IPC | Added PLAN § 2.5 "Process model" with diagram, two-process responsibilities, IPC contract (loopback HTTP + token via `runtime.json`, SSE for events). Rewrote § Tray to make this first-class. Added `mdqc tray` CLI command and `mdqc.ipc` package to project layout. Phase 4 split out into IPC + tray + notifications. |
| 3 | Tenacity `wait_chain` is sleep-after-failure, not delay-before-attempt — original 5-entry table caused off-by-one | Rewrote § Uploader retry table as 4 inter-retry sleeps. Replaced `UPLOAD_RETRY_SCHEDULE` constant with `UPLOAD_TOTAL_ATTEMPTS=5` + `UPLOAD_RETRY_SLEEPS=[4 entries]`. Added Phase 3 acceptance test using `freezegun` to record sleep durations across attempts. |
| 4 | `ssl.SSLContext.load_cert_chain()` does not accept PFX | Replaced § Future mTLS snippet with a working `cryptography.hazmat.primitives.serialization.pkcs12` based version. Documented the PEM-on-disk tradeoff and the SChannel alternative. Added `cryptography` to main dependencies (with sys_platform marker) so the package is in place when v1.1 lands. |
| 5 | Windows extras can be missed by build scripts and ship a broken exe | Moved `pywin32`, `winsdk`, `cryptography` from `[windows]` extra into main `dependencies` with `sys_platform == 'win32'` markers. Removed the `windows` extra entirely. Added a `mdqc selfcheck` packaging smoke test as a Phase 6 CI gate. |
