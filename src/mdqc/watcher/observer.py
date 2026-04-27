from __future__ import annotations

import contextlib
import fnmatch
import logging
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from mdqc.config.defaults import SCAN_INTERVAL_S
from mdqc.types import Vendor

log = logging.getLogger(__name__)

DRIVE_REMOTE = 4

DetectedCallback = Callable[[Path, Vendor], None]


def is_unc_path(path: Path) -> bool:
    raw = str(path)
    if raw.startswith("\\\\"):
        return True
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False
    drive = os.path.splitdrive(raw)[0]
    if not drive:
        return False
    drive_root = drive + "\\"
    try:
        result = kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive_root))
    except OSError:
        return False
    return int(result) == DRIVE_REMOTE


class _PatternHandler(FileSystemEventHandler):
    def __init__(
        self,
        watch_path: Path,
        vendor: Vendor,
        pattern: str,
        on_detected: DetectedCallback,
    ) -> None:
        self._watch_path = watch_path
        self._vendor = vendor
        self._pattern = pattern
        self._on_detected = on_detected

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe_emit(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        target = getattr(event, "dest_path", None) or event.src_path
        self._dispatch(Path(target), event.is_directory)

    def _maybe_emit(self, event: FileSystemEvent) -> None:
        self._dispatch(Path(event.src_path), event.is_directory)

    def _dispatch(self, path: Path, is_directory: bool) -> None:
        if self._vendor.is_directory_artifact and not is_directory:
            return
        if not self._vendor.is_directory_artifact and is_directory:
            return
        if not fnmatch.fnmatch(path.name, self._pattern):
            return
        try:
            self._on_detected(path, self._vendor)
        except Exception:
            log.exception("on_detected callback failed for %s", path)


class WatchdogObserver:
    def __init__(
        self,
        paths: list[tuple[Path, Vendor, str]],
        on_detected: DetectedCallback,
    ) -> None:
        self._specs = paths
        self._on_detected = on_detected
        self._observers: list[Observer | PollingObserver] = []
        self._scan_timer: threading.Timer | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        for watch_path, vendor, pattern in self._specs:
            handler = _PatternHandler(watch_path, vendor, pattern, self._on_detected)
            observer: Observer | PollingObserver
            observer = PollingObserver() if is_unc_path(watch_path) else Observer()
            observer.schedule(handler, str(watch_path), recursive=False)
            observer.start()
            self._observers.append(observer)
        self._schedule_scan()

    def stop(self) -> None:
        self._stop_event.set()
        if self._scan_timer is not None:
            self._scan_timer.cancel()
            self._scan_timer = None
        for observer in self._observers:
            observer.stop()
        for observer in self._observers:
            with contextlib.suppress(RuntimeError):
                observer.join(timeout=5.0)
        self._observers.clear()

    def _schedule_scan(self) -> None:
        if self._stop_event.is_set():
            return
        timer = threading.Timer(SCAN_INTERVAL_S, self._scan_tick)
        timer.daemon = True
        timer.start()
        self._scan_timer = timer

    def _scan_tick(self) -> None:
        try:
            self.full_scan()
        except Exception:
            log.exception("full_scan failed")
        self._schedule_scan()

    def full_scan(self) -> None:
        for watch_path, vendor, pattern in self._specs:
            if not watch_path.exists():
                continue
            try:
                entries = list(watch_path.iterdir())
            except OSError:
                continue
            for entry in entries:
                if vendor.is_directory_artifact and not entry.is_dir():
                    continue
                if not vendor.is_directory_artifact and not entry.is_file():
                    continue
                if not fnmatch.fnmatch(entry.name, pattern):
                    continue
                try:
                    self._on_detected(entry, vendor)
                except Exception:
                    log.exception("on_detected callback failed for %s", entry)
