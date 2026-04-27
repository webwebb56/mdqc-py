"""Durable on-disk spool for QC payloads.

See docs/AGENT_NOTES.md § Spool for atomicity rules and directory layout.
"""

from __future__ import annotations

from mdqc.spool.prune import prune_spool, recover_orphans
from mdqc.spool.store import Spool, SpoolError, SpoolFull

__all__ = [
    "Spool",
    "SpoolError",
    "SpoolFull",
    "prune_spool",
    "recover_orphans",
]
