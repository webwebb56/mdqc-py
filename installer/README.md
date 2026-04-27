# MD QC Agent (Python) — Windows installer

This directory holds the Inno Setup script that produces the Windows installer
for the Python port of the MD QC Agent. The installer wraps the PyInstaller
output from `dist/mdqc.exe`, the bundled Skyline assets, and a copy of `nssm.exe`
into a single `.exe` and registers `MassDynamicsQC` as a Windows service via
NSSM.

## Files

| Path | Purpose |
|---|---|
| `mdqc.iss` | The Inno Setup script. Compiled by `iscc.exe`. |
| `nssm.exe` | **Not checked in.** Drop the official `win64/nssm.exe` from the NSSM 2.24 release here before packaging. |
| `README.md` | This file. |

## NSSM dependency

The installer registers the agent as a Windows service via [NSSM](https://nssm.cc/),
the Non-Sucking Service Manager. NSSM is BSD-licensed and ships as a single
`.exe` — we do not vendor it because the upstream binary is signed.

`scripts/package.py` calls `_verify_nssm()` before invoking `iscc.exe`. The
preflight rejects the build if any of the following hold:

- `installer/nssm.exe` does not exist
- `installer/nssm.exe` is zero bytes (no more silent placeholder)
- `installer/nssm.sha256` exists and its first whitespace-delimited token does
  not match the SHA-256 of `installer/nssm.exe`

**To prepare for a release build (one-time, per machine):**

1. Download `https://nssm.cc/release/nssm-2.24.zip`.
2. Verify the SHA-256 against the value published on `nssm.cc` (or against the
   pinned value in `.github/workflows/ci.yml`).
3. Extract `nssm-2.24/win64/nssm.exe` and place it at `installer/nssm.exe`
   (alongside `mdqc.iss`).
4. Optionally write the file's SHA-256 to `installer/nssm.sha256` so the
   packaging preflight can detect tampering on subsequent builds:

   ```powershell
   (Get-FileHash installer\nssm.exe -Algorithm SHA256).Hash.ToLower() | Out-File installer\nssm.sha256 -Encoding ascii
   ```

CI provisions NSSM the same way: it downloads the zip from the pinned URL,
asserts the expected SHA-256, extracts `win64/nssm.exe`, then writes the
binary's SHA-256 to `installer/nssm.sha256` before invoking `package.py`. The
CI step fails fast if `EXPECTED_NSSM_SHA256` is left as `TODO` so a release
artifact cannot ship with an unverified NSSM.

## Inno Setup version

Inno Setup **6 or later** is required (the script uses `{commonappdata}` and
`ArchitecturesInstallIn64BitMode`, both 6.x).

Install from <https://jrsoftware.org/isinfo.php>. The default install location
on a 64-bit Windows host is `C:\Program Files (x86)\Inno Setup 6\`, which is
where `scripts/package.py` looks first.

## Build flow

### Dev build (Linux / macOS)

The PyInstaller binary cannot be built on non-Windows for a Windows target —
PyInstaller is not a cross-compiler. Use a Windows VM (or `windows-latest`
GitHub runner) for actual installer builds. On Linux/macOS, `scripts/package.py`
prints a friendly message and exits 0 so the rest of CI continues.

You can still run `python scripts/build.py` on macOS to produce a Unix
`mdqc` binary for local smoke tests; the installer step will simply skip.

### Release build (Windows)

From a Windows shell with Python 3.12 + the project venv activated:

```powershell
pip install -e ".[dev,build]"
python scripts/build.py --clean
python scripts/package.py
```

That produces:

- `dist/mdqc.exe`
- `dist/installer/mdqc-setup-py-v<version>.exe`

The installer is compiled with the version from `pyproject.toml`. To pin a
different version: `python scripts/package.py --version 0.2.0`.

### Manual rebuild from a Windows machine

If you want to skip `package.py` and run Inno Setup by hand:

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DAppVersion=0.1.0 installer\mdqc.iss
```

## Why NSSM (and not native pywin32 service)

The Rust agent registered itself as a service via the `windows-service` crate,
which calls `RegisterServiceCtrlHandlerExW` directly. The Python equivalent
would be `pywin32`'s `win32serviceutil.ServiceFramework`, plus
`pywin32_postinstall` for registration, plus `sc.exe failure` for recovery
actions.

We use NSSM instead because:

- **Fewer moving parts.** NSSM is a single signed binary that translates a
  service start request into a child-process exec of `mdqc.exe run --service-mode`.
  The Python agent does not need to know it is running as a service; it just
  handles `SIGTERM` (which NSSM sends on stop).
- **Recovery is config, not code.** `nssm set ... AppExit Default Restart`,
  `AppRestartDelay`, `AppThrottle` — all of this is set once in the installer
  and lives in the registry. With pywin32 we would invoke `sc.exe failure` and
  manage the same state via `subprocess`.
- **Service-mode UX matches a normal CLI.** `mdqc.exe run --service-mode` is the
  same code path as `mdqc.exe run --foreground` on a developer's macOS box,
  with only the logging destination differing. There is no service-mode-only
  bootstrap to debug.
- **NSSM is BSD-licensed and widely accepted.** Production Windows shops
  generally permit it. If a customer's IT review explicitly blocks third-party
  service wrappers, see `docs/AGENT_NOTES § Future § Native pywin32 service`
  for a fallback sketch.

If you ever need to switch off NSSM, the changes are confined to:

1. This `README.md` and `mdqc.iss` `[Run]` / `[UninstallRun]` sections.
2. A new pywin32 service entry point in `src/mdqc/service/` that invokes
   `mdqc.service.lifecycle.main_blocking(service_mode=True)`.

The agent's runtime behaviour does not change.
