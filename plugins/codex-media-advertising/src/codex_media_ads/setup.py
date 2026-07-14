from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping

from .config import PRIVATE_MODES, SECRET_FILE_MODE
from .models import PublishResult, PublishStatus
from .publishing.base import ProbeResult, probe_identity


CheckState = str


@dataclass(frozen=True)
class SetupCheck:
    name: str
    status: CheckState
    detail: str = ""


@dataclass(frozen=True)
class ChannelSetup:
    name: str
    status: CheckState
    background_enabled: bool
    expected_identity: str
    detail: str = ""


@dataclass(frozen=True)
class SetupResult:
    checks: dict[str, SetupCheck]
    channels: dict[str, ChannelSetup]
    config_path: Path


class SecretImportError(ValueError):
    """Raised when a credential cannot be copied without following links."""


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_MODES)
    path.parent.chmod(PRIVATE_MODES)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _default_chrome() -> Path | None:
    candidates = (
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    )
    return next((path for path in candidates if path.is_file()), None)


def _default_playwright_browser() -> Path | None:
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    roots = [Path(configured).expanduser()] if configured else []
    roots.append(Path.home() / "Library" / "Caches" / "ms-playwright")
    for root in roots:
        if root.is_dir() and any(root.glob("chromium-*")):
            return root
    return None


def _command_path(name: str) -> Path | None:
    value = shutil.which(name)
    return Path(value).resolve() if value else None


def _default_tools() -> dict[str, Path | None]:
    return {
        "python": Path(sys.executable).resolve(),
        "ffmpeg": _command_path("ffmpeg"),
        "ffprobe": _command_path("ffprobe"),
        "chrome": _default_chrome(),
        "playwright_browser": _default_playwright_browser(),
        "codimage": _command_path("codimage"),
        "narration": None,
    }


