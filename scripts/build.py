"""Drive PyInstaller to produce a single-file `mdqc` binary.

See docs/AGENT_NOTES.md § Packaging for hidden-imports rationale and
docs/PLAN.md § Phase 6 for the build/install flow.

Usage:
    python scripts/build.py [--clean]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"
SPEC_PATH = PROJECT_ROOT / "mdqc.spec"

HIDDEN_IMPORTS: list[str] = [
    "winsdk.windows.ui.notifications",
    "winsdk.windows.data.xml.dom",
    "pystray._win32",
    "pystray._darwin",
    "pystray._gtk",
    "watchdog.observers.winapi",
    "watchdog.observers.read_directory_changes",
    "watchdog.observers.polling",
    "httpx._transports.default",
    "pydantic.deprecated.decorator",
    "cryptography.hazmat.primitives.serialization.pkcs12",
]


def _add_data_arg(src: Path, dest: str) -> str:
    sep = ";" if sys.platform == "win32" else ":"
    return f"{src}{sep}{dest}"


def _binary_path() -> Path:
    name = "mdqc.exe" if sys.platform == "win32" else "mdqc"
    return DIST_DIR / name


def _clean() -> None:
    for path in (DIST_DIR, BUILD_DIR, SPEC_PATH):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            try:
                path.unlink()
            except OSError:
                pass


def _build() -> Path:
    try:
        from PyInstaller import __main__ as pyi_main
    except ImportError as exc:
        raise SystemExit(
            "PyInstaller is not installed. Run `pip install -e .[build]` first."
        ) from exc

    entry = PROJECT_ROOT / "src" / "mdqc" / "__main__.py"
    icon = PROJECT_ROOT / "assets" / "icon.png"
    assets_dir = PROJECT_ROOT / "assets"
    templates_dir = PROJECT_ROOT / "src" / "mdqc" / "webui" / "templates"
    static_dir = PROJECT_ROOT / "src" / "mdqc" / "webui" / "static"

    args: list[str] = [
        "--name=mdqc",
        "--onefile",
        "--noconfirm",
        f"--distpath={DIST_DIR}",
        f"--workpath={BUILD_DIR}",
        f"--specpath={PROJECT_ROOT}",
        f"--icon={icon}",
        f"--add-data={_add_data_arg(assets_dir, 'assets')}",
    ]
    if templates_dir.exists():
        args.append(f"--add-data={_add_data_arg(templates_dir, 'mdqc/webui/templates')}")
    if static_dir.exists():
        args.append(f"--add-data={_add_data_arg(static_dir, 'mdqc/webui/static')}")
    for mod in HIDDEN_IMPORTS:
        args.append(f"--hidden-import={mod}")
    args.append(str(entry))

    print(f"[build] running PyInstaller with {len(args)} args", flush=True)
    pyi_main.run(args)

    binary = _binary_path()
    if not binary.exists():
        raise SystemExit(f"PyInstaller finished but {binary} is missing")
    return binary


def _selfcheck(binary: Path) -> int:
    print(f"[build] running selfcheck: {binary} selfcheck", flush=True)
    result = subprocess.run([str(binary), "selfcheck"], check=False)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Build mdqc binary via PyInstaller")
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove existing dist/, build/, and spec file before building",
    )
    args = parser.parse_args()

    if args.clean:
        print("[build] cleaning dist/, build/, mdqc.spec")
        _clean()

    binary = _build()
    print(f"[build] produced: {binary}")

    code = _selfcheck(binary)
    if code != 0:
        print(f"[build] selfcheck FAILED with exit code {code}", file=sys.stderr)
        return code

    print("[build] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
