"""SkylineCmd.exe discovery and subprocess invocation.

See docs/AGENT_NOTES § Extractor for the priority-class trap, the ClickOnce
detection, and the stdout-vs-stderr error-checking contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from mdqc.config.defaults import SKYLINE_TIMEOUT_S

logger = logging.getLogger(__name__)

# Files that Skyline reads from the template directory but doesn't write to.
# Hardlinking them into the per-extraction temp dir is essentially free on
# NTFS — no I/O, just an extra directory entry pointing at the same inode.
_LIBRARY_EXTENSIONS = (".blib", ".skyl", ".sky.view")


def _strip_measured_results(sky_bytes: bytes) -> tuple[bytes, int]:
    """Remove the ``<measured_results>...</measured_results>`` block from a
    Skyline document.

    Returns ``(cleaned_bytes, n_replicates_removed)``. Idempotent — if the
    block is missing or the document is malformed (no closing tag), returns
    ``(sky_bytes, 0)`` unchanged.

    Why: operators commonly open the template in Skyline GUI to sanity-check
    that imports work, then save the document. The save persists imported
    replicates with their absolute file paths into the .sky. On the next run
    Skyline tries to refresh those imports against now-stale paths and the
    whole extraction fails — even though the file mdqc passed via
    --import-file was fine. Stripping the block on copy makes mdqc tolerant
    of polluted templates so this footgun stops biting people.
    """
    start_idx = sky_bytes.find(b"<measured_results")
    if start_idx < 0:
        return sky_bytes, 0
    end_marker = b"</measured_results>"
    end_idx = sky_bytes.find(end_marker, start_idx)
    if end_idx < 0:
        return sky_bytes, 0
    end_idx += len(end_marker)

    # Pull in the leading whitespace on the start line so we don't leave a
    # stranded blank-indented line behind.
    line_start = sky_bytes.rfind(b"\n", 0, start_idx) + 1
    if all(b in (0x20, 0x09) for b in sky_bytes[line_start:start_idx]):
        start_idx = line_start

    # Consume one trailing newline (CRLF or LF).
    if sky_bytes[end_idx:end_idx + 2] == b"\r\n":
        end_idx += 2
    elif end_idx < len(sky_bytes) and sky_bytes[end_idx] == 0x0A:
        end_idx += 1

    stripped = sky_bytes[start_idx:end_idx]
    replicate_count = stripped.count(b"<replicate ")
    return sky_bytes[:start_idx] + sky_bytes[end_idx:], replicate_count


class SkylineNotFound(Exception):
    pass


class SkylineClickOnceUnsupported(Exception):
    pass


class SkylineTimeout(Exception):
    pass


class SkylineFailed(Exception):
    def __init__(self, message: str, *, stdout: str = "", stderr: str = "", returncode: int = -1):
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@dataclass
class SkylineRunResult:
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    version: str | None = None


_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+){0,2})")
_ERROR_MARKERS = ("Error:", "ERROR:", "Failed", "FAILED", "Exception", "EXCEPTION")
_REGISTRY_KEYS = (
    (r"SOFTWARE\Apache\Skyline", "Path"),
    (r"SOFTWARE\Apache\Skyline", "InstallPath"),
    (r"SOFTWARE\ProteoWizard\Skyline", "InstallPath"),
    (r"SOFTWARE\Skyline", "InstallPath"),
    (r"SOFTWARE\WOW6432Node\ProteoWizard\Skyline", "InstallPath"),
)
_COMMON_PATHS = (
    r"C:\Program Files\Skyline\SkylineCmd.exe",
    r"C:\Program Files (x86)\Skyline\SkylineCmd.exe",
)


def is_clickonce_install(path: Path) -> bool:
    s = str(path).lower().replace("/", "\\")
    return "\\apps\\2.0\\" in s


def _registry_lookup() -> Path | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None

    for key_path, value_name in _REGISTRY_KEYS:
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    install, _ = winreg.QueryValueEx(key, value_name)
            except OSError:
                continue
            install_str = str(install).strip()
            if not install_str:
                continue
            candidate = Path(install_str)
            if candidate.is_file():
                return candidate
            cmd = candidate / "SkylineCmd.exe"
            if cmd.is_file():
                return cmd
    return None


def find_skyline(explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        explicit_path = Path(explicit)
        if explicit_path.is_file():
            return explicit_path

    from_registry = _registry_lookup()
    if from_registry is not None:
        return from_registry

    for raw in _COMMON_PATHS:
        p = Path(raw)
        if p.is_file():
            return p

    for name in ("SkylineCmd.exe", "SkylineCmd"):
        which = shutil.which(name)
        if which:
            return Path(which)

    return None


def _parse_version(stdout: str, stderr: str) -> str | None:
    for source in (stdout, stderr):
        if not source:
            continue
        match = _VERSION_RE.search(source)
        if match:
            return match.group(1)
    return None


def has_error_marker(stdout: str, stderr: str) -> bool:
    return any(any(marker in source for marker in _ERROR_MARKERS) for source in (stdout, stderr))


async def run_skyline(
    skyline_exe: Path,
    template: Path,
    raw_file: Path,
    report_name: str,
    output_csv: Path,
    timeout_s: int = SKYLINE_TIMEOUT_S,
    priority: str = "below_normal",
    report_skyr: Path | None = None,
) -> SkylineRunResult:
    # Each concurrent extraction needs its own copy of the template so Skyline
    # doesn't fight over the shared QC_Method.skyd cache file.
    # Spectral libraries (.blib) and the spectral-library-list (.skyl) are
    # read-only and large, so hardlink them instead of copying.
    tmp_dir = tempfile.mkdtemp(prefix="mdqc_sky_")
    tmp_template = Path(tmp_dir) / template.name
    sky_bytes = template.read_bytes()
    cleaned, n_stripped = _strip_measured_results(sky_bytes)
    tmp_template.write_bytes(cleaned)
    if n_stripped > 0:
        logger.warning(
            "Stripped %d embedded replicate(s) from template %s before passing to Skyline. "
            "The template was previously used to import results in the Skyline GUI; "
            "those references would otherwise cause Skyline to look up stale paths and fail.",
            n_stripped, template.name,
        )

    template_dir = template.parent
    for sibling in template_dir.iterdir():
        if not sibling.is_file() or sibling.name == template.name:
            continue
        if not any(sibling.name.lower().endswith(ext) for ext in _LIBRARY_EXTENSIONS):
            continue
        target = Path(tmp_dir) / sibling.name
        try:
            os.link(sibling, target)
        except (OSError, NotImplementedError):
            # Hardlinks unsupported (e.g. cross-volume) → fall back to copy.
            shutil.copy2(sibling, target)

    try:
        args = [f"--in={tmp_template}"]
        if report_skyr is not None and report_skyr.is_file():
            args.append(f"--report-add={report_skyr}")
        args += [
            f"--import-file={raw_file}",
            f"--report-name={report_name}",
            f"--report-file={output_csv}",
            "--report-format=csv",
        ]

        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            str(skyline_exe),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Set process priority AFTER spawn — see docs/AGENT_NOTES § Extractor priority-class trap.
        _apply_priority(proc.pid, priority)

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError as exc:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.communicate(), timeout=5)
            except (TimeoutError, ProcessLookupError):
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(ProcessLookupError, ValueError):
                    await proc.communicate()
            raise SkylineTimeout(
                f"SkylineCmd timed out after {timeout_s}s"
            ) from exc

        duration_ms = int((time.monotonic() - start) * 1000)
        stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
        stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
        version = _parse_version(stdout, stderr)

        return SkylineRunResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            version=version,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


_PRIORITY_MAP_POSIX = {"normal": 0, "below_normal": 10, "idle": 19}


def _apply_priority(pid: int, priority: str) -> None:
    try:
        proc = psutil.Process(pid)
        if sys.platform == "win32":
            mapping = {
                "normal": psutil.NORMAL_PRIORITY_CLASS,
                "below_normal": psutil.BELOW_NORMAL_PRIORITY_CLASS,
                "idle": psutil.IDLE_PRIORITY_CLASS,
            }
            proc.nice(mapping.get(priority, psutil.BELOW_NORMAL_PRIORITY_CLASS))
        else:
            proc.nice(_PRIORITY_MAP_POSIX.get(priority, 10))
    except (psutil.NoSuchProcess, psutil.AccessDenied, PermissionError, OSError):
        return
