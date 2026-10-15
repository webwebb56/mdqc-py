"""Build MDQC-Setup-<version>.exe: MDQC with its own Python, for USB installs.

The setup program needs no Python, no PowerShell and no internet on the
instrument PC. It carries python.org's official embeddable Python (signed by
the Python Software Foundation) with MDQC and every dependency installed into
it, wrapped by Inno Setup.

    python scripts/build_installer.py                  # stage + compile
    python scripts/build_installer.py --stage-only     # stage + smoke test, no Inno Setup

Steps:
  1. Build the mdqc wheel from this checkout.
  2. Download the embeddable Python (cached in build/cache) and unpack it.
  3. Let it load site-packages (the ._pth file), and install mdqc[tray] plus
     dependencies into it as Windows wheels only - nothing is compiled.
  4. Smoke-test the staged runtime: mdqc version and selfcheck.
  5. Compile installer/mdqc.iss with Inno Setup's ISCC.exe.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tomllib
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "installer"
APP = BUILD / "app"
RUNTIME = APP / "python"
CACHE = ROOT / "build" / "cache"
DIST = ROOT / "dist"

# python.org's embeddable package. Change both together; the hash pins the
# exact file so a tampered or truncated download fails the build.
PYTHON_VERSION = "3.13.16"
PYTHON_SHA256 = os.environ.get("MDQC_EMBED_SHA256", "")

# Written next to python.exe; `mdqc stop` only ends processes under its own
# folder when it finds this (see src/mdqc/cli/stop.py).
RUNTIME_MARKER = "MDQC_RUNTIME"

ISCC_CANDIDATES = [
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe",
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Inno Setup 6" / "ISCC.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
]


def run(cmd: list[str], **kw: object) -> None:
    print("  $", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)  # type: ignore[arg-type]


def version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def build_wheel() -> Path:
    out = BUILD / "wheel"
    shutil.rmtree(out, ignore_errors=True)
    run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--quiet", "-w", out, ROOT])
    (wheel,) = out.glob("mdqc-*.whl")
    return wheel


def fetch_embeddable() -> Path:
    name = f"python-{PYTHON_VERSION}-embed-amd64.zip"
    target = CACHE / name
    if not target.is_file():
        CACHE.mkdir(parents=True, exist_ok=True)
        url = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{name}"
        print(f"  downloading {url}", flush=True)
        tmp = target.with_suffix(".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    print(f"  {name} sha256 {digest}")
    if PYTHON_SHA256 and digest != PYTHON_SHA256.lower():
        sys.exit(f"{name}: sha256 {digest} does not match MDQC_EMBED_SHA256")
    return target


def stage_runtime(embed_zip: Path, wheel: Path) -> None:
    shutil.rmtree(APP, ignore_errors=True)
    RUNTIME.mkdir(parents=True)
    with zipfile.ZipFile(embed_zip) as z:
        z.extractall(RUNTIME)

    # The embeddable Python ignores site-packages unless its ._pth file says
    # otherwise; without this nothing installed below would import.
    (pth,) = RUNTIME.glob("python3*._pth")
    lines = [ln for ln in pth.read_text(encoding="utf-8").splitlines() if ln.strip() != "#import site"]
    lines += ["Lib\\site-packages", "import site"]
    pth.write_text("\n".join(lines) + "\n", encoding="utf-8")

    site = RUNTIME / "Lib" / "site-packages"
    site.mkdir(parents=True)
    major, minor = PYTHON_VERSION.split(".")[:2]
    run([
        sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
        "--target", site,
        "--platform", "win_amd64", "--python-version", f"{major}.{minor}",
        "--implementation", "cp", "--only-binary=:all:",
        f"mdqc[tray] @ {wheel.as_uri()}",
    ])
    # pip --target leaves console-script launchers bound to the build
    # machine's Python; the installer starts everything with python -m mdqc.
    shutil.rmtree(site / "bin", ignore_errors=True)
    (RUNTIME / RUNTIME_MARKER).write_text(
        "Private Python for the MD QC Agent. Managed by its setup program.\n", encoding="utf-8"
    )

    # Command-line entry point beside the runtime, for support use.
    (APP / "mdqc.cmd").write_text('@"%~dp0python\\python.exe" -m mdqc %*\n', encoding="ascii")

    # Icon for the shortcuts, the setup program and Apps & features.
    from PIL import Image

    Image.open(ROOT / "assets" / "icon.png").save(
        APP / "mdqc.ico", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)]
    )


def smoke_test() -> None:
    py = RUNTIME / "python.exe"
    env = {**os.environ, "MDQC_DATA_DIR": str(BUILD / "smoke-data")}
    for args in (["version"], ["selfcheck"]):
        run([py, "-m", "mdqc", *args], env=env)


def find_iscc(explicit: str | None) -> Path:
    for p in [Path(explicit)] if explicit else ISCC_CANDIDATES:
        if p.is_file():
            return p
    found = shutil.which("ISCC")
    if found:
        return Path(found)
    sys.exit("Inno Setup's ISCC.exe not found. Install Inno Setup 6, or pass --iscc.")


def compile_installer(iscc: Path, ver: str, sign_cmd: str | None) -> Path:
    DIST.mkdir(exist_ok=True)
    cmd = [iscc, "/Qp", f"/DAppVersion={ver}", f"/DSourceDir={APP}", f"/O{DIST}"]
    if sign_cmd:
        cmd.append(f"/Smdqcsign={sign_cmd}")
        cmd.append("/DSign")
    cmd.append(ROOT / "installer" / "mdqc.iss")
    run(cmd)
    out = DIST / f"MDQC-Setup-{ver}.exe"
    print(f"\nBuilt {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage-only", action="store_true", help="Stage and smoke-test; skip Inno Setup.")
    ap.add_argument("--iscc", help="Path to ISCC.exe.")
    ap.add_argument(
        "--sign", help='Sign tool command for Inno Setup, e.g. "signtool sign /fd sha256 ... $f".'
    )
    args = ap.parse_args()

    ver = version()
    print(f"MDQC {ver} with Python {PYTHON_VERSION}")
    print("1. wheel")
    wheel = build_wheel()
    print("2. embeddable Python")
    embed = fetch_embeddable()
    print("3. runtime")
    stage_runtime(embed, wheel)
    print("4. smoke test")
    smoke_test()
    if args.stage_only:
        print(f"\nStaged in {APP}")
        return
    print("5. setup program")
    compile_installer(find_iscc(args.iscc), ver, args.sign)


if __name__ == "__main__":
    main()
