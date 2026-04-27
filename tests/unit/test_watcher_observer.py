from __future__ import annotations

import sys
import time
from pathlib import Path

from watchdog.observers.polling import PollingObserver

from mdqc.types import Vendor
from mdqc.watcher.observer import WatchdogObserver, _PatternHandler, is_unc_path


def test_is_unc_path_true_for_double_backslash() -> None:
    assert is_unc_path(Path(r"\\server\share"))


def test_is_unc_path_false_for_drive_letter() -> None:
    if sys.platform == "win32":
        assert is_unc_path(Path(r"C:\foo")) in (True, False)
    else:
        assert not is_unc_path(Path(r"C:\foo"))


def test_is_unc_path_false_for_posix_path() -> None:
    assert not is_unc_path(Path("/tmp/foo"))


def test_pattern_handler_filters_by_pattern(tmp_path: Path) -> None:
    seen: list[tuple[Path, Vendor]] = []

    def cb(p: Path, v: Vendor) -> None:
        seen.append((p, v))

    handler = _PatternHandler(tmp_path, Vendor.THERMO, "*.raw", cb)

    class Evt:
        is_directory = False

        def __init__(self, src: str) -> None:
            self.src_path = src

    handler.on_created(Evt(str(tmp_path / "ok.raw")))
    handler.on_created(Evt(str(tmp_path / "skip.txt")))
    assert seen == [(tmp_path / "ok.raw", Vendor.THERMO)]


def test_pattern_handler_rejects_directory_for_thermo(tmp_path: Path) -> None:
    seen: list[tuple[Path, Vendor]] = []
    handler = _PatternHandler(tmp_path, Vendor.THERMO, "*.raw", lambda p, v: seen.append((p, v)))

    class Evt:
        def __init__(self, src: str, is_dir: bool) -> None:
            self.src_path = src
            self.is_directory = is_dir

    handler.on_created(Evt(str(tmp_path / "dir.raw"), True))
    assert seen == []


def test_pattern_handler_accepts_directory_for_bruker(tmp_path: Path) -> None:
    seen: list[tuple[Path, Vendor]] = []
    handler = _PatternHandler(tmp_path, Vendor.BRUKER, "*.d", lambda p, v: seen.append((p, v)))

    class Evt:
        def __init__(self, src: str, is_dir: bool) -> None:
            self.src_path = src
            self.is_directory = is_dir

    handler.on_created(Evt(str(tmp_path / "run.d"), True))
    assert seen == [(tmp_path / "run.d", Vendor.BRUKER)]


def test_full_scan_emits_existing_files(tmp_path: Path) -> None:
    (tmp_path / "a.raw").write_bytes(b"x")
    (tmp_path / "b.txt").write_bytes(b"x")

    seen: list[tuple[Path, Vendor]] = []
    obs = WatchdogObserver(
        [(tmp_path, Vendor.THERMO, "*.raw")],
        lambda p, v: seen.append((p, v)),
    )
    obs.full_scan()
    assert seen == [(tmp_path / "a.raw", Vendor.THERMO)]


def test_observer_smoke_with_polling(tmp_path: Path) -> None:
    seen: list[tuple[Path, Vendor]] = []

    def cb(p: Path, v: Vendor) -> None:
        seen.append((p, v))

    handler = _PatternHandler(tmp_path, Vendor.THERMO, "*.raw", cb)
    observer = PollingObserver(timeout=0.2)
    observer.schedule(handler, str(tmp_path), recursive=False)
    observer.start()
    try:
        time.sleep(0.3)
        (tmp_path / "smoke.raw").write_bytes(b"data")
        deadline = time.time() + 3.0
        while time.time() < deadline and not seen:
            time.sleep(0.1)
    finally:
        observer.stop()
        observer.join(timeout=2.0)
    assert (tmp_path / "smoke.raw", Vendor.THERMO) in seen
