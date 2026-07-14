from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from codex_media_ads.models import PublishResult, PublishStatus
from codex_media_ads.publishing.base import ProbeResult
from codex_media_ads.setup import SecretImportError, SetupService, result_payload


def _ready_probe(identity: str = "creator@example.test") -> ProbeResult:
    return ProbeResult(authenticated=True, observed_identity=identity)


def _dry_run() -> PublishResult:
    return PublishResult(
        status=PublishStatus.SKIPPED,
        evidence={"dry_run": True, "final_action_skipped": True},
    )


def _service(tmp_path: Path, **overrides) -> SetupService:
    tools = {
        "python": tmp_path / "python",
        "ffmpeg": tmp_path / "ffmpeg",
        "ffprobe": tmp_path / "ffprobe",
        "chrome": tmp_path / "Chrome",
        "playwright_browser": tmp_path / "chromium",
        "codimage": tmp_path / "codimage",
        "narration": tmp_path / "narration",
    }
    for path in tools.values():
        path.touch()
        path.chmod(0o700)
    arguments = {
        "state_root": tmp_path / "state",
        "tool_paths": tools,
        "render_probe": lambda: True,
        "probes": {"instagram": _ready_probe()},
        "dry_runs": {"instagram": _dry_run},
    }
    arguments.update(overrides)
    return SetupService(**arguments)


