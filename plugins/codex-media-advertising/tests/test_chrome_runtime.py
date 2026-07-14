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


def test_safe_chrome_api_is_exported_from_publishing_package() -> None:
    from codex_media_ads.publishing import ManagedChrome as ExportedChrome
    from codex_media_ads.publishing import clone_profile as exported_clone

    assert ExportedChrome is ManagedChrome
    assert exported_clone is clone_profile


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
        "DawnGraphiteCache/data",
        "DawnWebGPUCache/data",
        "GraphiteDawnCache/data",
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
        cdp_probe=lambda _port: True,
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
        kill_group=lambda pgid, sig: signals.append((pgid, sig)),
        get_process_group=lambda pid: pid,
        sleep=lambda _: None,
        monotonic=lambda: next(clock),
    )
    chrome.close()

    assert signals == [(101, 15), (101, 9)]


def test_close_is_idempotent_and_never_invokes_broad_process_kill(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    signals: list[tuple[int, int]] = []
    process = FakeProcess()
    process.returncode = 0
    chrome = ManagedChrome(
        process=process,
        clone_root=clone,
        port=1234,
        log_path=tmp_path / "run.log",
        process_table=lambda: [],
        kill_group=lambda pgid, sig: signals.append((pgid, sig)),
        get_process_group=lambda pid: pid,
        sleep=lambda _: None,
        monotonic=lambda: 0.0,
    )
    chrome.close()
    chrome.close()
    assert signals == []


def test_close_refuses_stale_snapshot_pid_and_never_signals_reused_process(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    marker = f"--user-data-dir={clone.resolve()}"
    process = FakeProcess(401)
    signals: list[tuple[int, int]] = []
    chrome = ManagedChrome(
        process=process,
        clone_root=clone,
        port=1234,
        log_path=tmp_path / "run.log",
        process_table=lambda: [ProcessRecord(401, 1, f"chrome {marker}")],
        get_process_group=lambda _pid: 777,
        kill_group=lambda pgid, sig: signals.append((pgid, sig)),
    )
    with pytest.raises(RuntimeError, match="process group identity"):
        chrome.close()
    assert signals == []


def test_close_closes_log_and_remains_idempotent_when_discovery_raises(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    log = (tmp_path / "run.log").open("wb")
    chrome = ManagedChrome(
        process=FakeProcess(401),
        clone_root=clone,
        port=1234,
        log_path=tmp_path / "run.log",
        process_table=lambda: (_ for _ in ()).throw(OSError("ps failed")),
        get_process_group=lambda pid: pid,
        kill_group=lambda _pgid, _sig: None,
        _log_handle=log,
    )
    with pytest.raises(OSError, match="ps failed"):
        chrome.close()
    assert log.closed
    chrome.close()


def test_close_closes_log_and_remains_idempotent_when_group_signal_raises(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    marker = f"--user-data-dir={clone.resolve()}"
    log = (tmp_path / "run.log").open("wb")
    chrome = ManagedChrome(
        process=FakeProcess(401),
        clone_root=clone,
        port=1234,
        log_path=tmp_path / "run.log",
        process_table=lambda: [ProcessRecord(401, 1, f"chrome {marker}")],
        get_process_group=lambda pid: pid,
        kill_group=lambda _pgid, _sig: (_ for _ in ()).throw(OSError("signal failed")),
        _log_handle=log,
    )
    with pytest.raises(OSError, match="signal failed"):
        chrome.close()
    assert log.closed
    chrome.close()


def test_launch_waits_for_cdp_and_reports_redacted_bounded_log_on_early_exit(tmp_path: Path) -> None:
    source = make_source(tmp_path / "source")

    def popen(_argv, **kwargs):
        kwargs["stdout"].write(
            b"Authorization: Bearer bearer-secret remainder\n"
            + b"x" * 9000
            + b'\n{"access_token":"access-secret"}\n'
        )
        process = FakeProcess()
        process.returncode = 17
        return process

    with pytest.raises(RuntimeError) as captured:
        launch_chrome(
            chrome_path=Path("chrome"),
            profile_source=source,
            profile_name="Profile 1",
            state_root=tmp_path / "state",
            popen=popen,
            cdp_probe=lambda _port: False,
            startup_timeout=1,
        )
    message = str(captured.value)
    assert "exited with 17" in message
    assert "bearer-secret" not in message
    assert "access-secret" not in message
    assert len(message) < 6000


def test_launch_times_out_when_cdp_never_becomes_ready(tmp_path: Path) -> None:
    source = make_source(tmp_path / "source")
    clock = iter([0.0, 0.0, 2.0])
    with pytest.raises(RuntimeError, match="CDP startup timed out"):
        launch_chrome(
            chrome_path=Path("chrome"),
            profile_source=source,
            profile_name="Profile 1",
            state_root=tmp_path / "state",
            popen=lambda *_args, **_kwargs: FakeProcess(),
            cdp_probe=lambda _port: False,
            startup_timeout=1,
            monotonic=lambda: next(clock),
            sleep=lambda _seconds: None,
            process_table=lambda: [],
        )