def _is_nonsecret_configuration(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            allows_path = normalized.endswith("_file") or normalized.endswith("_path")
            if not allows_path and any(
                marker in normalized
                for marker in ("token", "secret", "password", "cookie", "api_key")
            ):
                return False
            if not _is_nonsecret_configuration(item):
                return False
    elif isinstance(value, (list, tuple)):
        return all(_is_nonsecret_configuration(item) for item in value)
    return True


class SetupService:
    """Rerunnable setup checks and conservative background enablement gates."""

    def __init__(
        self,
        state_root: Path,
        *,
        tool_paths: Mapping[str, Path | str | None] | None = None,
        probes: Mapping[str, ProbeResult | Callable[[], ProbeResult]] | None = None,
        dry_runs: Mapping[str, Callable[[], PublishResult]] | None = None,
        render_probe: Callable[[], bool] | None = None,
        run: Callable[..., object] = subprocess.run,
        python_version: tuple[int, int] | None = None,
    ) -> None:
        self.state_root = Path(state_root).expanduser().absolute()
        defaults = _default_tools()
        if tool_paths:
            defaults.update(
                {
                    name: Path(path).expanduser().absolute() if path is not None else None
                    for name, path in tool_paths.items()
                }
            )
        self.tool_paths = defaults
        self.probes = dict(probes or {})
        self.dry_runs = dict(dry_runs or {})
        self.render_probe = render_probe
        self.run = run
        self.python_version = python_version or (sys.version_info.major, sys.version_info.minor)

    def _state_check(self) -> SetupCheck:
        try:
            self.state_root.mkdir(parents=True, exist_ok=True, mode=PRIVATE_MODES)
            self.state_root.chmod(PRIVATE_MODES)
            probe = self.state_root / ".setup-write-probe"
            descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            probe.unlink()
        except OSError:
            return SetupCheck(
                "writable_private_state", "blocked", "Private state is not writable."
            )
        return SetupCheck("writable_private_state", "ok")

    def _tool_check(self, name: str, output_name: str | None = None) -> SetupCheck:
        output_name = output_name or name
        path = self.tool_paths.get(name)
        if path is None or not Path(path).exists():
            return SetupCheck(output_name, "missing", f"{output_name} is not installed.")
        return SetupCheck(output_name, "ok")

    def _probe_for(self, channel: str) -> ProbeResult | None:
        value = self.probes.get(channel)
        if value is None:
            return None
        try:
            return value() if callable(value) else value
        except Exception:
            return ProbeResult(authenticated=False)

    def run_checks(self, *, enabled: list[str] | tuple[str, ...] = ()) -> dict[str, SetupCheck]:
        python_check = self._tool_check("python")
        if python_check.status == "ok" and self.python_version < (3, 11):
            python_check = SetupCheck(
                "python", "blocked", "Python 3.11 or newer is required."
            )
        checks = {
            "python": python_check,
            "ffmpeg": self._tool_check("ffmpeg"),
            "ffprobe": self._tool_check("ffprobe"),
            "chrome": self._tool_check("chrome"),
            "playwright_browser": self._tool_check("playwright_browser"),
            "codimage": self._tool_check("codimage"),
            "narration_provider": self._tool_check(
                "narration", "narration_provider"
            ),
            "writable_private_state": self._state_check(),
        }
        for channel in enabled:
            probe = self._probe_for(channel)
            if probe is None:
                status, detail = "missing", "The enabled adapter is not configured."
            elif not probe.authenticated:
                status, detail = "blocked", "The enabled adapter is not authenticated."
            else:
                status, detail = "ok", ""
            checks[f"adapter:{channel}"] = SetupCheck(
                f"adapter:{channel}", status, detail
            )
        return checks

    def _synthetic_render(self) -> bool:
        if self.render_probe is not None:
            try:
                return bool(self.render_probe())
            except Exception:
                return False
        ffmpeg = self.tool_paths.get("ffmpeg")
        if ffmpeg is None or not Path(ffmpeg).exists():
            return False
        health = self.state_root / "health"
        health.mkdir(parents=True, exist_ok=True, mode=PRIVATE_MODES)
        health.chmod(PRIVATE_MODES)
        with tempfile.TemporaryDirectory(dir=health) as temporary:
            output = Path(temporary) / "setup-render.mp4"
            try:
                completed = self.run(
                    [
                        str(ffmpeg),
                        "-nostdin",
                        "-loglevel",
                        "error",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        "color=c=black:s=16x16:d=0.1",
                        "-pix_fmt",
                        "yuv420p",
                        str(output),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            except OSError:
                return False
            return getattr(completed, "returncode", 1) == 0 and output.is_file()

    @staticmethod
    def _dry_run_safe(result: PublishResult) -> bool:
        return (
            result.status == PublishStatus.SKIPPED
            and result.evidence.get("dry_run") is True
            and result.evidence.get("final_action_skipped", True) is True
        )

    def configure(
        self,
        *,
        enabled: list[str] | tuple[str, ...],
        channels: Mapping[str, Mapping[str, object]] | None = None,
    ) -> SetupResult:
        channels = dict(channels or {})
        if not _is_nonsecret_configuration(channels):
            raise ValueError("channel configuration contains secret-bearing keys")
        checks = self.run_checks(enabled=enabled)
        rendered = self._synthetic_render()
        checks["synthetic_ffmpeg_render"] = SetupCheck(
            "synthetic_ffmpeg_render",
            "ok" if rendered else "blocked",
            "" if rendered else "The synthetic FFmpeg render did not pass.",
        )
        dependencies_ok = all(check.status == "ok" for check in checks.values())
        configured: dict[str, ChannelSetup] = {}
        for name in enabled:
            settings = channels.get(name, {})
            expected = str(settings.get("expected_identity", "")).strip()
            probe = self._probe_for(name)
            authenticated = bool(probe and probe.authenticated)
            identity_ok = bool(
                probe and probe_identity(expected, probe.observed_identity).ok
            )
            dry_run = self.dry_runs.get(name)
            try:
                dry_run_ok = bool(dry_run and self._dry_run_safe(dry_run()))
            except Exception:
                dry_run_ok = False
            enabled_in_background = (
                dependencies_ok
                and rendered
                and authenticated
                and identity_ok
                and dry_run_ok
            )
            configured[name] = ChannelSetup(
                name=name,
                status="ok" if enabled_in_background else "blocked",
                background_enabled=enabled_in_background,
                expected_identity=expected,
                detail=(
                    ""
                    if enabled_in_background
                    else "Render, authentication, exact identity, and dry-run gates must pass."
                ),
            )

        payload = {
            "schema_version": 1,
            "channels": {
                name: {
                    **dict(channels.get(name, {})),
                    "background_enabled": result.background_enabled,
                }
                for name, result in configured.items()
            },
        }
        config_path = self.state_root / "config" / "setup.json"
        _atomic_write(
            config_path,
            (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(),
            SECRET_FILE_MODE,
        )
        return SetupResult(checks=checks, channels=configured, config_path=config_path)

    def import_secret(self, source: Path, destination_name: str) -> Path:
        source = Path(source).expanduser().absolute()
        if Path(destination_name).name != destination_name or destination_name in {"", ".", ".."}:
            raise SecretImportError("secret destination must be a single filename")
        try:
            if source.resolve(strict=True) != source:
                raise SecretImportError("secret source path cannot contain a symlink")
            info = source.lstat()
        except SecretImportError:
            raise
        except OSError as exc:
            raise SecretImportError("secret source is missing") from exc
        if stat.S_ISLNK(info.st_mode):
            raise SecretImportError("secret source cannot be a symlink")
        if not stat.S_ISREG(info.st_mode):
            raise SecretImportError("secret source must be a regular file")

        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(source, flags)
            with os.fdopen(descriptor, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise SecretImportError("secret source could not be copied safely") from exc

        secrets_root = self.state_root / "secrets"
        destination = secrets_root / destination_name
        _atomic_write(destination, data, SECRET_FILE_MODE)
        return destination


def result_payload(result: SetupResult) -> dict[str, object]:
    ready = all(check.status == "ok" for check in result.checks.values()) and all(
        channel.background_enabled for channel in result.channels.values()
    )
    return {
        "ok": ready,
        "status": "ready" if ready else "blocked",
        "checks": {name: asdict(check) for name, check in result.checks.items()},
        "channels": {name: asdict(channel) for name, channel in result.channels.items()},
        "config_path": str(result.config_path),
    }
