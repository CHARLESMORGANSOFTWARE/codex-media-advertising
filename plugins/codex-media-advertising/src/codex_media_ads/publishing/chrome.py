from __future__ import annotations

import http.client
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence

from ..config import PRIVATE_MODES, SECRET_FILE_MODE
from .base import redact_diagnostic


_EXCLUDED_DIRECTORIES = {
    "cache",
    "code cache",
    "gpucache",
    "grshadercache",
    "shadercache",
    "dawncache",
    "cachestorage",
    "scriptcache",
    "crashpad",
}
_EXCLUDED_FILES = {"lock", "lockfile"}


def _excluded(path: Path) -> bool:
    name = path.name
    lowered = name.casefold()
    if path.is_symlink():
        return True
    if (
        lowered in _EXCLUDED_DIRECTORIES
        or lowered.endswith("cache")
        or "shader" in lowered
    ):
        return True
    if lowered in _EXCLUDED_FILES or lowered.endswith(".lock"):
        return True
    if lowered.startswith("singleton"):
        return True
    return False


def _copy_profile_tree(source: Path, destination: Path) -> None:
    destination.mkdir(mode=PRIVATE_MODES, parents=True, exist_ok=False)
    destination.chmod(PRIVATE_MODES)
    for entry in source.iterdir():
        if _excluded(entry):
            continue
        target = destination / entry.name
        if entry.is_dir():
            _copy_profile_tree(entry, target)
        elif entry.is_file():
            shutil.copyfile(entry, target, follow_symlinks=False)
            target.chmod(SECRET_FILE_MODE)


def clone_profile(profile_source: Path, destination: Path, *, profile_name: str) -> Path:
    """Copy auth-bearing Chrome state without transient caches or runtime locks."""

    if not profile_name or Path(profile_name).name != profile_name or profile_name in {".", ".."}:
        raise ValueError("profile name must be one directory name")
    source_root = Path(profile_source).expanduser().resolve(strict=True)
    destination = Path(destination).expanduser().resolve()
    if _is_within(destination, source_root):
        raise ValueError("profile clone destination must remain outside the source")
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"profile clone destination is not empty: {destination}")
    local_state = source_root / "Local State"
    selected_profile = source_root / profile_name
    if local_state.is_symlink() or not local_state.is_file():
        raise FileNotFoundError(f"Chrome Local State is unavailable: {local_state}")
    if selected_profile.is_symlink() or not selected_profile.is_dir():
        raise FileNotFoundError(f"Chrome profile is unavailable: {selected_profile}")

    destination.mkdir(mode=PRIVATE_MODES, parents=True, exist_ok=True)
    destination.chmod(PRIVATE_MODES)
    shutil.copyfile(local_state, destination / "Local State", follow_symlinks=False)
    (destination / "Local State").chmod(SECRET_FILE_MODE)
    _copy_profile_tree(selected_profile, destination / profile_name)
    return destination


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    parent_pid: int
    command: str
    process_group_id: int | None = None
    session_id: int | None = None
    state: str = ""
    start_token: str = ""


