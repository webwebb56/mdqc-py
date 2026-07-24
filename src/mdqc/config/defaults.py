"""All magic numbers in one place. Sourced from docs/AGENT_NOTES.md.

Do not scatter these across modules. If you need a new constant, add it here
and document where it came from.
"""

from __future__ import annotations

# ─── Watcher ────────────────────────────────────────────────────────────────
SCAN_INTERVAL_S = 30
"""How often the polling fallback scans the watch directory."""

STABILITY_WINDOW_S = 60
"""File must show no size/mtime change for this long before promoting STABILIZING → READY."""

STABILIZATION_TIMEOUT_S = 600
"""Hard upper bound for stabilization. After 10 minutes, mark FAILED."""

PROCESSING_TIMEOUT_S = 1800
"""Hard upper bound for PROCESSING state. 30 minutes."""

BRUKER_STABILITY_WINDOW_S = 90
"""Bruker .d folders need longer — timsdata.dll keeps file handles open."""

PROCESSED_REGISTRY_MAX = 10_000
"""FIFO eviction threshold for processed-files registry."""

# ─── Extractor ──────────────────────────────────────────────────────────────
SKYLINE_TIMEOUT_S = 900
"""SkylineCmd.exe extraction timeout. 15 minutes."""

# ─── Spool ──────────────────────────────────────────────────────────────────
MAX_PENDING_MB = 1000
"""Reject new payloads if pending dir exceeds this size."""

MAX_AGE_DAYS = 30
"""Delete payloads older than this from pending/ and failed/."""

COMPLETED_RETENTION_COUNT = 10
"""Keep only the N most recent (by mtime) in completed/."""

# ─── Uploader ───────────────────────────────────────────────────────────────
UPLOAD_TOTAL_ATTEMPTS = 5
"""Total upload attempts including the first."""

# 4 inter-retry sleep ranges (min_seconds, max_seconds), one per gap between
# attempts. Index i is the sleep BEFORE attempt i+2 (i.e., after attempt i+1
# fails). DO NOT prepend a (0, 0) — see docs/AGENT_NOTES § Uploader for the
# Tenacity off-by-one trap.
UPLOAD_RETRY_SLEEPS: list[tuple[int, int]] = [
    (20, 40),       # before attempt 2: 30s ± 10s
    (90, 150),      # before attempt 3: 2m ± 30s
    (480, 720),     # before attempt 4: 10m ± 2m
    (3000, 4200),   # before attempt 5: 1h ± 10m
]

HTTP_TIMEOUT_S = 30
HTTP_CONNECT_TIMEOUT_S = 10

# Named MD platform environments for POST /api/evosep_qcs. "dev" is
# live-verified (returned 201 in testing, 2026-07-24). "prod" follows the
# same host-swap pattern Giuseppe's platform uses elsewhere (app.massdynamics.com
# is the customer-facing app — see the Evosep QC screenshots) but has NOT been
# independently confirmed with a live request — verify before routing real
# customer data through it.
ENDPOINT_DEV = "https://dev.massdynamics.com/api/evosep_qcs"
ENDPOINT_PROD = "https://app.massdynamics.com/api/evosep_qcs"
DEFAULT_ENDPOINT = ENDPOINT_DEV

# ─── Failure tracking ───────────────────────────────────────────────────────
FAILED_FILES_MAX = 100
ACTIVITY_LOG_MAX = 50

# ─── Notifications ──────────────────────────────────────────────────────────
AUMID = "MassDynamics.QCAgent"
"""Must match the AUMID set on the installer-created Start Menu shortcut."""

NOTIFICATION_BATCH_WINDOW_S = 30
"""Batch notifications within this window into a single summary toast."""

# ─── IPC ────────────────────────────────────────────────────────────────────
RUNTIME_FILE_NAME = "runtime.json"
"""Service writes port + token here; tray reads it."""

IPC_TOKEN_BYTES = 32
"""secrets.token_urlsafe input length."""

IPC_HEADER = "X-MDQC-Token"

TRAY_RUNTIME_POLL_TIMEOUT_S = 30
"""Tray waits up to this long for runtime.json on startup."""

# ─── Update checker ─────────────────────────────────────────────────────────
UPDATE_CHECK_INTERVAL_S = 24 * 60 * 60
GITHUB_RELEASES_API = (
    "https://api.github.com/repos/webwebb56/mdqc-py/releases/latest"
)

# ─── Service shutdown ───────────────────────────────────────────────────────
SHUTDOWN_GRACE_S = 30
"""Hard timeout for graceful shutdown after SIGTERM."""

# ─── Schema ─────────────────────────────────────────────────────────────────
PAYLOAD_SCHEMA_VERSION = "1.1"
