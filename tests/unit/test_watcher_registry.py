from __future__ import annotations

from pathlib import Path

import pytest

from mdqc.config import defaults, paths
from mdqc.watcher.registry import ProcessedRegistry


def test_add_persist_reload(tmp_data_dir: Path) -> None:
    reg = ProcessedRegistry()
    reg.add(Path("/data/run1.raw"))
    reg.add(Path("/data/run2.raw"))
    assert reg.contains(Path("/data/run1.raw"))
    assert reg.contains(Path("/data/run2.raw"))

    reg2 = ProcessedRegistry()
    assert reg2.contains(Path("/data/run1.raw"))
    assert reg2.contains(Path("/data/run2.raw"))


def test_clear(tmp_data_dir: Path) -> None:
    reg = ProcessedRegistry()
    reg.add(Path("/data/run1.raw"))
    reg.clear()
    assert not reg.contains(Path("/data/run1.raw"))
    assert ProcessedRegistry().contains(Path("/data/run1.raw")) is False


def test_fifo_eviction_at_max(
    tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(defaults, "PROCESSED_REGISTRY_MAX", 3)
    reg = ProcessedRegistry()
    reg.add(Path("/data/run1.raw"))
    reg.add(Path("/data/run2.raw"))
    reg.add(Path("/data/run3.raw"))
    reg.add(Path("/data/run4.raw"))

    assert not reg.contains(Path("/data/run1.raw"))
    assert reg.contains(Path("/data/run2.raw"))
    assert reg.contains(Path("/data/run3.raw"))
    assert reg.contains(Path("/data/run4.raw"))
    assert len(reg) == 3


def test_corrupt_file_recovers_gracefully(tmp_data_dir: Path) -> None:
    paths.processed_registry_path().parent.mkdir(parents=True, exist_ok=True)
    paths.processed_registry_path().write_text("{not json", encoding="utf-8")

    reg = ProcessedRegistry()
    assert len(reg) == 0
    reg.add(Path("/data/recovered.raw"))
    assert reg.contains(Path("/data/recovered.raw"))


def test_atomic_write_uses_tmp(tmp_data_dir: Path) -> None:
    reg = ProcessedRegistry()
    reg.add(Path("/data/x.raw"))
    target = paths.processed_registry_path()
    assert target.exists()
    assert not target.with_suffix(target.suffix + ".tmp").exists()


def test_duplicate_add_is_noop(tmp_data_dir: Path) -> None:
    reg = ProcessedRegistry()
    reg.add(Path("/data/dup.raw"))
    reg.add(Path("/data/dup.raw"))
    assert len(reg) == 1
