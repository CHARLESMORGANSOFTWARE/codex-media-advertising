from __future__ import annotations

import os
from pathlib import Path

import pytest

from codex_media_ads.publishing.chrome import (
    ManagedChrome,
    ProcessRecord,
    clone_profile,
    launch_chrome,
)


def make_source(root: Path) -> Path:
    root.mkdir()
    (root / "Local State").write_text('{"profile": {}}')
    profile = root / "Profile 1"
    (profile / "Network").mkdir(parents=True)
    (profile / "Network" / "Cookies").write_text("session")
    for relative in (
        "Cache/data",
        "Code Cache/js/data",
        "GPUCache/data",
        "ShaderCache/data",
        "Service Worker/CacheStorage/data",
        "Service Worker/ScriptCache/data",
        "Crashpad/settings.dat",
    ):
        path = profile / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("excluded")
    for name in ("SingletonLock", "LOCK", "lockfile"):
        (profile / name).write_text("excluded")
    (root / "Default").mkdir()
    (root / "Default" / "Cookies").write_text("wrong profile")
    (root / "SingletonCookie").write_text("excluded root")
    return root


def test_profile_clone_copies_only_selected_profile_and_excludes_runtime_data(tmp_path: Path) -> None:
    source = make_source(tmp_path / "source")
    clone = clone_profile(source, tmp_path / "private" / "clone", profile_name="Profile 1")

    assert (clone / "Local State").exists()
    assert (clone / "Profile 1" / "Network" / "Cookies").read_text() == "session"
    assert not (clone / "Default").exists()
    relative_paths = [path.relative_to(clone).as_posix() for path in clone.rglob("*")]
    assert not any("Cache" in path for path in relative_paths)
    assert not any("Crashpad" in path for path in relative_paths)
    assert not any(Path(path).name.lower() in {"singletonlock", "lock", "lockfile"} for path in relative_paths)
    assert clone.stat().st_mode & 0o777 == 0o700


def test_profile_clone_rejects_unsafe_names_and_nonempty_destinations(tmp_path: Path) -> None:
    source = make_source(tmp_path / "source")
    with pytest.raises(ValueError, match="profile name"):
        clone_profile(source, tmp_path / "clone", profile_name="../Default")
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "mine").write_text("do not replace")
    with pytest.raises(FileExistsError):
        clone_profile(source, destination, profile_name="Profile 1")
    assert (destination / "mine").read_text() == "do not replace"

    with pytest.raises(ValueError, match="outside the source"):
        clone_profile(source, source / "Profile 1" / "nested", profile_name="Profile 1")


def test_profile_clone_files_are_owner_only(tmp_path: Path) -> None:
    source = make_source(tmp_path / "source")
    clone = clone_profile(source, tmp_path / "clone", profile_name="Profile 1")
    assert (clone / "Local State").stat().st_mode & 0o777 == 0o600
    assert (clone / "Profile 1" / "Network" / "Cookies").stat().st_mode & 0o777 == 0o600


class FakeProcess:
    def __init__(self, pid: int = 700) -> None:
        self.pid = pid
        self.args: list[str] = []
        self.returncode = None

    def poll(self):
        return self.returncode


def test_launch_uses_private_clone_dynamic_loopback_cdp_and_per_run_log(tmp_path: Path) -> None:
    source = make_source(tmp_path / "source")
    captured: dict[str, object] = {}

    def popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProcess()

    managed = launch_chrome(
        chrome_path=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        profile_source=source,
        profile_name="Profile 1",
        state_root=tmp_path / "private-state",
        popen=popen,
    )

    argv = captured["argv"]
    assert "--remote-debugging-address=127.0.0.1" in argv
    port_arg = next(value for value in argv if value.startswith("--remote-debugging-port="))
    assert int(port_arg.split("=", 1)[1]) > 0
    data_arg = next(value for value in argv if value.startswith("--user-data-dir="))
    assert Path(data_arg.split("=", 1)[1]).is_relative_to(tmp_path / "private-state")
    assert managed.cdp_url == f"http://127.0.0.1:{managed.port}"
    assert managed.log_path.exists()
    assert managed.log_path.stat().st_mode & 0o777 == 0o600
    assert captured["kwargs"]["shell"] is False


def test_launch_rejects_arguments_that_override_isolation(tmp_path: Path) -> None:
    source = make_source(tmp_path / "source")
    with pytest.raises(ValueError, match="managed Chrome argument"):
        launch_chrome(
            chrome_path=Path("chrome"),
            profile_source=source,
            profile_name="Profile 1",
            state_root=tmp_path / "state",
            extra_args=("--user-data-dir=/unsafe",),
            popen=lambda *_args, **_kwargs: FakeProcess(),
        )


def test_close_terms_then_kills_only_processes_matching_clone_root(tmp_path: Path) -> None:
    clone = tmp_path / "state" / "browser-profiles" / "run-abc"
    clone.mkdir(parents=True)
    marker = f"--user-data-dir={clone.resolve()}"
    tables = iter(
        [
            [
                ProcessRecord(101, 1, f"chrome {marker}"),
                ProcessRecord(102, 101, "chrome helper"),
                ProcessRecord(999, 1, "Google Chrome --user-data-dir=/someone/else"),
                ProcessRecord(998, 1, f"Google Chrome {marker}-suffix"),
            ],
            [
                ProcessRecord(101, 1, f"chrome {marker}"),
                ProcessRecord(102, 101, "chrome helper"),
                ProcessRecord(999, 1, "Google Chrome --user-data-dir=/someone/else"),
                ProcessRecord(998, 1, f"Google Chrome {marker}-suffix"),
            ],
        ]
    )
    last = []

    def process_table():
        nonlocal last
        try:
            last = next(tables)
        except StopIteration:
            pass
        return last

    signals: list[tuple[int, int]] = []
    clock = iter([0.0, 6.0])
    chrome = ManagedChrome(
        process=FakeProcess(101),
        clone_root=clone,
        port=9222,
        log_path=tmp_path / "run.log",
        process_table=process_table,
        kill=lambda pid, sig: signals.append((pid, sig)),
        sleep=lambda _: None,
        monotonic=lambda: next(clock),
    )
    chrome.close()

    assert (101, 15) in signals and (102, 15) in signals
    assert (101, 9) in signals and (102, 9) in signals
    assert all(pid != 999 for pid, _ in signals)
    assert all(pid != 998 for pid, _ in signals)


def test_close_is_idempotent_and_never_invokes_broad_process_kill(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    signals: list[tuple[int, int]] = []
    chrome = ManagedChrome(
        process=FakeProcess(),
        clone_root=clone,
        port=1234,
        log_path=tmp_path / "run.log",
        process_table=lambda: [],
        kill=lambda pid, sig: signals.append((pid, sig)),
        sleep=lambda _: None,
        monotonic=lambda: 0.0,
    )
    chrome.close()
    chrome.close()
    assert signals == []
