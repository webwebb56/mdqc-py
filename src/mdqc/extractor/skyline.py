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
import subprocess
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


# Skyline scatters previously-imported result references across multiple
# elements. Stripping only <measured_results> leaves Skyline complaining
# "No results information found in the document settings" because the
# per-peptide / per-precursor / per-transition <*_results> blocks still
# reference replicates that no longer exist. We strip them all.
_RESULTS_ELEMENTS = (
    b"transition_results",
    b"precursor_results",
    b"peptide_results",
    b"measured_results",
)


def _strip_measured_results(sky_bytes: bytes) -> tuple[bytes, int]:
    """Remove all imported-result references from a Skyline ``.sky`` document.

    Strips ``<measured_results>`` (the global replicate catalog) plus every
    ``<peptide_results>``, ``<precursor_results>``, and ``<transition_results>``
    block that Skyline scatters through each peptide / precursor / transition
    node. The result is what *Edit → Manage Results → Remove All → Save* in the
    Skyline GUI produces: a "fresh" template with no imported data.

    Returns ``(cleaned_bytes, n_replicates_removed)``. Idempotent: returns
    ``(sky_bytes, 0)`` unchanged on already-clean documents and on parse
    failures (we'd rather pass a polluted template through than corrupt a
    working one — Skyline will surface a clear error either way).

    Why this exists: operators routinely open the template in Skyline GUI to
    sanity-check that imports work, then save the document. The save persists
    imported replicates with their absolute file paths. On the next mdqc run
    Skyline tries to refresh those imports against stale paths and the whole
    extraction fails — even though the file mdqc passed via ``--import-file``
    was fine. Stripping at copy time makes mdqc tolerant of this footgun.
    """
    cleaned = sky_bytes
    for tag in _RESULTS_ELEMENTS:
        # ^[ \t]*  - consume leading indentation on the line
        # <tag(>|\s) - tag with closing > or attribute whitespace
        # [\s\S]*? - non-greedy match across newlines (these elements aren't
        #            self-nested, so non-greedy is safe and avoids over-matching)
        # </tag>   - matching close
        # [\r\n]*  - consume trailing line break
        pattern = re.compile(
            rb"^[ \t]*<" + tag + rb"(?:>|\s[\s\S]*?>)[\s\S]*?</" + tag + rb">[\r\n]*",
            re.MULTILINE,
        )
        cleaned = pattern.sub(b"", cleaned)
    # Count only the replicates we actually removed: original count minus
    # whatever survived (typically 0 — the regex strips everything that
    # contains <replicate ...>).
    n_replicates = sky_bytes.count(b"<replicate ") - cleaned.count(b"<replicate ")
    return cleaned, n_replicates


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


_TEMPLATE_HEAD_BYTES = 8192
_FORMAT_VERSION_RE = re.compile(r'<srm_settings\b[^>]*?\bformat_version="([^"]+)"')
_SOFTWARE_VERSION_RE = re.compile(r'<srm_settings\b[^>]*?\bsoftware_version="([^"]+)"')
_MAJOR_MINOR_RE = re.compile(r"\s*(\d+)\.(\d+)")


def read_skyline_version(exe: Path, *, timeout_s: float = 30.0) -> str | None:
    """Installed Skyline version, e.g. ``"26.1.0.57"``, or None if unreadable.

    Tries the executable's Windows file version first - instant, no process -
    then ``SkylineCmd --version``, which prints e.g.
    ``Skyline (64-bit) 26.1.0.057 (c07debd50)``.
    """
    if sys.platform == "win32":
        try:
            import win32api

            info = win32api.GetFileVersionInfo(str(exe), "\\")
            ms, ls = info["FileVersionMS"], info["FileVersionLS"]
            return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
        except Exception:  # pywin32 absent, or no version resource on the file
            pass
    try:
        result = subprocess.run(
            [str(exe), "--version"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_version(result.stdout, result.stderr)


def read_template_format(path: Path) -> tuple[str | None, str | None]:
    """``(format_version, software_version)`` from a ``.sky`` document's root element.

    Only the head of the file is read - templates can be large. Returns
    ``(None, None)`` for anything that isn't a readable Skyline document.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(_TEMPLATE_HEAD_BYTES).decode("utf-8", "replace")
    except OSError:
        return None, None
    fmt = _FORMAT_VERSION_RE.search(head)
    saved_by = _SOFTWARE_VERSION_RE.search(head)
    return (fmt.group(1) if fmt else None, saved_by.group(1) if saved_by else None)


def _major_minor(version: str | None) -> tuple[int, int] | None:
    if not version:
        return None
    m = _MAJOR_MINOR_RE.match(version)
    return (int(m.group(1)), int(m.group(2))) if m else None


def template_newer_than_skyline(
    format_version: str | None, skyline_version: str | None
) -> bool | None:
    """True when a template's document format is newer than the installed Skyline.

    Skyline refuses such a document outright - "The document format version
    26.11 is newer than the version 26.1 supported by Skyline (64-bit)
    26.1.0.057" - so every extraction fails. That is what stopped Evosep's
    first Sciex 7500 installation (September 2026). Skyline's own message ties
    the newest format it reads to its major.minor (26.1.0.057 reads up to
    26.1), so compare those, as integers: 26.11 is newer than 26.1.

    None when either version can't be read - unknown, not compatible.
    """
    fmt = _major_minor(format_version)
    installed = _major_minor(skyline_version)
    if fmt is None or installed is None:
        return None
    return fmt > installed


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
