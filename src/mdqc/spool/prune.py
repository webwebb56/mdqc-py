from __future__ import annotations

import logging
import time
from pathlib import Path

from mdqc.config import paths
from mdqc.config.defaults import (
    COMPLETED_RETENTION_COUNT,
    MAX_AGE_DAYS,
    MAX_PENDING_MB,
)

log = logging.getLogger(__name__)


def prune_spool(
    root: Path | None = None,
    *,
    max_pending_mb: int = MAX_PENDING_MB,
    max_age_days: int = MAX_AGE_DAYS,
    completed_retention: int = COMPLETED_RETENTION_COUNT,
    local_only: bool = False,
) -> dict[str, int]:
    """Bound spool disk usage.

    ``local_only`` selects the completed/ retention policy. When the agent has
    no cloud destination, completed/ holds the only copy of every payload, so
    pruning it down to a fixed count destroys data the operator cannot
    recover — completed/ is then bounded by age instead, exactly like
    pending/ and failed/. See COMPLETED_RETENTION_COUNT for the rationale.
    """
    spool_root = root if root is not None else paths.spool_dir()
    pending_dir = spool_root / "pending"
    failed_dir = spool_root / "failed"
    completed_dir = spool_root / "completed"

    cutoff = time.time() - max_age_days * 86400
    pending_aged = _remove_older_than(pending_dir, cutoff)
    failed_aged = _remove_older_than(failed_dir, cutoff)
    if local_only:
        completed_pruned = _remove_older_than(completed_dir, cutoff)
    else:
        completed_pruned = _retain_newest(completed_dir, completed_retention)

    if max_pending_mb > 0:
        size_mb = _dir_size_bytes(pending_dir) / (1024 * 1024)
        if size_mb >= max_pending_mb:
            log.critical(
                "Spool pending dir over size cap",
                extra={"size_mb": size_mb, "limit_mb": max_pending_mb},
            )

    return {
        "pending_aged_out": pending_aged,
        "failed_aged_out": failed_aged,
        "completed_pruned": completed_pruned,
    }


def recover_orphans(root: Path | None = None) -> int:
    spool_root = root if root is not None else paths.spool_dir()
    pending_dir = spool_root / "pending"
    if not pending_dir.exists():
        return 0
    count = 0
    for p in pending_dir.iterdir():
        if p.is_file() and p.name.endswith(".tmp"):
            try:
                p.unlink()
                count += 1
            except OSError as e:
                log.warning(
                    "Failed to remove orphan tmp file",
                    extra={"path": str(p), "error": str(e)},
                )
    return count


def _remove_older_than(directory: Path, cutoff_epoch: float) -> int:
    if not directory.exists():
        return 0
    removed = 0
    for p in directory.iterdir():
        if not p.is_file():
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff_epoch:
            try:
                p.unlink()
                removed += 1
            except OSError as e:
                log.warning(
                    "Failed to remove aged-out payload",
                    extra={"path": str(p), "error": str(e)},
                )
    return removed


def _retain_newest(directory: Path, keep: int) -> int:
    if not directory.exists() or keep < 0:
        return 0
    files: list[tuple[float, Path]] = []
    for p in directory.iterdir():
        if not p.is_file():
            continue
        try:
            files.append((p.stat().st_mtime, p))
        except OSError:
            continue
    if len(files) <= keep:
        return 0
    files.sort(key=lambda t: t[0], reverse=True)
    to_remove = files[keep:]
    removed = 0
    for _, p in to_remove:
        try:
            p.unlink()
            removed += 1
        except OSError as e:
            log.warning(
                "Failed to remove completed payload during prune",
                extra={"path": str(p), "error": str(e)},
            )
    return removed


def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for p in path.iterdir():
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total