def _system_process_table() -> list[ProcessRecord]:
    bsd_systems = {"Darwin", "FreeBSD", "OpenBSD", "NetBSD"}
    session_keyword = "sess" if platform.system() in bsd_systems else "sid"
    completed = subprocess.run(
        [
            "ps",
            "-axo",
            f"pid=,ppid=,pgid=,{session_keyword}=,state=,lstart=,command=",
        ],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        diagnostic = redact_diagnostic(completed.stderr.strip())
        if not diagnostic:
            diagnostic = "no ps diagnostic was returned"
        raise RuntimeError(
            f"process discovery failed (ps exit {completed.returncode}): {diagnostic}"
        )
    records: list[ProcessRecord] = []
    for line in completed.stdout.splitlines():
        parts = line.strip().split(maxsplit=10)
        if len(parts) != 11:
            continue
        try:
            records.append(
                ProcessRecord(
                    pid=int(parts[0]),
                    parent_pid=int(parts[1]),
                    command=parts[10],
                    process_group_id=int(parts[2]),
                    session_id=int(parts[3]),
                    state=parts[4],
                    start_token=" ".join(parts[5:10]),
                )
            )
        except ValueError:
            continue
    return records


def _matching_processes(records: Iterable[ProcessRecord], clone_root: Path) -> set[int]:
    records = tuple(records)
    marker = f"--user-data-dir={clone_root.resolve()}"
    marker_pattern = re.compile(
        rf"(?:(?<=[\s'\"])|^){re.escape(marker)}(?=[\s'\"]|$)"
    )
    roots = {
        record.pid for record in records if marker_pattern.search(record.command) is not None
    }
    family = set(roots)
    changed = True
    while changed:
        changed = False
        for record in records:
            if record.parent_pid in family and record.pid not in family:
                family.add(record.pid)
                changed = True
    return family


class PopenLike(Protocol):
    pid: int

    def poll(self) -> int | None: ...


@dataclass
class ManagedChrome:
    process: PopenLike
    clone_root: Path
    port: int
    log_path: Path
    process_table: Callable[[], Iterable[ProcessRecord]] = _system_process_table
    get_process_group: Callable[[int], int] = os.getpgid
    get_session: Callable[[int], int] = os.getsid
    kill_group: Callable[[int, int], None] = os.killpg
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    _log_handle: object | None = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _owned_process_group: int | None = field(default=None, init=False, repr=False)
    _owned_session: int | None = field(default=None, init=False, repr=False)
    _root_start_token: str = field(default="", init=False, repr=False)

    @property
    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            # Popen.poll() returning None proves this is still our unreaped child,
            # so its PID cannot have been recycled. start_new_session=True makes
            # that PID the uniquely owned process-group ID.
            process_group = self._establish_owned_identity()
            if process_group is None:
                return
            try:
                self.kill_group(process_group, signal.SIGTERM)
            except ProcessLookupError:
                return
            deadline = self.monotonic() + 5.0
            remaining: tuple[ProcessRecord, ...] = ()
            while True:
                remaining = self._remaining_owned_members()
                if not remaining:
                    # Reap the owned root only after no live group members remain.
                    self.process.poll()
                    return
                if self.monotonic() >= deadline:
                    break
                self.sleep(0.1)
            self._verify_owned_anchor()
            try:
                self.kill_group(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        finally:
            if self._log_handle is not None:
                close = getattr(self._log_handle, "close", None)
                if close is not None:
                    close()

    def _establish_owned_identity(self) -> int | None:
        if self.process.poll() is not None:
            return None
        records = tuple(self.process_table())
        matching = _matching_processes(records, self.clone_root)
        root = next((record for record in records if record.pid == self.process.pid), None)
        if self.process.pid not in matching or root is None:
            raise RuntimeError(
                "refusing to signal Chrome because clone process identity changed"
            )
        process_group = self.get_process_group(self.process.pid)
        if process_group != self.process.pid:
            raise RuntimeError(
                "refusing to signal Chrome because process group identity changed"
            )
        session = self.get_session(self.process.pid)
        if session != self.process.pid:
            raise RuntimeError(
                "refusing to signal Chrome because session identity changed"
            )
        if root.process_group_id not in (None, process_group):
            raise RuntimeError("Chrome process table group identity is inconsistent")
        if root.session_id not in (None, session):
            raise RuntimeError("Chrome process table session identity is inconsistent")
        self._owned_process_group = process_group
        self._owned_session = session
        self._root_start_token = root.start_token
        return process_group

    def _owned_snapshot(self) -> tuple[ProcessRecord, ...]:
        if self._owned_process_group is None or self._owned_session is None:
            raise RuntimeError("Chrome process ownership was not established")
        return tuple(
            record
            for record in self.process_table()
            if record.process_group_id == self._owned_process_group
            and record.session_id == self._owned_session
        )

    def _verify_owned_anchor(self) -> tuple[ProcessRecord, ...]:
        records = self._owned_snapshot()
        root = next((record for record in records if record.pid == self.process.pid), None)
        if root is None:
            raise RuntimeError("refusing to signal Chrome because owned root anchor vanished")
        if self._root_start_token and root.start_token != self._root_start_token:
            raise RuntimeError(
                "refusing to signal Chrome because root start identity changed"
            )
        if root.state.upper().startswith("Z"):
            if not self._root_start_token:
                raise RuntimeError(
                    "refusing to signal Chrome helpers without a stable root start identity"
                )
        elif self.process.pid not in _matching_processes(records, self.clone_root):
            raise RuntimeError(
                "refusing to signal Chrome because live root identity changed"
            )
        return records

    def _remaining_owned_members(self) -> tuple[ProcessRecord, ...]:
        records = self._verify_owned_anchor()
        return tuple(
            record for record in records if not record.state.upper().startswith("Z")
        )

    def __enter__(self) -> ManagedChrome:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _probe_loopback_cdp(port: int) -> bool:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.2)
    try:
        connection.request("GET", "/json/version")
        response = connection.getresponse()
        if response.status != 200:
            return False
        payload = json.loads(response.read().decode("utf-8"))
        return bool(payload.get("webSocketDebuggerUrl"))
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def _bounded_log_tail(path: Path, limit: int = 4096) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - limit)
            handle.seek(start)
            raw = handle.read(limit)
    except OSError as exc:
        return f"log unavailable: {redact_diagnostic(str(exc))}"
    if start:
        newline = raw.find(b"\n")
        raw = b"" if newline < 0 else raw[newline + 1 :]
    return redact_diagnostic(raw.decode("utf-8", errors="replace"))


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def launch_chrome(
    *,
    chrome_path: Path,
    profile_source: Path,
    profile_name: str,
    state_root: Path,
    extra_args: Sequence[str] = (),
    popen: Callable[..., PopenLike] = subprocess.Popen,
    startup_timeout: float = 15.0,
    cdp_probe: Callable[[int], bool] = _probe_loopback_cdp,
    process_table: Callable[[], Iterable[ProcessRecord]] = _system_process_table,
    get_process_group: Callable[[int], int] = os.getpgid,
    get_session: Callable[[int], int] = os.getsid,
    kill_group: Callable[[int, int], None] = os.killpg,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> ManagedChrome:
    """Launch a single isolated Chrome run using only private state-owned paths."""

    managed_prefixes = (
        "--user-data-dir",
        "--profile-directory",
        "--remote-debugging-address",
        "--remote-debugging-port",
    )
    if any(str(argument).startswith(managed_prefixes) for argument in extra_args):
        raise ValueError("extra_args contains a managed Chrome argument")
    if startup_timeout <= 0:
        raise ValueError("startup_timeout must be positive")
    state_root = Path(state_root).expanduser().resolve()
    state_root.mkdir(mode=PRIVATE_MODES, parents=True, exist_ok=True)
    state_root.chmod(PRIVATE_MODES)
    run_id = uuid.uuid4().hex
    clone_root = (state_root / "browser-profiles" / f"run-{run_id}").resolve()
    logs_root = (state_root / "logs" / "chrome").resolve()
    if not _is_within(clone_root, state_root) or not _is_within(logs_root, state_root):
        raise ValueError("Chrome run paths must remain inside private state")
    logs_root.mkdir(mode=PRIVATE_MODES, parents=True, exist_ok=True)
    logs_root.chmod(PRIVATE_MODES)
    clone_profile(profile_source, clone_root, profile_name=profile_name)

    port = _loopback_port()
    log_path = logs_root / f"run-{run_id}.log"
    log_fd = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, SECRET_FILE_MODE)
    log_handle = os.fdopen(log_fd, "wb", buffering=0)
    argv = [
        str(Path(chrome_path).expanduser()),
        f"--user-data-dir={clone_root}",
        f"--profile-directory={profile_name}",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        *extra_args,
    ]
    try:
        process = popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            shell=False,
            start_new_session=True,
            close_fds=True,
        )
    except BaseException:
        log_handle.close()
        raise
    managed = ManagedChrome(
        process=process,
        clone_root=clone_root,
        port=port,
        log_path=log_path,
        process_table=process_table,
        get_process_group=get_process_group,
        get_session=get_session,
        kill_group=kill_group,
        sleep=sleep,
        monotonic=monotonic,
        _log_handle=log_handle,
    )
    deadline = monotonic() + startup_timeout
    failure = ""
    while True:
        returncode = process.poll()
        if returncode is not None:
            failure = f"Chrome exited with {returncode} before CDP became ready"
            break
        if cdp_probe(port):
            return managed
        if monotonic() >= deadline:
            failure = f"Chrome CDP startup timed out after {startup_timeout:g} seconds"
            break
        sleep(0.1)

    cleanup_detail = ""
    try:
        managed.close()
    except Exception as exc:
        cleanup_detail = f"; cleanup={redact_diagnostic(str(exc))}"
    diagnostic = _bounded_log_tail(log_path)
    safe_log_path = redact_diagnostic(str(log_path))
    raise RuntimeError(
        f"{failure}; log={safe_log_path}; diagnostic={diagnostic}{cleanup_detail}"
    )
