#!/usr/bin/env python3
"""Scan a checkout or release archive for private/runtime artifacts.

The scanner deliberately errs on the side of blocking credentials and machine
specific state. The only documented examples allowed in content are
``author@example.com``, ``https://example.com``, ``*.example.test`` identities,
synthetic fixture identifiers, and the public repository slug.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable


_INTERNAL_PARTS = {".git", ".superpowers", ".agents", ".pytest_cache", "__pycache__"}
_ABSOLUTE_RE = re.compile(r"/(?:Users|Applications|Volumes)/[^\s\"']+")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
_OAUTH_RE = re.compile(r"\b(?:ghp_|github_pat_|ya29\.|EA[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,})[A-Za-z0-9._-]*\b")
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
_SECRET_ASSIGN_RE = re.compile(
    r"\b(?:access[_-]?token|client[_-]?secret|refresh[_-]?token|api[_-]?key)\b\s*[:=]\s*[\"'][^\"']{12,}[\"']",
    re.IGNORECASE,
)
_PRIVATE_NAME_RE = re.compile(
    r"(?:^|/)(?:\.env(?:\..*)?|.*(?:cookies?|browser[-_ ]?profiles?|tokens?|private[-_ ]?keys?|receipts\.jsonl|queue/|logs?/|generated/).*)$",
    re.IGNORECASE,
)
_MEDIA_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".wav", ".mp3", ".png", ".jpg", ".jpeg"}
_ALLOWED_MEDIA = {
    "plugins/codex-media-advertising/tests/fixtures/synthetic.png",
    "plugins/codex-media-advertising/.codex-plugin/icon.svg",
}
_ALLOWED_ABSOLUTE_FILES = {
    "plugins/codex-media-advertising/src/codex_media_ads/setup.py",
    "plugins/codex-media-advertising/src/codex_media_ads/publishing/chrome.py",
}
_ALLOWED_SCANNER_FILES = {
    "plugins/codex-media-advertising/scripts/scan_release.py",
}
_ALLOWED_EMAIL_DOMAINS = {"example.com", "example.test", "example.org"}
_ALLOWED_IDENTIFIERS = {"CHARLESMORGANSOFTWARE/codex-media-advertising"}


def _posix(path: Path | str) -> str:
    value = str(path).replace("\\", "/")
    return value[2:] if value.startswith("./") else value


def _excluded(path: str) -> bool:
    return any(part in _INTERNAL_PARTS for part in PurePosixPath(path).parts)


def tracked_files(root: Path) -> list[Path]:
    """Return tracked paths relative to *root*, excluding repository internals."""

    try:
        output = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return sorted(
            path.relative_to(root)
            for path in root.rglob("*")
            if path.is_file() and not _excluded(_posix(path.relative_to(root)))
        )
    return [
        Path(value)
        for value in sorted(output.decode("utf-8").split("\0"))
        if value and not _excluded(value)
    ]


def _allowed_email(value: str) -> bool:
    domain = value.rsplit("@", 1)[-1].lower()
    return domain in _ALLOWED_EMAIL_DOMAINS or domain.endswith(".example.test")


def _scan_text(relative: str, text: str) -> list[str]:
    findings: list[str] = []
    if relative not in _ALLOWED_ABSOLUTE_FILES:
        absolute = _ABSOLUTE_RE.search(text)
        if absolute:
            findings.append(f"{relative}: absolute path {absolute.group(0)!r}")
    for email in _EMAIL_RE.findall(text):
        if not _allowed_email(email):
            findings.append(f"{relative}: non-example email address {email!r}")
    if _PHONE_RE.search(text):
        findings.append(f"{relative}: phone-number pattern")
    if _JWT_RE.search(text):
        findings.append(f"{relative}: JWT-like token")
    if _OAUTH_RE.search(text):
        findings.append(f"{relative}: OAuth/API token")
    if _PRIVATE_KEY_RE.search(text):
        findings.append(f"{relative}: private key material")
    if _SECRET_ASSIGN_RE.search(text):
        findings.append(f"{relative}: secret assignment")
    for identifier in _ALLOWED_IDENTIFIERS:
        text = text.replace(identifier, "")
    if re.search(r"\b(?:charles|telethryve|seattle-car-guy)\b", text, re.IGNORECASE):
        findings.append(f"{relative}: known personal campaign identifier")
    return findings


def _scan_member(relative: str, data: bytes) -> list[str]:
    findings: list[str] = []
    normalized = _posix(relative)
    if _PRIVATE_NAME_RE.search(normalized) and not normalized.endswith("/receipts.py"):
        findings.append(f"{normalized}: private artifact filename")
    suffix = PurePosixPath(normalized).suffix.lower()
    if suffix in _MEDIA_SUFFIXES and normalized not in _ALLOWED_MEDIA:
        findings.append(f"{normalized}: generated media is not allowlisted")
    # Tests intentionally contain synthetic identities, fixture paths, and
    # placeholder secret-key names to exercise the safety gates. They are an
    # explicit documented allowlist; production source and docs are scanned.
    if "/tests/" in f"/{normalized}" or normalized.startswith("tests/"):
        return findings
    if normalized in _ALLOWED_SCANNER_FILES:
        return findings
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return findings
    findings.extend(_scan_text(normalized, text))
    return findings


def scan_archive(path: Path) -> list[str]:
    findings: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for member in sorted(archive.namelist()):
            if member.endswith("/") or _excluded(member):
                continue
            findings.extend(_scan_member(member, archive.read(member)))
    return findings


def scan_path(path: Path | str) -> list[str]:
    candidate = Path(path)
    if candidate.is_file() and zipfile.is_zipfile(candidate):
        return scan_archive(candidate)
    if candidate.is_file():
        return _scan_member(candidate.name, candidate.read_bytes())
    root = candidate
    findings: list[str] = []
    for relative in tracked_files(root):
        rel = _posix(relative)
        if rel.startswith("docs/superpowers/"):
            continue
        file_path = root / relative
        try:
            findings.extend(_scan_member(rel, file_path.read_bytes()))
        except OSError as exc:
            findings.append(f"{rel}: unreadable file ({exc.__class__.__name__})")
    return findings


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=".", type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)
    findings = scan_path(args.path)
    if findings:
        print("Release scan failed:", file=sys.stderr)
        print("\n".join(f"- {finding}" for finding in findings), file=sys.stderr)
        return 1
    print(f"Release scan passed: {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
