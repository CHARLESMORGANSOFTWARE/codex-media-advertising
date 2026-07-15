from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _run_installer(
    plugin_root: Path,
    lock: Path,
    install_root: Path | str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(plugin_root / "scripts/install_speech.py"),
            "--lock",
            str(lock),
            "--install-root",
            str(install_root),
            "--dry-run",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _load_installer(plugin_root: Path) -> ModuleType:
    path = plugin_root / "scripts" / "install_speech.py"
    spec = importlib.util.spec_from_file_location("speech_installer_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_speech_lock_pins_git_and_models(plugin_root: Path) -> None:
    lock = json.loads(
        (plugin_root / "dependencies/speech.lock.json").read_text()
    )

    assert lock == {
        "schema_version": 1,
        "git_url": "https://github.com/speaches-ai/speaches.git",
        "git_revision": "22ba05d9c00dfb4302e2403d82ad786a48db3e3b",
        "python_version": "3.12",
        "uv_version": "0.10.12",
        "tts_model": "speaches-ai/Kokoro-82M-v1.0-ONNX",
        "stt_model": "Systran/faster-distil-whisper-small.en",
    }


def test_speech_installer_dry_run_is_pinned_and_non_mutating(
    tmp_path: Path, plugin_root: Path
) -> None:
    install_root = tmp_path / "install"

    result = subprocess.run(
        [
            sys.executable,
            str(plugin_root / "scripts/install_speech.py"),
            "--lock",
            str(plugin_root / "dependencies/speech.lock.json"),
            "--install-root",
            str(install_root),
            "--dry-run",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "uv==0.10.12" in result.stdout
    assert "git clone" in result.stdout
    assert "22ba05d9c00dfb4302e2403d82ad786a48db3e3b" in result.stdout
    assert "git rev-parse HEAD" in result.stdout
    assert "uv python install 3.12" in result.stdout
    assert "uv sync --frozen" in result.stdout
    assert str(install_root / "speech" / "speaches") in result.stdout
    assert not install_root.exists()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"schema_version": 2}, "schema_version"),
        ({"unexpected": "value"}, "schema"),
        ({"uv_version": 10}, "uv_version"),
    ],
)
def test_speech_installer_rejects_invalid_lock_schema(
    tmp_path: Path,
    plugin_root: Path,
    mutation: dict[str, object],
    message: str,
) -> None:
    payload = json.loads(
        (plugin_root / "dependencies/speech.lock.json").read_text()
    )
    payload.update(mutation)
    lock = tmp_path / "invalid.lock.json"
    lock.write_text(json.dumps(payload))

    result = _run_installer(plugin_root, lock, tmp_path / "install")

    assert result.returncode == 2
    assert message in result.stderr
    assert not (tmp_path / "install").exists()


@pytest.mark.parametrize(
    "revision",
    [
        "22ba05d9",
        "z" * 40,
        "22BA05D9C00DFB4302E2403D82AD786A48DB3E3B",
    ],
)
def test_speech_installer_rejects_unpinned_git_revision(
    tmp_path: Path,
    plugin_root: Path,
    revision: str,
) -> None:
    payload = json.loads(
        (plugin_root / "dependencies/speech.lock.json").read_text()
    )
    payload["git_revision"] = revision
    lock = tmp_path / "invalid.lock.json"
    lock.write_text(json.dumps(payload))

    result = _run_installer(plugin_root, lock, tmp_path / "install")

    assert result.returncode == 2
    assert "git_revision" in result.stderr
    assert not (tmp_path / "install").exists()


@pytest.mark.parametrize("install_root", ["", "/", "relative/install"])
def test_speech_installer_rejects_unsafe_install_roots(
    tmp_path: Path,
    plugin_root: Path,
    install_root: str,
) -> None:
    result = _run_installer(
        plugin_root,
        plugin_root / "dependencies/speech.lock.json",
        install_root,
    )

    assert result.returncode == 2
    assert "unsafe install root" in result.stderr
    assert not (tmp_path / "install").exists()


