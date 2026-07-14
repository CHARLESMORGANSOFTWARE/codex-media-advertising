from __future__ import annotations

import os
import plistlib
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


DEFAULT_NAMESPACE = "com.codex-media-ads"
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


@dataclass(frozen=True)
class Schedule:
    calendar: Mapping[str, int] | None = None
    interval: int | None = None


class LaunchdBuilder:
    def __init__(
        self,
        *,
        executable: Path,
        working_directory: Path,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> None:
        self.executable = Path(executable).expanduser().absolute()
        self.working_directory = Path(working_directory).expanduser().absolute()
        self.namespace = namespace.rstrip(".")

    def label(self, name: str) -> str:
        if not _NAME.fullmatch(name):
            raise ValueError(
                "automation name must contain only lowercase letters, digits, and hyphens"
            )
        return f"{self.namespace}.{name}"

    @staticmethod
    def _validated_schedule(schedule: Schedule | None) -> Schedule:
        schedule = schedule or Schedule(calendar={"Hour": 9, "Minute": 0})
        if (schedule.calendar is None) == (schedule.interval is None):
            raise ValueError("configure exactly one launchd schedule type")
        if schedule.interval is not None:
            if isinstance(schedule.interval, bool) or schedule.interval < 60:
                raise ValueError("StartInterval must be at least 60 seconds")
            return schedule
        assert schedule.calendar is not None
        allowed = {"Minute", "Hour", "Day", "Weekday", "Month"}
        if not schedule.calendar or set(schedule.calendar) - allowed:
            raise ValueError("invalid StartCalendarInterval keys")
        ranges = {
            "Minute": (0, 59),
            "Hour": (0, 23),
            "Day": (1, 31),
            "Weekday": (0, 7),
            "Month": (1, 12),
        }
        for key, value in schedule.calendar.items():
            minimum, maximum = ranges[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"invalid launchd calendar value for {key}")
        return schedule

    def build(
        self,
        name: str,
        *,
        state_root: Path,
        schedule: Schedule | None = None,
    ) -> dict[str, object]:
        label = self.label(name)
        schedule = self._validated_schedule(schedule)
        state_root = Path(state_root).expanduser().absolute()
        logs = state_root / "logs"
        payload: dict[str, object] = {
            "Label": label,
            "ProgramArguments": [
                str(self.executable),
                "publish",
                "next",
                "--schedule",
                name,
                "--format",
                "json",
            ],
            "WorkingDirectory": str(self.working_directory),
            "EnvironmentVariables": {
                "CODEX_MEDIA_ADS_STATE_ROOT": str(state_root),
            },
            "StandardOutPath": str(logs / f"{name}.stdout.log"),
            "StandardErrorPath": str(logs / f"{name}.stderr.log"),
            "RunAtLoad": False,
        }
        if schedule.calendar is not None:
            payload["StartCalendarInterval"] = dict(schedule.calendar)
        else:
            payload["StartInterval"] = schedule.interval
        return payload

    def owns(
        self,
        payload: object,
        name: str,
        *,
        state_root: Path | None = None,
    ) -> bool:
        if not isinstance(payload, dict):
            return False
        environment = payload.get("EnvironmentVariables")
        if not isinstance(environment, dict) or set(environment) != {
            "CODEX_MEDIA_ADS_STATE_ROOT"
        }:
            return False
        configured_root = environment.get("CODEX_MEDIA_ADS_STATE_ROOT")
        if not isinstance(configured_root, str) or not Path(
            configured_root
        ).is_absolute():
            return False
        root = (
            Path(state_root).expanduser().absolute()
            if state_root is not None
            else Path(configured_root)
        )
        if configured_root != str(root):
            return False
        has_calendar = "StartCalendarInterval" in payload
        has_interval = "StartInterval" in payload
        if has_calendar == has_interval:
            return False
        try:
            schedule = (
                Schedule(calendar=payload["StartCalendarInterval"])
                if has_calendar
                else Schedule(interval=payload["StartInterval"])
            )
            expected = self.build(name, state_root=root, schedule=schedule)
        except (KeyError, TypeError, ValueError):
            return False
        return payload == expected


class LaunchdManager:
    def __init__(
        self,
        builder: LaunchdBuilder,
        *,
        launch_agents_dir: Path | None = None,
        run: Callable[..., object] = subprocess.run,
        uid: int | None = None,
    ) -> None:
        self.builder = builder
        self.launch_agents_dir = (
            Path(launch_agents_dir).expanduser().absolute()
            if launch_agents_dir is not None
            else (Path.home() / "Library" / "LaunchAgents").absolute()
        )
        self.run = run
        self.uid = os.getuid() if uid is None else uid

    def _path(self, name: str) -> Path:
        return self.launch_agents_dir / f"{self.builder.label(name)}.plist"

    @staticmethod
    def _write_atomic(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                plistlib.dump(payload, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise

    def install(
        self,
        name: str,
        *,
        state_root: Path,
        schedule: Schedule | None = None,
    ) -> Path:
        state_root = Path(state_root).expanduser().absolute()
        logs = state_root / "logs"
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        logs.chmod(0o700)
        path = self._path(name)
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise ValueError("existing LaunchAgent path is not plugin-owned")
            try:
                existing = plistlib.loads(path.read_bytes())
            except (OSError, ValueError, plistlib.InvalidFileException):
                raise ValueError("existing LaunchAgent path is not plugin-owned")
            if not self.builder.owns(existing, name, state_root=state_root):
                raise ValueError("existing LaunchAgent path is not plugin-owned")
            self.run(
                ["launchctl", "bootout", f"gui/{self.uid}", str(path)],
                check=False,
                capture_output=True,
                text=True,
            )
        self._write_atomic(
            path,
            self.builder.build(name, state_root=state_root, schedule=schedule),
        )
        self.run(
            ["launchctl", "bootstrap", f"gui/{self.uid}", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return path

    def list(self) -> list[str]:
        if not self.launch_agents_dir.is_dir():
            return []
        prefix = f"{self.builder.namespace}."
        names: list[str] = []
        for path in sorted(self.launch_agents_dir.glob(f"{prefix}*.plist")):
            try:
                payload = plistlib.loads(path.read_bytes())
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
            label = payload.get("Label")
            if not isinstance(label, str) or label != path.stem or not label.startswith(prefix):
                continue
            name = label.removeprefix(prefix)
            if _NAME.fullmatch(name) and self.builder.owns(payload, name):
                names.append(name)
        return names

    def remove(self, name: str, *, state_root: Path | None = None) -> bool:
        path = self._path(name)
        if not path.is_file() or path.is_symlink():
            return False
        try:
            payload = plistlib.loads(path.read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            return False
        if not self.builder.owns(payload, name, state_root=state_root):
            return False
        completed = self.run(
            ["launchctl", "bootout", f"gui/{self.uid}", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if getattr(completed, "returncode", 1) not in {0, 3, 5, 113}:
            raise subprocess.CalledProcessError(
                getattr(completed, "returncode", 1),
                ["launchctl", "bootout", f"gui/{self.uid}", str(path)],
            )
        path.unlink()
        return True

    def uninstall(self, *, state_root: Path, preserve_state: bool = True) -> list[str]:
        if not preserve_state:
            raise ValueError("state deletion requires a separate explicit operation")
        removed: list[str] = []
        for name in self.list():
            if self.remove(name, state_root=state_root):
                removed.append(name)
        return removed
