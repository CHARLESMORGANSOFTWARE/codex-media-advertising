from __future__ import annotations

import hashlib
import importlib.util
import zipfile
from pathlib import Path

import pytest


def _load_script(name: str):
    root = Path(__file__).parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, root)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_scanner_accepts_documented_synthetic_examples(tmp_path: Path):
    scanner = _load_script("scan_release")
    (tmp_path / "README.md").write_text(
        "Contact author@example.com; see https://example.com; fixture id synthetic-123.\n"
    )
    assert scanner.scan_path(tmp_path) == []


def test_release_scanner_rejects_private_artifacts_and_absolute_paths(tmp_path: Path):
    scanner = _load_script("scan_release")
    (tmp_path / ".env.production").write_text("TOKEN=not-real\n")
    (tmp_path / "notes.txt").write_text("/Users/private/Library/Application Support/secret\n")
    findings = scanner.scan_path(tmp_path)
    assert any("filename" in finding for finding in findings)
    assert any("absolute path" in finding for finding in findings)


def test_build_release_is_sorted_normalized_and_scanned(tmp_path: Path, monkeypatch):
    builder = _load_script("build_release")
    source = tmp_path / "repo"
    source.mkdir()
    (source / "b.txt").write_text("b\n")
    (source / "a.txt").write_text("a\n")
    monkeypatch.setattr(builder, "tracked_files", lambda _: [Path("b.txt"), Path("a.txt")])
    monkeypatch.setattr(builder, "run_scanner", lambda *_: [])
    archive, digest = builder.build_release(source, tmp_path / "dist")
    assert archive.name == "codex-media-advertising-0.1.0.zip"
    assert digest.read_text().startswith(hashlib.sha256(archive.read_bytes()).hexdigest())
    with zipfile.ZipFile(archive) as handle:
        assert handle.namelist() == ["a.txt", "b.txt"]
        assert [info.date_time for info in handle.infolist()] == [(2020, 1, 1, 0, 0, 0)] * 2