@pytest.mark.parametrize("suffix", ["/./install", "//install"])
def test_speech_installer_rejects_non_lexical_install_roots(
    tmp_path: Path,
    plugin_root: Path,
    suffix: str,
) -> None:
    install_root = f"{tmp_path}{suffix}"

    result = _run_installer(
        plugin_root,
        plugin_root / "dependencies/speech.lock.json",
        install_root,
    )

    assert result.returncode == 2
    assert "not lexical" in result.stderr


def test_speech_installer_rejects_symlinked_install_root(
    tmp_path: Path, plugin_root: Path
) -> None:
    actual_parent = tmp_path / "actual"
    actual_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    install_root = linked_parent / "install"

    result = _run_installer(
        plugin_root,
        plugin_root / "dependencies/speech.lock.json",
        install_root,
    )

    assert result.returncode == 2
    assert "symlink" in result.stderr
    assert not install_root.exists()


@pytest.mark.parametrize("location", ["checkout", "descendant"])
def test_speech_installer_rejects_install_root_inside_plugin_checkout(
    plugin_root: Path, location: str
) -> None:
    install_root = (
        plugin_root
        if location == "checkout"
        else plugin_root / "checkout-contained-install"
    )

    result = _run_installer(
        plugin_root,
        plugin_root / "dependencies/speech.lock.json",
        install_root,
    )

    assert result.returncode == 2
    assert "plugin checkout" in result.stderr
    if location == "descendant":
        assert not install_root.exists()


def test_speech_installer_rejects_symlink_alias_into_plugin_checkout(
    tmp_path: Path, plugin_root: Path
) -> None:
    plugin_alias = tmp_path / "plugin-alias"
    plugin_alias.symlink_to(plugin_root, target_is_directory=True)
    install_root = plugin_alias / "checkout-contained-install"

    result = _run_installer(
        plugin_root,
        plugin_root / "dependencies/speech.lock.json",
        install_root,
    )

    assert result.returncode == 2
    assert "plugin checkout" in result.stderr
    assert not install_root.exists()


def test_speech_installer_rejects_symlinked_speaches_destination(
    tmp_path: Path, plugin_root: Path
) -> None:
    install_root = tmp_path / "install"
    speech_root = install_root / "speech"
    speech_root.mkdir(parents=True)
    actual = tmp_path / "actual-speaches"
    actual.mkdir()
    (speech_root / "speaches").symlink_to(actual, target_is_directory=True)

    result = _run_installer(
        plugin_root,
        plugin_root / "dependencies/speech.lock.json",
        install_root,
    )

    assert result.returncode == 2
    assert "symlink" in result.stderr


