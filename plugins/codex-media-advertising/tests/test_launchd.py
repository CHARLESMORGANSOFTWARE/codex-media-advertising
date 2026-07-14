from __future__ import annotations

import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_media_ads.automation.launchd import (
    LaunchdBuilder,
    LaunchdManager,
    Schedule,
)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, args: list[str], **kwargs):
        self.calls.append((list(args), kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _builder(tmp_path: Path) -> LaunchdBuilder:
    executable = tmp_path / "venv" / "bin" / "codex-media-ads"
    executable.parent.mkdir(parents=True)
    executable.touch()
    workdir = tmp_path / "plugin"
    workdir.mkdir()
    return LaunchdBuilder(executable=executable, working_directory=workdir)


def test_launchagent_uses_absolute_cli_and_state_paths(tmp_path: Path) -> None:
    plist = _builder(tmp_path).build("daily-short", state_root=tmp_path / "state")
    args = plist["ProgramArguments"]

    assert Path(args[0]).is_absolute()
    assert args[1:] == [
        "publish",
        "next",
        "--schedule",
        "daily-short",
        "--format",
        "json",
    ]
    assert Path(plist["WorkingDirectory"]).is_absolute()
    assert Path(plist["StandardOutPath"]).is_absolute()
    assert Path(plist["StandardErrorPath"]).is_absolute()
    assert Path(plist["EnvironmentVariables"]["CODEX_MEDIA_ADS_STATE_ROOT"]).is_absolute()
    assert "StartCalendarInterval" in plist
    assert plist["RunAtLoad"] is False
    assert plist["Label"] == "com.codex-media-ads.daily-short"


@pytest.mark.parametrize(
    "schedule",
    [
        Schedule(calendar={"Hour": 24, "Minute": 0}),
        Schedule(calendar={"Hour": 8, "Minute": 60}),
        Schedule(interval=59),
        Schedule(calendar={"Hour": 8}, interval=3600),
    ],
)
def test_schedule_validation_rejects_unsafe_values(
    tmp_path: Path, schedule: Schedule
) -> None:
    with pytest.raises(ValueError):
        _builder(tmp_path).build(
            "daily-short", state_root=tmp_path / "state", schedule=schedule
        )


def test_install_writes_atomically_then_bootstraps_exact_user_domain(
    tmp_path: Path,
) -> None:
    recorder = Recorder()
    agents = tmp_path / "home" / "Library" / "LaunchAgents"
    manager = LaunchdManager(
        _builder(tmp_path),
        launch_agents_dir=agents,
        run=recorder,
        uid=501,
    )

    path = manager.install("daily-short", state_root=tmp_path / "state")

    assert path == agents / "com.codex-media-ads.daily-short.plist"
    assert plistlib.loads(path.read_bytes())["RunAtLoad"] is False
    assert recorder.calls == [
        (
            ["launchctl", "bootstrap", "gui/501", str(path)],
            {"check": True, "capture_output": True, "text": True},
        )
    ]
    assert not list(agents.glob("*.tmp"))


def test_install_is_rerunnable_by_booting_out_owned_existing_agent(
    tmp_path: Path,
) -> None:
    recorder = Recorder()
    manager = LaunchdManager(
        _builder(tmp_path),
        launch_agents_dir=tmp_path / "LaunchAgents",
        run=recorder,
        uid=501,
    )

    manager.install("daily-short", state_root=tmp_path / "state")
    manager.install("daily-short", state_root=tmp_path / "state")

    assert [call[0][1] for call in recorder.calls] == [
        "bootstrap",
        "bootout",
        "bootstrap",
    ]


def test_install_never_overwrites_a_non_owned_plist(tmp_path: Path) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    path = agents / "com.codex-media-ads.daily-short.plist"
    original = plistlib.dumps({"Label": "org.example.foreign"})
    path.write_bytes(original)
    manager = LaunchdManager(
        _builder(tmp_path), launch_agents_dir=agents, run=Recorder(), uid=501
    )

    with pytest.raises(ValueError, match="not plugin-owned"):
        manager.install("daily-short", state_root=tmp_path / "state")

    assert path.read_bytes() == original


def test_remove_boots_out_exact_path_and_only_deletes_plugin_owned_plists(
    tmp_path: Path,
) -> None:
    recorder = Recorder()
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    owned = agents / "com.codex-media-ads.daily-short.plist"
    owned.write_bytes(plistlib.dumps({"Label": "com.codex-media-ads.daily-short"}))
    foreign = agents / "com.example.keep.plist"
    foreign.write_text("keep")
    manager = LaunchdManager(
        _builder(tmp_path), launch_agents_dir=agents, run=recorder, uid=502
    )
    state_root = tmp_path / "state"
    state_root.mkdir()

    removed = manager.remove("daily-short", state_root=state_root)

    assert removed is True
    assert recorder.calls[0][0] == [
        "launchctl",
        "bootout",
        "gui/502",
        str(owned),
    ]
    assert not owned.exists()
    assert foreign.exists()
    assert state_root.exists()


def test_uninstall_preserves_user_state_and_removes_only_owned_agents(
    tmp_path: Path,
) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "com.codex-media-ads.one.plist").write_bytes(
        plistlib.dumps({"Label": "com.codex-media-ads.one"})
    )
    (agents / "org.other.plist").write_text("keep")
    state_root = tmp_path / "state"
    state_root.mkdir()
    recorder = Recorder()
    manager = LaunchdManager(
        _builder(tmp_path), launch_agents_dir=agents, run=recorder, uid=503
    )

    removed = manager.uninstall(state_root=state_root, preserve_state=True)

    assert removed == ["one"]
    assert state_root.exists()
    assert (agents / "org.other.plist").exists()


def test_list_returns_only_valid_plugin_owned_agents(tmp_path: Path) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "com.codex-media-ads.daily-short.plist").write_bytes(
        plistlib.dumps({"Label": "com.codex-media-ads.daily-short"})
    )
    (agents / "com.codex-media-ads.spoof.plist").write_bytes(
        plistlib.dumps({"Label": "org.attacker"})
    )

    names = LaunchdManager(
        _builder(tmp_path), launch_agents_dir=agents, run=Recorder(), uid=501
    ).list()

    assert names == ["daily-short"]
