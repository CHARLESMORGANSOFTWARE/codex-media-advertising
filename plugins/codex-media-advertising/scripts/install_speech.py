#!/usr/bin/env python3
"""Install the plugin's pinned local Speaches dependency."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import sysconfig
from typing import Any, Sequence


LOCK_FIELDS = {
    "schema_version",
    "git_url",
    "git_revision",
    "python_version",
    "uv_version",
    "tts_model",
    "stt_model",
}


class SpeechInstallError(ValueError):
    """Raised when the speech dependency cannot be installed safely."""


def _is_same_or_descendant(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def load_lock(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SpeechInstallError(f"cannot read lock file: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != LOCK_FIELDS:
        raise SpeechInstallError("invalid speech lock schema")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise SpeechInstallError("schema_version must be 1")
    for field in LOCK_FIELDS - {"schema_version"}:
        if not isinstance(payload[field], str) or not payload[field]:
            raise SpeechInstallError(f"{field} must be a non-empty string")
    if re.fullmatch(r"[0-9a-f]{40}", payload["git_revision"]) is None:
        raise SpeechInstallError(
            "git_revision must be a 40-character lowercase hexadecimal commit"
        )
    return payload


def validate_install_root(raw_path: str, plugin_root: Path) -> Path:
    if not raw_path:
        raise SpeechInstallError("unsafe install root: path is empty")
    path = Path(raw_path)
    if not path.is_absolute():
        raise SpeechInstallError("unsafe install root: path must be absolute")
    if path == Path(path.anchor):
        raise SpeechInstallError("unsafe install root: filesystem root is forbidden")
    if any(
        component in {"", ".", ".."}
        for component in raw_path.split("/")[1:]
    ):
        raise SpeechInstallError("unsafe install root: path is not lexical")
    lexical_plugin_root = plugin_root.absolute()
    canonical_plugin_root = plugin_root.resolve(strict=True)
    canonical_path = path.resolve(strict=False)
    if _is_same_or_descendant(
        path, lexical_plugin_root
    ) or _is_same_or_descendant(canonical_path, canonical_plugin_root):
        raise SpeechInstallError(
            "unsafe install root: path must be outside the plugin checkout"
        )
    for component in (path, *path.parents):
        if component.is_symlink():
            raise SpeechInstallError(
                f"unsafe install root: path contains symlink {component}"
            )
    return path


def _uv_executable() -> str:
    return str(Path(sysconfig.get_path("scripts")) / "uv")


def validate_managed_paths(install_root: Path) -> None:
    speech_root = install_root / "speech"
    source = speech_root / "speaches"
    for path in (speech_root, source):
        if path.is_symlink():
            raise SpeechInstallError(
                f"unsafe install root: managed path is a symlink: {path}"
            )
        if path.exists() and not path.is_dir():
            raise SpeechInstallError(
                f"unsafe install root: managed path is not a directory: {path}"
            )


def _ensure_private_install_root(install_root: Path) -> None:
    install_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not install_root.is_dir():
        raise SpeechInstallError("unsafe install root: path is not a directory")
    install_root.chmod(0o700)
    if stat.S_IMODE(install_root.stat().st_mode) != 0o700:
        raise SpeechInstallError("unsafe install root: mode must be 0700")


def build_plan(
    lock: dict[str, Any], install_root: Path
) -> list[tuple[list[str], Path | None]]:
    source_parent = install_root / "speech"
    source = source_parent / "speaches"
    revision = lock["git_revision"]
    plan: list[tuple[list[str], Path | None]] = [
        (["mkdir", "-m", "0700", "-p", str(install_root)], None),
        (["mkdir", "-p", str(source_parent)], None),
        (
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                f"uv=={lock['uv_version']}",
            ],
            None,
        ),
    ]
    if source.exists():
        plan.extend(
            [
                (
                    ["git", "remote", "set-url", "origin", lock["git_url"]],
                    source,
                ),
                (["git", "remote", "get-url", "origin"], source),
                (["git", "fetch", "--force", "origin", revision], source),
            ]
        )
    else:
        plan.append(
            (
                [
                    "git",
                    "clone",
                    "--no-checkout",
                    lock["git_url"],
                    str(source),
                ],
                None,
            )
        )
    plan.extend(
        [
            (["git", "checkout", "--detach", revision], source),
            (["git", "rev-parse", "HEAD"], source),
            (["git", "status", "--porcelain", "--untracked-files=no"], source),
            ([_uv_executable(), "python", "install", lock["python_version"]], None),
            ([_uv_executable(), "sync", "--frozen"], source),
        ]
    )
    return plan


def print_plan(plan: list[tuple[list[str], Path | None]]) -> None:
    for command, cwd in plan:
        location = f" cwd={shlex.quote(str(cwd))}" if cwd is not None else ""
        print(f"dry-run:{location} {shlex.join(command)}")
    print("dry-run: no files changed")


def execute_plan(
    plan: list[tuple[list[str], Path | None]], revision: str, git_url: str
) -> None:
    for command, cwd in plan:
        if command[:4] == ["mkdir", "-m", "0700", "-p"]:
            _ensure_private_install_root(Path(command[4]))
            continue
        if command[:2] == ["mkdir", "-p"]:
            Path(command[2]).mkdir(parents=True, exist_ok=True)
            continue
        kwargs: dict[str, object] = {"check": True}
        if cwd is not None:
            kwargs["cwd"] = cwd
        if command == ["git", "remote", "get-url", "origin"]:
            kwargs.update({"capture_output": True, "text": True})
            completed = subprocess.run(command, **kwargs)
            if completed.stdout.strip() != git_url:
                raise SpeechInstallError(
                    "managed Speaches origin does not match locked git_url"
                )
            continue
        if command == ["git", "rev-parse", "HEAD"]:
            kwargs.update({"capture_output": True, "text": True})
            completed = subprocess.run(command, **kwargs)
            if completed.stdout.strip() != revision:
                raise SpeechInstallError(
                    "checked out HEAD does not match pinned revision"
                )
            continue
        if command == ["git", "status", "--porcelain", "--untracked-files=no"]:
            kwargs.update({"capture_output": True, "text": True})
            completed = subprocess.run(command, **kwargs)
            if completed.stdout.strip():
                raise SpeechInstallError(
                    "managed Speaches checkout has tracked changes; refusing to sync"
                )
            continue
        subprocess.run(command, **kwargs)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install pinned local Speaches dependencies"
    )
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--install-root", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--validate-only", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        lock = load_lock(args.lock)
        plugin_root = Path(__file__).resolve().parents[1]
        install_root = validate_install_root(args.install_root, plugin_root)
        validate_managed_paths(install_root)
        if args.validate_only:
            return 0
        plan = build_plan(lock, install_root)
        if args.dry_run:
            print_plan(plan)
            return 0
        execute_plan(plan, lock["git_revision"], lock["git_url"])
        return 0
    except subprocess.CalledProcessError as exc:
        print(
            f"speech install command failed ({exc.returncode}): "
            f"{shlex.join(str(part) for part in exc.cmd)}",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"speech install error: {exc}", file=sys.stderr)
        return 1
    except SpeechInstallError as exc:
        print(f"speech install error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