def test_speech_installer_clones_and_checks_out_exact_revision_when_absent(
    tmp_path: Path,
    plugin_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = _load_installer(plugin_root)
    revision = "22ba05d9c00dfb4302e2403d82ad786a48db3e3b"
    install_root = tmp_path / "install"
    source = install_root / "speech" / "speaches"
    calls: list[tuple[list[str], dict[str, object]]] = []

    def record(args: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((list(args), kwargs))
        stdout = f"{revision}\n" if args == ["git", "rev-parse", "HEAD"] else ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(installer.subprocess, "run", record)
    monkeypatch.setattr(installer, "_uv_executable", lambda: "/managed/bin/uv")

    result = installer.main(
        [
            "--lock",
            str(plugin_root / "dependencies/speech.lock.json"),
            "--install-root",
            str(install_root),
        ]
    )

    assert result == 0
    assert install_root.is_dir()
    assert [call[0] for call in calls] == [
        [sys.executable, "-m", "pip", "install", "uv==0.10.12"],
        [
            "git",
            "clone",
            "--no-checkout",
            "https://github.com/speaches-ai/speaches.git",
            str(source),
        ],
        ["git", "checkout", "--detach", revision],
        ["git", "rev-parse", "HEAD"],
        ["git", "status", "--porcelain", "--untracked-files=no"],
        ["/managed/bin/uv", "python", "install", "3.12"],
        ["/managed/bin/uv", "sync", "--frozen"],
    ]
    assert all(
        isinstance(call[0], list) and call[1]["check"] is True
        for call in calls
    )
    assert calls[2][1]["cwd"] == source
    assert calls[3][1] == {
        "check": True,
        "cwd": source,
        "capture_output": True,
        "text": True,
    }
    assert calls[6][1]["cwd"] == source


@pytest.mark.parametrize("precreate_mode", [None, 0o755])
def test_speech_installer_enforces_owner_only_install_root(
    tmp_path: Path,
    plugin_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    precreate_mode: int | None,
) -> None:
    installer = _load_installer(plugin_root)
    revision = "22ba05d9c00dfb4302e2403d82ad786a48db3e3b"
    install_root = tmp_path / "install"
    if precreate_mode is not None:
        install_root.mkdir(mode=precreate_mode)
        install_root.chmod(precreate_mode)

    def succeed(args: list[str], **kwargs: object) -> SimpleNamespace:
        stdout = f"{revision}\n" if args == ["git", "rev-parse", "HEAD"] else ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(installer.subprocess, "run", succeed)
    monkeypatch.setattr(installer, "_uv_executable", lambda: "/managed/bin/uv")

    result = installer.main(
        [
            "--lock",
            str(plugin_root / "dependencies/speech.lock.json"),
            "--install-root",
            str(install_root),
        ]
    )

    assert result == 0
    assert stat.S_IMODE(install_root.stat().st_mode) == 0o700


def test_speech_installer_fetches_exact_revision_when_checkout_exists(
    tmp_path: Path,
    plugin_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = _load_installer(plugin_root)
    revision = "22ba05d9c00dfb4302e2403d82ad786a48db3e3b"
    install_root = tmp_path / "install"
    source = install_root / "speech" / "speaches"
    source.mkdir(parents=True)
    calls: list[tuple[list[str], dict[str, object]]] = []

    origin_url = "https://github.com/speaches-ai/speaches.git"

    def record(args: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((list(args), kwargs))
        if args == ["git", "rev-parse", "HEAD"]:
            stdout = f"{revision}\n"
        elif args == ["git", "remote", "get-url", "origin"]:
            stdout = f"{origin_url}\n"
        else:
            stdout = ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(installer.subprocess, "run", record)
    monkeypatch.setattr(installer, "_uv_executable", lambda: "/managed/bin/uv")

    result = installer.main(
        [
            "--lock",
            str(plugin_root / "dependencies/speech.lock.json"),
            "--install-root",
            str(install_root),
        ]
    )

    assert result == 0
    commands = [call[0] for call in calls]
    assert ["git", "remote", "set-url", "origin", origin_url] in commands
    assert ["git", "remote", "get-url", "origin"] in commands
    assert ["git", "fetch", "--force", "origin", revision] in commands
    assert not any(command[:2] == ["git", "clone"] for command in commands)
    assert commands.index(["git", "remote", "set-url", "origin", origin_url]) < (
        commands.index(["git", "remote", "get-url", "origin"])
    ) < commands.index(["git", "fetch", "--force", "origin", revision])
    fetch = next(call for call in calls if call[0][:2] == ["git", "fetch"])
    assert fetch[1] == {"check": True, "cwd": source}


def test_speech_installer_resets_mismatched_origin_before_fetch(
    tmp_path: Path,
    plugin_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = _load_installer(plugin_root)
    revision = "22ba05d9c00dfb4302e2403d82ad786a48db3e3b"
    locked_url = "https://github.com/speaches-ai/speaches.git"
    current_origin = "https://example.invalid/untrusted.git"
    install_root = tmp_path / "install"
    source = install_root / "speech" / "speaches"
    source.mkdir(parents=True)
    calls: list[list[str]] = []

    def record(args: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal current_origin
        calls.append(list(args))
        if args[:4] == ["git", "remote", "set-url", "origin"]:
            current_origin = args[4]
            stdout = ""
        elif args == ["git", "remote", "get-url", "origin"]:
            stdout = f"{current_origin}\n"
        elif args == ["git", "rev-parse", "HEAD"]:
            stdout = f"{revision}\n"
        else:
            stdout = ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(installer.subprocess, "run", record)
    monkeypatch.setattr(installer, "_uv_executable", lambda: "/managed/bin/uv")

    result = installer.main(
        [
            "--lock",
            str(plugin_root / "dependencies/speech.lock.json"),
            "--install-root",
            str(install_root),
        ]
    )

    assert result == 0
    assert current_origin == locked_url
    reset = ["git", "remote", "set-url", "origin", locked_url]
    verify = ["git", "remote", "get-url", "origin"]
    fetch = ["git", "fetch", "--force", "origin", revision]
    assert calls.index(reset) < calls.index(verify) < calls.index(fetch)


def test_speech_installer_rejects_origin_that_does_not_reset_to_lock(
    tmp_path: Path,
    plugin_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installer = _load_installer(plugin_root)
    revision = "22ba05d9c00dfb4302e2403d82ad786a48db3e3b"
    install_root = tmp_path / "install"
    source = install_root / "speech" / "speaches"
    source.mkdir(parents=True)
    calls: list[list[str]] = []

    def record(args: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(list(args))
        if args == ["git", "remote", "get-url", "origin"]:
            stdout = "https://example.invalid/still-untrusted.git\n"
        elif args == ["git", "rev-parse", "HEAD"]:
            stdout = f"{revision}\n"
        else:
            stdout = ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(installer.subprocess, "run", record)
    monkeypatch.setattr(installer, "_uv_executable", lambda: "/managed/bin/uv")

    result = installer.main(
        [
            "--lock",
            str(plugin_root / "dependencies/speech.lock.json"),
            "--install-root",
            str(install_root),
        ]
    )

    assert result == 2
    assert "origin does not match locked git_url" in capsys.readouterr().err
    assert ["git", "fetch", "--force", "origin", revision] not in calls


def test_speech_installer_rejects_dirty_tracked_checkout_before_uv_sync(
    tmp_path: Path,
    plugin_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installer = _load_installer(plugin_root)
    revision = "22ba05d9c00dfb4302e2403d82ad786a48db3e3b"
    origin_url = "https://github.com/speaches-ai/speaches.git"
    install_root = tmp_path / "install"
    source = install_root / "speech" / "speaches"
    source.mkdir(parents=True)
    calls: list[list[str]] = []

    def record(args: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(list(args))
        if args == ["git", "remote", "get-url", "origin"]:
            stdout = f"{origin_url}\n"
        elif args == ["git", "rev-parse", "HEAD"]:
            stdout = f"{revision}\n"
        elif args == ["git", "status", "--porcelain", "--untracked-files=no"]:
            stdout = " M pyproject.toml\n"
        else:
            stdout = ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(installer.subprocess, "run", record)
    monkeypatch.setattr(installer, "_uv_executable", lambda: "/managed/bin/uv")

    result = installer.main(
        [
            "--lock",
            str(plugin_root / "dependencies/speech.lock.json"),
            "--install-root",
            str(install_root),
        ]
    )

    assert result == 2
    assert "tracked changes" in capsys.readouterr().err
    assert ["/managed/bin/uv", "sync", "--frozen"] not in calls


def test_speech_installer_aborts_when_checked_out_head_is_not_pinned(
    tmp_path: Path,
    plugin_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installer = _load_installer(plugin_root)
    install_root = tmp_path / "install"
    calls: list[list[str]] = []

    def record(args: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(list(args))
        stdout = f"{'0' * 40}\n" if args == ["git", "rev-parse", "HEAD"] else ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(installer.subprocess, "run", record)
    monkeypatch.setattr(installer, "_uv_executable", lambda: "/managed/bin/uv")

    result = installer.main(
        [
            "--lock",
            str(plugin_root / "dependencies/speech.lock.json"),
            "--install-root",
            str(install_root),
        ]
    )

    assert result == 2
    assert "checked out HEAD does not match pinned revision" in capsys.readouterr().err
    assert not any(command[:2] == ["/managed/bin/uv", "python"] for command in calls)
