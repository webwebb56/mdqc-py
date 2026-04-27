# MD QC Agent (Python)

**Automated quality control monitoring for mass spectrometry instruments — Python implementation.**

This is a Python port of the Rust [`mdqc-agent`](https://github.com/MassDynamics/MD-EVOSEP-system-suitability-control). It implements the same data path (watch → finalize → classify → extract → spool → upload), reads the same `config.toml`, and produces payloads compatible with the same MD cloud ingest endpoint and Streamlit dashboard.

## Status

- **Phase 0–6 complete** (foundations, modules, IPC, web UI, packaging, installer, CI).
- **321 tests passing** (`pytest -q`), 3 skipped (`windows_only` markers on macOS/Linux).
- **Status: alpha.** End-to-end smoke test (watcher → finalizer → spool → uploader-in-local-only-mode) passes on macOS/Linux with a `FakeExtractor`. Real-Skyline run on a Windows VM is the next acceptance gate; see `docs/PLAN.md § Phase 6` for the remaining acceptance criteria.

## Why a Python port

- Aligns the agent's tech stack with the rest of MD's Python-first codebase (Streamlit dashboard, MCP server, analysis tooling).
- Lower contribution barrier — anyone on the team can read and extend it.
- Shared payload schema with the Rust agent allows side-by-side rollout during migration.

## Architecture at a glance

Two cooperating processes:

- **`mdqc.exe run --service-mode`** — headless background service, NSSM-managed. Runs the watcher, extractor, spool, uploader, and a localhost FastAPI for the wizard/dashboard.
- **`mdqc.exe tray`** — per-user UI process started at login. Tray icon, browser launcher, toast notifications. Talks to the service over loopback HTTP with a token written to `runtime.json`.

See `docs/PLAN.md § 2.5 Process model` for the full picture.

## Quick start (developer)

```bash
git clone https://github.com/MassDynamics/mdqc-py
cd mdqc-py
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run tests (321 should pass, 3 windows_only skips on macOS/Linux).
pytest -q

# Lint
ruff check src tests

# Smoke-test the bundle wiring (no Skyline required).
pytest tests/integration -v

# Verify all runtime modules import cleanly.
mdqc selfcheck

# Run in foreground (dev mode: service + tray work in one process).
mdqc run --foreground
```

## Building the Windows installer

```bash
# One-time: install build extras.
pip install -e ".[dev,build]"

# Build the PyInstaller binary into dist/mdqc(.exe). Verifies via mdqc selfcheck.
python scripts/build.py --clean

# Build the Inno Setup installer into dist/installer/
# (Windows only; on macOS/Linux this prints an informational message and exits.)
python scripts/package.py
```

See `installer/README.md` for the NSSM dependency and the release flow.

## Documentation

| File | Purpose |
|---|---|
| [`docs/PLAN.md`](./docs/PLAN.md) | Architecture, project layout, phased build plan, tech stack, packaging strategy |
| [`docs/AGENT_NOTES.md`](./docs/AGENT_NOTES.md) | **Read before touching any module.** Gotchas, magic numbers, scar tissue from the Rust implementation. |
| [`docs/REVIEW_FINDINGS.md`](./docs/REVIEW_FINDINGS.md) | Initial review of the plan and the fixes applied |

## Project layout

```
src/mdqc/
├── types.py              # Enums (ControlType, Vendor, FinalizationState…)
├── config/               # Pydantic schema + paths + defaults (every magic number)
├── log.py                # structlog setup
├── classifier.py         # Filename → ControlType
├── metrics.py            # Per-target + run-level metric computation
├── baseline.py           # Baseline cache and comparison
├── spool/                # Durable on-disk queue (atomic state transitions)
├── watcher/              # File detection + finalization state machine
├── extractor/            # Skyline subprocess + CSV report parsing
├── uploader.py           # httpx + tenacity (4 inter-retry sleeps, see AGENT_NOTES)
├── failed_files.py       # Persistent failed-files store
├── activity_log.py       # Recent activity log
├── notifications.py      # winsdk toasts (tray process only)
├── crash.py              # excepthook + faulthandler + crash dialog
├── update_checker.py     # GitHub releases poll
├── ipc/                  # runtime.json + httpx client (tray↔service)
├── service/              # asyncio lifecycle + signal handling
├── webui/                # FastAPI app + HTMX templates
└── cli/                  # typer subcommands
```

## License

Apache 2.0 — see [LICENSE](./LICENSE).
