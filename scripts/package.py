"""Drive the Inno Setup compiler to produce the Windows installer.

Only meaningful on Windows; on other platforms this script prints instructions
and exits 0 (so CI can run it as a no-op gate on Linux/macOS).

Usage:
    python scripts/package.py [--version X.Y.Z]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALLER_DIR = PROJECT_ROOT / "installer"
ISS_FILE = INSTALLER_DIR / "mdqc.iss"
NSSM_PATH = INSTALLER_DIR / "nssm.exe"
NSSM_SHA256_PATH = INSTALLER_DIR / "nssm.sha256"
DEFAULT_ISCC_LOCATIONS = [
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
]

NSSM_ZIP_URL = "https://nssm.cc/release/nssm-2.24.zip"
NSSM_ZIP_SHA256_NOTE = (
    "The 2.24 release ships nssm.exe under nssm-2.24/win64/nssm.exe. "
    "Pin the zip SHA-256 in .github/workflows/ci.yml when wiring auto-download."
)


def _read_version_from_pyproject() -> str:
    pyproject = PROJECT_ROOT / "pyproject.toml"
    with open(pyproject, "rb") as fh:
        data = tomllib.load(fh)
    return str(data["project"]["version"])


def _find_iscc() -> Path | None:
    for candidate in DEFAULT_ISCC_LOCATIONS:
        if candidate.exists():
            return candidate
    found = shutil.which("iscc")
    if found:
        return Path(found)
    found = shutil.which("ISCC")
    if found:
        return Path(found)
    return None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def _verify_nssm() -> None:
    if not NSSM_PATH.exists():
        raise SystemExit(
            f"[package] FAIL: {NSSM_PATH} is missing.\n"
            f"    Download {NSSM_ZIP_URL}, verify SHA-256, extract win64/nssm.exe\n"
            f"    to that path, then re-run this script. ({NSSM_ZIP_SHA256_NOTE})"
        )

    try:
        size = NSSM_PATH.stat().st_size
    except OSError as exc:
        raise SystemExit(f"[package] FAIL: cannot stat {NSSM_PATH}: {exc}") from exc
    if size == 0:
        raise SystemExit(
            f"[package] FAIL: {NSSM_PATH} is zero bytes — refusing to package a "
            f"placeholder. Provision a real nssm.exe (see installer/README.md)."
        )

    if NSSM_SHA256_PATH.exists():
        try:
            expected = NSSM_SHA256_PATH.read_text(encoding="utf-8").strip().lower().split()[0]
        except (OSError, IndexError) as exc:
            raise SystemExit(
                f"[package] FAIL: cannot read {NSSM_SHA256_PATH}: {exc}"
            ) from exc
        actual = _sha256_file(NSSM_PATH)
        if actual != expected:
            raise SystemExit(
                f"[package] FAIL: {NSSM_PATH} SHA-256 mismatch.\n"
                f"    expected: {expected}\n"
                f"    actual:   {actual}"
            )


def _check_binary() -> None:
    binary = PROJECT_ROOT / "dist" / "mdqc.exe"
    if not binary.exists():
        raise SystemExit(
            f"[package] missing {binary}. Run `python scripts/build.py` first."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build mdqc Windows installer")
    parser.add_argument(
        "--version",
        default=None,
        help="Override version (default: read from pyproject.toml)",
    )
    args = parser.parse_args()

    version = args.version or _read_version_from_pyproject()

    if sys.platform != "win32":
        print(
            "[package] Installer build only runs on Windows. "
            f"The Inno Setup script is at {ISS_FILE.relative_to(PROJECT_ROOT)}. "
            f"Version that would be packaged: {version}",
        )
        return 0

    if not ISS_FILE.exists():
        raise SystemExit(f"[package] missing {ISS_FILE}")

    _check_binary()
    _verify_nssm()

    iscc = _find_iscc()
    if iscc is None:
        raise SystemExit(
            "[package] ISCC.exe not found. Install Inno Setup 6 from "
            "https://jrsoftware.org/isinfo.php"
        )

    out_dir = PROJECT_ROOT / "dist" / "installer"
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(iscc),
        f"/DAppVersion={version}",
        str(ISS_FILE),
    ]
    print(f"[package] running: {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, check=False, cwd=str(INSTALLER_DIR), env=os.environ)
    if result.returncode != 0:
        return result.returncode

    expected = out_dir / f"mdqc-setup-py-v{version}.exe"
    if not expected.exists():
        print(
            f"[package] WARNING: expected {expected} but it does not exist; "
            f"check {out_dir} for actual output.",
            file=sys.stderr,
        )
    else:
        print(f"[package] produced: {expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
