from __future__ import annotations

import os
import plistlib
import subprocess
import sys
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


def test_install_refuses_same_label_plist_with_foreign_command(tmp_path: Path) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    path = agents / "com.codex-media-ads.daily-short.plist"
    builder = _builder(tmp_path)
    payload = builder.build("daily-short", state_root=tmp_path / "state")
    payload["ProgramArguments"] = ["/bin/rm", "-rf", str(tmp_path)]
    original = plistlib.dumps(payload)
    path.write_bytes(original)
    recorder = Recorder()
    manager = LaunchdManager(
        builder, launch_agents_dir=agents, run=recorder, uid=501
    )

    with pytest.raises(ValueError, match="not plugin-owned"):
        manager.install("daily-short", state_root=tmp_path / "state")

    assert recorder.calls == []
    assert path.read_bytes() == original


def test_remove_boots_out_exact_path_and_only_deletes_plugin_owned_plists(
    tmp_path: Path,
) -> None:
    recorder = Recorder()
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    builder = _builder(tmp_path)
    state_root = tmp_path / "state"
    state_root.mkdir()
    owned = agents / "com.codex-media-ads.daily-short.plist"
    owned.write_bytes(
        plistlib.dumps(builder.build("daily-short", state_root=state_root))
    )
    foreign = agents / "com.example.keep.plist"
    foreign.write_text("keep")
    manager = LaunchdManager(
        builder, launch_agents_dir=agents, run=recorder, uid=502
    )

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


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update({"RunAtLoad": True}),
        lambda payload: payload.update({"StandardOutPath": "/tmp/foreign.log"}),
        lambda payload: payload.update(
            {"EnvironmentVariables": {"CODEX_MEDIA_ADS_STATE_ROOT": "/tmp/other"}}
        ),
        lambda payload: payload.update({"ProgramArguments": ["/bin/echo", "owned"]}),
    ],
)
def test_remove_refuses_malformed_same_label_plist(
    tmp_path: Path, mutation
) -> None:
    builder = _builder(tmp_path)
    state_root = tmp_path / "state"
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    path = agents / "com.codex-media-ads.daily-short.plist"
    payload = builder.build("daily-short", state_root=state_root)
    mutation(payload)
    path.write_bytes(plistlib.dumps(payload))
    recorder = Recorder()
    manager = LaunchdManager(
        builder, launch_agents_dir=agents, run=recorder, uid=501
    )

    assert manager.remove("daily-short", state_root=state_root) is False
    assert path.exists()
    assert recorder.calls == []


def test_uninstall_preserves_user_state_and_removes_only_owned_agents(
    tmp_path: Path,
) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    builder = _builder(tmp_path)
    state_root = tmp_path / "state"
    state_root.mkdir()
    (agents / "com.codex-media-ads.one.plist").write_bytes(
        plistlib.dumps(builder.build("one", state_root=state_root))
    )
    (agents / "org.other.plist").write_text("keep")
    recorder = Recorder()
    manager = LaunchdManager(
        builder, launch_agents_dir=agents, run=recorder, uid=503
    )

    removed = manager.uninstall(state_root=state_root, preserve_state=True)

    assert removed == ["one"]
    assert state_root.exists()
    assert (agents / "org.other.plist").exists()


def test_list_returns_only_valid_plugin_owned_agents(tmp_path: Path) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    builder = _builder(tmp_path)
    state_root = tmp_path / "state"
    (agents / "com.codex-media-ads.daily-short.plist").write_bytes(
        plistlib.dumps(builder.build("daily-short", state_root=state_root))
    )
    (agents / "com.codex-media-ads.spoof.plist").write_bytes(
        plistlib.dumps({"Label": "org.attacker"})
    )

    names = LaunchdManager(
        builder, launch_agents_dir=agents, run=Recorder(), uid=501
    ).list()

    assert names == ["daily-short"]