def test_setup_does_not_enable_unprobed_channel(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.probes["instagram"] = ProbeResult(authenticated=False)

    result = service.configure(
        enabled=["instagram"],
        channels={"instagram": {"expected_identity": "creator@example.test"}},
    )

    assert result.channels["instagram"].background_enabled is False
    assert result.channels["instagram"].status == "blocked"


def test_setup_requires_exact_identity_and_final_action_skipping_dry_run(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        probes={"instagram": _ready_probe("different@example.test")},
    )

    mismatch = service.configure(
        enabled=["instagram"],
        channels={"instagram": {"expected_identity": "creator@example.test"}},
    )
    assert mismatch.channels["instagram"].background_enabled is False

    service.probes["instagram"] = _ready_probe()
    service.dry_runs["instagram"] = lambda: PublishResult(
        status=PublishStatus.PUBLISHED
    )
    unsafe = service.configure(
        enabled=["instagram"],
        channels={"instagram": {"expected_identity": "creator@example.test"}},
    )
    assert unsafe.channels["instagram"].background_enabled is False


@pytest.mark.parametrize(
    "evidence",
    [
        {"dry_run": True},
        {"final_action_skipped": True},
        {"dry_run": True, "final_action_skipped": False},
    ],
)
def test_setup_requires_explicit_dry_run_and_final_action_skipped_evidence(
    tmp_path: Path, evidence: dict[str, object]
) -> None:
    service = _service(
        tmp_path,
        dry_runs={
            "instagram": lambda: PublishResult(
                status=PublishStatus.SKIPPED,
                evidence=evidence,
            )
        },
    )

    result = service.configure(
        enabled=["instagram"],
        channels={"instagram": {"expected_identity": "creator@example.test"}},
    )

    assert result.channels["instagram"].background_enabled is False


def test_setup_enables_only_after_all_live_job_gates_pass(tmp_path: Path) -> None:
    result = _service(tmp_path).configure(
        enabled=["instagram"],
        channels={"instagram": {"expected_identity": "creator@example.test"}},
    )

    assert result.channels["instagram"].background_enabled is True
    assert result.checks["synthetic_ffmpeg_render"].status == "ok"


def test_setup_never_enables_background_when_a_dependency_is_missing(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.tool_paths["ffprobe"] = tmp_path / "missing-ffprobe"

    result = service.configure(
        enabled=["instagram"],
        channels={"instagram": {"expected_identity": "creator@example.test"}},
    )

    assert result.channels["instagram"].background_enabled is False


def test_setup_browser_channel_requires_affirmative_controls_readiness(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        dry_runs={
            "instagram": lambda: PublishResult(
                status=PublishStatus.SKIPPED,
                evidence={
                    "dry_run": True,
                    "final_action_skipped": True,
                    "controls_ready": True,
                    "controls": {"upload": True, "submit": True},
                    "controls_enabled": {"upload": True, "submit": False},
                },
            )
        },
    )

    result = service.configure(
        enabled=["instagram"],
        channels={
            "instagram": {
                "expected_identity": "creator@example.test",
                "mode": "browser",
            }
        },
    )

    assert result.channels["instagram"].background_enabled is False


def test_checks_report_ok_blocked_and_missing_without_real_subprocesses(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.tool_paths["ffprobe"] = tmp_path / "missing-ffprobe"
    service.tool_paths["narration"] = None
    service.probes["instagram"] = ProbeResult(authenticated=False)

    checks = service.run_checks(enabled=["instagram"])

    assert checks["python"].status == "ok"
    assert checks["ffprobe"].status == "missing"
    assert checks["narration_provider"].status == "missing"
    assert checks["adapter:instagram"].status == "blocked"
    assert checks["writable_private_state"].status == "ok"


def test_tool_checks_reject_directories_and_non_executable_files(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.tool_paths["ffmpeg"] = tmp_path
    non_executable = tmp_path / "not-executable"
    non_executable.write_text("tool")
    non_executable.chmod(0o600)
    service.tool_paths["ffprobe"] = non_executable

    checks = service.run_checks()

    assert checks["ffmpeg"].status != "ok"
    assert checks["ffprobe"].status != "ok"


def test_writable_state_probe_recovers_from_stale_prior_probe(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.state_root.mkdir(parents=True)
    (service.state_root / ".setup-write-probe").write_text("stale")

    checks = service.run_checks()

    assert checks["writable_private_state"].status == "ok"


def test_python_check_blocks_unsupported_interpreter(tmp_path: Path) -> None:
    checks = _service(tmp_path, python_version=(3, 10)).run_checks()

    assert checks["python"].status == "blocked"


def test_setup_summary_is_blocked_when_required_checks_are_not_ok(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.tool_paths["ffmpeg"] = None

    summary = result_payload(service.configure(enabled=[]))

    assert summary["ok"] is False
    assert summary["status"] == "blocked"


def test_setup_writes_nonsecret_config_and_never_secret_content(tmp_path: Path) -> None:
    service = _service(tmp_path)

    result = service.configure(
        enabled=["instagram"],
        channels={
            "instagram": {
                "expected_identity": "creator@example.test",
                "mode": "browser",
            }
        },
    )

    payload = result.config_path.read_text()
    assert "creator@example.test" in payload
    assert "background_enabled" in payload
    assert "token" not in payload.casefold()
    assert stat.S_IMODE(result.config_path.stat().st_mode) == 0o600
    with pytest.raises(ValueError, match="secret-bearing"):
        service.configure(
            enabled=[],
            channels={"x": {"access_token": "do-not-write"}},
        )


@pytest.mark.parametrize(
    "key",
    [
        "authorization",
        "bearer",
        "token",
        "cookie",
        "key",
        "password",
        "client_secret",
    ],
)
def test_setup_recursively_rejects_secret_like_keys(
    tmp_path: Path, key: str
) -> None:
    with pytest.raises(ValueError, match="secret-bearing"):
        _service(tmp_path).configure(
            enabled=["x"],
            channels={
                "x": {
                    "expected_identity": "creator@example.test",
                    "metadata": {key: "must-not-persist"},
                }
            },
        )


def test_setup_rejects_unknown_nonsecret_channel_keys(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown"):
        _service(tmp_path).configure(
            enabled=["instagram"],
            channels={
                "instagram": {
                    "expected_identity": "creator@example.test",
                    "nickname": "unexpected",
                }
            },
        )


def test_secret_import_rejects_symlinks_and_copies_atomically_private(
    tmp_path: Path,
) -> None:
    source = tmp_path / "credentials.json"
    source.write_text(json.dumps({"access_token": "private-value"}))
    symlink = tmp_path / "credentials-link.json"
    symlink.symlink_to(source)
    service = _service(tmp_path)

    with pytest.raises(SecretImportError, match="symlink"):
        service.import_secret(symlink, "x.json")

    destination = service.import_secret(source, "x.json")

    assert destination.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert not list(destination.parent.glob("*.tmp"))


def test_secret_import_rejects_destination_traversal(tmp_path: Path) -> None:
    source = tmp_path / "credentials.json"
    source.write_text("private")

    with pytest.raises(SecretImportError):
        _service(tmp_path).import_secret(source, "../escaped.json")


def test_secret_import_rejects_a_symlinked_source_parent(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    source = actual / "credentials.json"
    source.write_text("private")
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(SecretImportError, match="symlink"):
        _service(tmp_path).import_secret(linked / "credentials.json", "x.json")


def test_secret_import_rejects_symlinked_private_destination_root(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.state_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (service.state_root / "secrets").symlink_to(outside, target_is_directory=True)
    source = tmp_path / "credential.json"
    source.write_text("private")

    with pytest.raises(SecretImportError, match="symlink"):
        service.import_secret(source, "x.json")

    assert not (outside / "x.json").exists()
