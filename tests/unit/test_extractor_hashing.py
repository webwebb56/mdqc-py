from __future__ import annotations

from pathlib import Path

from mdqc.extractor import compute_raw_hash, compute_template_hash


def test_template_hash_stable(tmp_path: Path) -> None:
    f = tmp_path / "tmpl.sky"
    f.write_bytes(b"some skyline template content")
    h1 = compute_template_hash(f)
    h2 = compute_template_hash(f)
    assert h1 == h2
    assert len(h1) == 64


def test_template_hash_changes_with_content(tmp_path: Path) -> None:
    a = tmp_path / "a.sky"
    b = tmp_path / "b.sky"
    a.write_bytes(b"alpha")
    b.write_bytes(b"beta")
    assert compute_template_hash(a) != compute_template_hash(b)


def test_file_hash_stable_across_calls(tmp_path: Path) -> None:
    f = tmp_path / "raw.raw"
    f.write_bytes(b"x" * 1024)
    assert compute_raw_hash(f) == compute_raw_hash(f)


def test_directory_hash_uses_filename_size_not_contents(tmp_path: Path) -> None:
    d1 = tmp_path / "first.d"
    d2 = tmp_path / "second.d"
    d1.mkdir()
    d2.mkdir()

    (d1 / "analysis.tdf").write_bytes(b"A" * 100)
    (d1 / "analysis.tdf_bin").write_bytes(b"B" * 200)

    (d2 / "analysis.tdf").write_bytes(b"Z" * 100)
    (d2 / "analysis.tdf_bin").write_bytes(b"Q" * 200)

    assert compute_raw_hash(d1) == compute_raw_hash(d2)


def test_directory_hash_changes_with_size(tmp_path: Path) -> None:
    d1 = tmp_path / "x.d"
    d2 = tmp_path / "y.d"
    d1.mkdir()
    d2.mkdir()
    (d1 / "f").write_bytes(b"x" * 10)
    (d2 / "f").write_bytes(b"x" * 11)
    assert compute_raw_hash(d1) != compute_raw_hash(d2)


def test_directory_hash_changes_with_filename(tmp_path: Path) -> None:
    d1 = tmp_path / "a.d"
    d2 = tmp_path / "b.d"
    d1.mkdir()
    d2.mkdir()
    (d1 / "alpha").write_bytes(b"x" * 10)
    (d2 / "beta").write_bytes(b"x" * 10)
    assert compute_raw_hash(d1) != compute_raw_hash(d2)