@pytest.mark.parametrize(
    "install_root",
    [
        "",
        "/",
        "{home}",
        "{state}",
        "{state}/nested/codex-media-ads",
        "{home}/custom/not-the-plugin",
    ],
)
def test_uninstall_dry_run_rejects_unsafe_install_roots(
    tmp_path: Path, install_root: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state = home / ".codex-media-ads"
    script = Path(__file__).parents[1] / "scripts" / "uninstall.sh"
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "CODEX_MEDIA_ADS_STATE_ROOT": str(state),
            "CODEX_MEDIA_ADS_INSTALL_ROOT": install_root.format(
                home=home, state=state
            ),
        }
    )

    result = subprocess.run(
        ["/bin/sh", str(script), "--dry-run"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "unsafe install root" in result.stderr


def test_uninstall_dry_run_allows_safe_plugin_named_home_descendant(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    script = Path(__file__).parents[1] / "scripts" / "uninstall.sh"
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "CODEX_MEDIA_ADS_STATE_ROOT": str(home / ".codex-media-ads"),
            "CODEX_MEDIA_ADS_INSTALL_ROOT": str(
                home / "custom" / "codex-media-ads"
            ),
        }
    )

    result = subprocess.run(
        ["/bin/sh", str(script), "--dry-run"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "launchctl was not invoked" in result.stdout


def test_uninstall_rejects_filesystem_root_as_state_ancestor(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    script = Path(__file__).parents[1] / "scripts" / "uninstall.sh"
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "CODEX_MEDIA_ADS_STATE_ROOT": "/",
            "CODEX_MEDIA_ADS_INSTALL_ROOT": str(
                home / "custom" / "codex-media-ads"
            ),
        }
    )

    result = subprocess.run(
        ["/bin/sh", str(script), "--dry-run"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "unsafe install root" in result.stderr


def test_uninstall_allows_safe_root_when_home_has_symlinked_ancestor(
    tmp_path: Path,
) -> None:
    actual_parent = tmp_path / "actual"
    home = actual_parent / "home"
    home.mkdir(parents=True)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    lexical_home = linked_parent / "home"
    script = Path(__file__).parents[1] / "scripts" / "uninstall.sh"
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(lexical_home),
            "CODEX_MEDIA_ADS_STATE_ROOT": str(lexical_home / ".codex-media-ads"),
            "CODEX_MEDIA_ADS_INSTALL_ROOT": str(
                lexical_home / "custom" / "codex-media-ads"
            ),
        }
    )

    result = subprocess.run(
        ["/bin/sh", str(script), "--dry-run"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0


def test_install_dry_run_prints_speech_handoff_without_writing(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    install_root = home / ".local" / "share" / "codex-media-ads"
    state_root = home / ".codex-media-ads"
    script = Path(__file__).parents[1] / "scripts" / "install.sh"
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "CODEX_MEDIA_ADS_INSTALL_ROOT": str(install_root),
            "CODEX_MEDIA_ADS_STATE_ROOT": str(state_root),
            "PYTHON_BIN": sys.executable,
        }
    )

    result = subprocess.run(
        ["/bin/sh", str(script), "--dry-run"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "scripts/install_speech.py" in result.stdout
    assert "dependencies/speech.lock.json" in result.stdout
    assert f"--install-root' '{install_root}" in result.stdout
    assert "--dry-run" in result.stdout
    assert "no files changed and no LaunchAgent loaded" in result.stdout
    assert not install_root.exists()
    assert not state_root.exists()
    assert not (home / "Library" / "LaunchAgents").exists()


def test_install_invokes_speech_helper_after_base_package_install(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    install_root = home / ".local" / "share" / "codex-media-ads"
    state_root = home / ".codex-media-ads"
    calls_path = tmp_path / "python-calls.txt"
    fake_python = tmp_path / "python3"
    fake_python.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> '{calls_path}'\n"
        "if [ \"$1\" = '-m' ] && [ \"$2\" = 'venv' ]; then\n"
        "    mkdir -p \"$3/bin\"\n"
        "    cp \"$0\" \"$3/bin/python\"\n"
        "fi\n"
    )
    fake_python.chmod(0o700)
    script = Path(__file__).parents[1] / "scripts" / "install.sh"
    plugin_root = script.parent.parent
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "CODEX_MEDIA_ADS_INSTALL_ROOT": str(install_root),
            "CODEX_MEDIA_ADS_STATE_ROOT": str(state_root),
            "PYTHON_BIN": str(fake_python),
        }
    )

    result = subprocess.run(
        ["/bin/sh", str(script)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    calls = calls_path.read_text().splitlines()
    assert calls == [
        f"-m venv {install_root}/venv",
        f"-m pip install --upgrade {plugin_root}",
        (
            f"{plugin_root}/scripts/install_speech.py "
            f"--lock {plugin_root}/dependencies/speech.lock.json "
            f"--install-root {install_root}"
        ),
    ]


@pytest.mark.parametrize("alias_kind", ["dotdot", "symlink"])
def test_uninstall_rejects_physical_state_overlap_before_any_action(
    tmp_path: Path, alias_kind: str
) -> None:
    home = tmp_path / "home"
    install_root = home / "custom" / "codex-media-ads"
    install_root.mkdir(parents=True)
    sentinel = install_root / "keep.txt"
    sentinel.write_text("keep")
    target_state = install_root / "private-state"
    target_state.mkdir()
    if alias_kind == "dotdot":
        state_root = home / "placeholder" / ".." / "custom" / "codex-media-ads" / "private-state"
    else:
        state_root = home / "state-alias"
        state_root.symlink_to(target_state, target_is_directory=True)
    marker = tmp_path / "launchctl-called"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_launchctl = fake_bin / "launchctl"
    fake_launchctl.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    fake_launchctl.chmod(0o700)
    script = Path(__file__).parents[1] / "scripts" / "uninstall.sh"
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{environment.get('PATH', '')}",
            "CODEX_MEDIA_ADS_STATE_ROOT": str(state_root),
            "CODEX_MEDIA_ADS_INSTALL_ROOT": str(install_root),
        }
    )

    result = subprocess.run(
        ["/bin/sh", str(script), "--dry-run"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "unsafe install root" in result.stderr
    assert sentinel.read_text() == "keep"
    assert not marker.exists()
