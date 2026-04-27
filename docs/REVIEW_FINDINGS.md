# Python Port Plan Review Findings

This file captures the five review findings raised against the Python port plan, with recommended fixes before implementation starts.

## 1. Token-only v1 breaks cert deployments

**Location:** `python-port/PLAN.md:21`
**Priority:** P1

The plan says v1 will use bearer tokens only, but the current Rust config schema and uploader already support `certificate_thumbprint` mTLS. Any customer or site configured with certificate-only auth would silently fall into local-only/no-upload behavior in the Python port unless this is handled explicitly.

**Recommended fix:**

Keep `certificate_thumbprint` as a supported v1 config path, or add a required pre-migration step that converts every cert-only deployment to bearer-token auth before installing the Python agent. If mTLS is deferred, make startup fail loudly when `certificate_thumbprint` is configured without `api_token`; do not enter local-only mode.

## 2. Tray/service split is underspecified

**Location:** `python-port/PLAN.md:189`
**Priority:** P1

The plan needs a first-class separate tray process plus IPC to the headless service. A Windows service runs outside the interactive user session, so `pystray` inside the NSSM-wrapped service is not a reliable UI path. The existing installer starts `mdqc tray` from the user's `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` key, and `AGENT_NOTES.md` already recommends the split.

**Recommended fix:**

Define two processes in the architecture:

- `mdqc run --service-mode`: headless NSSM-managed service for watching, extraction, spooling, and upload.
- `mdqc tray`: per-user session process launched at login, responsible for tray menu, browser launch, notifications, and user-facing commands.

Add an IPC contract between them, such as a localhost FastAPI control endpoint bound to `127.0.0.1` with a local token, or named pipes on Windows. The service should not own tray UI.

## 3. Tenacity waits are shifted

**Location:** `python-port/AGENT_NOTES.md:310`
**Priority:** P1

The retry schedule is written as attempt delays, but Tenacity waits are sleeps after a failed attempt before the next call. If an implementation feeds all five entries to `wait_chain`, retry 2 will happen immediately and every later delay will be off by one.

**Recommended fix:**

Represent the policy as 5 total attempts with 4 inter-retry sleeps:

- after attempt 1 fails: 30s +/- 10s
- after attempt 2 fails: 2m +/- 30s
- after attempt 3 fails: 10m +/- 2m
- after attempt 4 fails: 1h +/- 10m

Use a custom Tenacity wait function keyed to the next attempt number, or build a `wait_chain` containing only the four inter-retry waits. Add a unit test that records call timestamps for all 5 attempts.

## 4. PFX loading snippet will not work

**Location:** `python-port/AGENT_NOTES.md:410`
**Priority:** P2

The deferred mTLS recipe shows `ssl.SSLContext.load_cert_chain(pfx_path, password=password)`, but `load_cert_chain()` expects PEM cert/key files, not PKCS#12/PFX files.

**Recommended fix:**

Replace the snippet with a tested implementation path:

- export the certificate to PFX using PowerShell or `certutil`;
- convert PFX to PEM certificate and private key using `cryptography`;
- load the PEM files with `SSLContext.load_cert_chain()`;
- store temporary files in a service-account-readable private directory;
- securely delete temporary material after the HTTP client is built.

Alternatively, choose a Windows-native client certificate integration approach and document it before v1.1 work starts.

## 5. Windows extras can be missed

**Location:** `python-port/pyproject.toml:55`
**Priority:** P2

The Windows packages are optional extras, but the planned Windows app requires `pywin32` for exclusive file-open checks and `winsdk` for notifications. A normal install or PyInstaller build can produce an executable missing required Windows modules if the build scripts do not explicitly install the Windows extras.

**Recommended fix:**

Make the Windows build path install `.[windows,build]` explicitly and document that in `README.md` and packaging scripts. Consider moving `pywin32` into mandatory dependencies if exclusive-open behavior is required for every Windows deployment. Add a packaging smoke test that imports `win32file`, `winsdk.windows.ui.notifications`, `pystray._win32`, and `watchdog.observers.winapi` from the frozen app environment.
