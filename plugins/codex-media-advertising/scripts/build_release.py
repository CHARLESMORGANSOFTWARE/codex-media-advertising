#!/usr/bin/env python3
"""Build the deterministic Codex Media & Advertising source archive."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Iterable


VERSION = "0.1.0"
ARCHIVE_NAME = f"codex-media-advertising-{VERSION}.zip"
_EXCLUDED_PREFIXES = (
    ".git/",
    ".superpowers/",
    ".agents/",
    "docs/superpowers/",
)
_EXCLUDED_PARTS = {".pytest_cache", "__pycache__"}


def _scanner_module():
    location = Path(__file__).with_name("scan_release.py")
    spec = importlib.util.spec_from_file_location("codex_media_ads_scan_release", location)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load release scanner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [Path(item) for item in result.stdout.decode().split("\0") if item]


def archive_files(files: Iterable[Path]) -> list[Path]:
    selected: list[Path] = []
    for path in files:
        name = path.as_posix()
        if name.startswith(_EXCLUDED_PREFIXES):
            continue
        if any(part in _EXCLUDED_PARTS for part in path.parts):
            continue
        selected.append(path)
    return sorted(selected, key=lambda item: item.as_posix())


def run_scanner(path: Path) -> list[str]:
    return _scanner_module().scan_path(path)


def build_release(root: Path | str, dist: Path | str | None = None) -> tuple[Path, Path]:
    root = Path(root).resolve()
    dist = Path(dist).resolve() if dist is not None else root / "dist"
    checkout_findings = run_scanner(root)
    if checkout_findings:
        raise RuntimeError("checkout failed release scan:\n" + "\n".join(checkout_findings))
    files = archive_files(tracked_files(root))
    for relative in files:
        source = root / relative
        if source.is_symlink():
            raise RuntimeError(f"tracked symlink cannot be included in release: {relative}")
        if not source.is_file():
            raise RuntimeError(f"tracked release path is not a regular file: {relative}")
    dist.mkdir(parents=True, exist_ok=True)
    archive = dist / ARCHIVE_NAME
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as handle:
        for relative in files:
            data = (root / relative).read_bytes()
            info = zipfile.ZipInfo(relative.as_posix(), date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            handle.writestr(info, data)
    archive_findings = run_scanner(archive)
    if archive_findings:
        raise RuntimeError("archive failed release scan:\n" + "\n".join(archive_findings))
    digest = dist / f"{ARCHIVE_NAME}.sha256"
    digest.write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n")
    return archive, digest


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", type=Path)
    parser.add_argument("--dist", type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)
    archive, digest = build_release(args.root, args.dist)
    print(f"Built {archive}")
    print(f"SHA-256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
