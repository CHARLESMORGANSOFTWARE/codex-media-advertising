from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_media_ads import cli
from codex_media_ads.automation.launchd import LaunchdBuilder, LaunchdManager
from codex_media_ads.creative.pipeline import CreativePipeline
from codex_media_ads.creative.providers import CodimageProvider, CommandNarrationProvider
from codex_media_ads.models import (
    AccountConfig,
    CampaignManifest,
    PublishRequest,
    PublishResult,
    PublishStatus,
)

from test_orchestrator import PLATFORMS, orchestrator, x_request


class RuntimeResponse:
    status_code = 200
    headers: dict[str, str] = {}
    text = ""

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def json(self) -> dict[str, object]:
        return self.payload


class RuntimeTransport:
    def __init__(self, responses: int = 4) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, **_kwargs) -> RuntimeResponse:
        self.calls.append((method, url))
        if self.responses <= 0:
            raise AssertionError(f"unexpected runtime request: {method} {url}")
        self.responses -= 1
        return RuntimeResponse({"screen_name": "runtime-creator"})


def write_runtime_config(
    state_root: Path,
    *,
    python_executable: str,
    include_creative: bool = False,
) -> tuple[Path, Path]:
    config_root = state_root / "config"
    config_root.mkdir(parents=True, mode=0o700)
    secret_root = state_root / "secrets"
    secret_root.mkdir(parents=True, mode=0o700)
    secret_file = secret_root / "x.json"
    secret_file.write_text(
        json.dumps(
            {
                "consumer_key": "fake-consumer-key",
                "consumer_secret": "fake-consumer-secret",
                "access_token": "fake-access-token",
                "access_token_secret": "fake-access-secret",
            }
        )
    )
    secret_file.chmod(0o600)
    config: dict[str, object] = {
        "accounts": {
            "x": {
                "account_id": "runtime-x",
                "expected_identity": "runtime-creator",
                "mode": "api",
                "secret_file": str(secret_file),
            }
        }
    }
    if include_creative:
        fixtures = Path(__file__).parent / "fixtures"
        config["creative"] = {
            "image": {
                "provider": "codimage",
                "project_root": str(state_root / "codimage-project"),
                "executable_prefix": [
                    python_executable,
                    str(fixtures / "fake_codimage.py"),
                ],
            },
            "narration": {
                "provider": "command",
                "command_template": [
                    python_executable,
                    str(fixtures / "fake_tts.py"),
                    "--text-file",
                    "{text_file}",
                    "--output-path",
                    "{output_path}",
                    "--voice",
                    "{voice}",
                ],
                "work_dir": str(state_root / "narration-work"),
            },
        }
    runtime_file = config_root / "runtime.json"
    runtime_file.write_text(json.dumps(config))
    runtime_file.chmod(0o600)
    return runtime_file, secret_file


def write_campaign(path: Path) -> Path:
    campaign = CampaignManifest(
        schema_version="1",
        brand="Example",
        campaign_id="cli-launch",
        rights_confirmed=True,
        audience="drivers",
        offer="Try it",
        proof_points=["Works"],
        calls_to_action=["Learn more"],
        visual_prompts=["A product on a desk"],
        narration="A concise launch message.",
        duration_seconds=15,
        destinations=list(PLATFORMS),
        timezone="UTC",
        schedule=[datetime(2026, 7, 14, tzinfo=timezone.utc)],
        daily_cap=20,
        retry_limit=1,
        failure_pause_threshold=2,
    )
    path.write_text(campaign.model_dump_json(exclude_computed_fields=True))
    return path


def run_cli(capsys, args: list[str], orchestrator=None):
    exit_code = cli.main(args, orchestrator=orchestrator)
    output = capsys.readouterr().out
    return exit_code, json.loads(output)


@pytest.mark.parametrize(
    "args",
    [
        ["setup", "--help"],
        ["automation", "install", "--help"],
        ["automation", "list", "--help"],
        ["automation", "remove", "--help"],
        ["campaign", "validate", "--help"],
        ["campaign", "build", "--help"],
        ["queue", "add", "--help"],
        ["queue", "status", "--help"],
        ["publish", "next", "--help"],
        ["publish", "probe", "--help"],
        ["platform", "pause", "--help"],
        ["platform", "resume", "--help"],
        ["receipts", "show", "--help"],
    ],
)
def test_complete_command_surface(args: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert exc.value.code == 0


def test_campaign_validate_defaults_to_json(capsys, tmp_path: Path) -> None:
    campaign_path = write_campaign(tmp_path / "campaign.json")

    exit_code, payload = run_cli(
        capsys, ["campaign", "validate", str(campaign_path)]
    )

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["status"] == "valid"


def test_campaign_build_dry_run_never_creates_success_receipt(
    capsys, tmp_path: Path, orchestrator
) -> None:
    campaign_path = write_campaign(tmp_path / "campaign.json")

    exit_code, payload = run_cli(
        capsys,
        ["campaign", "build", str(campaign_path), "--dry-run"],
        orchestrator,
    )

    assert exit_code == 0
    assert payload["live_success_count"] == 0
    assert orchestrator.queue_store.receipts.count_successes_on_date(
        "2026-07-14", "UTC"
    ) == 0


def test_validation_failure_returns_exit_two_and_safe_json(capsys, tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"api_token":"plain-secret"}')

    exit_code, payload = run_cli(capsys, ["campaign", "validate", str(path)])

    assert exit_code == 2
    assert payload["ok"] is False
    assert payload["error_category"] in {"validation", "configuration"}
    assert "plain-secret" not in json.dumps(payload)


def test_blocked_probe_returns_exit_three_and_redacts_detail(
    capsys, orchestrator
) -> None:
    adapter = orchestrator.adapters["youtube"]
    adapter.probe = adapter.probe.model_copy(
        update={
            "authenticated": False,
            "detail": "Authorization: Bearer top-secret-token",
        }
    )

    exit_code, payload = run_cli(
        capsys,
        ["publish", "probe", "--platform", "youtube"],
        orchestrator,
    )

    assert exit_code == 3
    assert payload["ok"] is False
    assert payload["status"] == "blocked"
    assert "top-secret-token" not in json.dumps(payload)
    assert "account_id" not in json.dumps(payload)


def test_failed_publish_returns_exit_four(capsys, orchestrator, x_request) -> None:
    orchestrator.adapters["x"].next_result = orchestrator.adapters[
        "x"
    ].publish(x_request).model_copy(
        update={
            "status": PublishStatus.FAILED,
            "platform_id": "",
            "evidence": {},
            "error_category": "validation",
        }
    )
    orchestrator.adapters["x"].publish_calls = 0
    request_path = x_request.media_path.with_suffix(".json")
    request_path.write_text(x_request.model_dump_json())
    run_cli(capsys, ["queue", "add", str(request_path)], orchestrator)

    exit_code, payload = run_cli(
        capsys, ["publish", "next"], orchestrator
    )

    assert exit_code == 4
    assert payload["ok"] is False
    assert payload["status"] == "failed"
    assert Path(payload["receipt_file"]).is_file()


def test_platform_pause_resume_and_receipts_commands_are_json_first(
    capsys, orchestrator
) -> None:
    exit_code, paused = run_cli(
        capsys,
        ["platform", "pause", "--platform", "x"],
        orchestrator,
    )
    assert exit_code == 0
    assert paused == {"ok": True, "status": "paused", "platform": "x"}

    exit_code, resumed = run_cli(
        capsys,
        ["platform", "resume", "--platform", "x"],
        orchestrator,
    )
    assert exit_code == 0
    assert resumed == {"ok": True, "status": "resumed", "platform": "x"}

    exit_code, receipts = run_cli(
        capsys, ["receipts", "show"], orchestrator
    )
    assert exit_code == 0
    assert receipts["ok"] is True
    assert receipts["receipts"][0]["event"] == "resume"
    assert "account_id" not in json.dumps(receipts)


def test_queue_status_is_an_intentional_noop(capsys, orchestrator) -> None:
    exit_code, payload = run_cli(capsys, ["queue", "status"], orchestrator)

    assert exit_code == 0
    assert payload == {
        "ok": True,
        "status": "idle",
        "pending": 0,
        "claimed": 0,
        "completed": 0,
        "failed": 0,
    }


def test_module_help_invokes_the_cli() -> None:
    package_root = Path(__file__).parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(package_root / "src")

    result = subprocess.run(
        [sys.executable, "-m", "codex_media_ads.cli", "--help"],
        cwd=package_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "usage: codex-media-ads" in result.stdout


def test_publish_next_dry_run_marks_the_attempt_receipt(
    capsys, orchestrator, x_request
) -> None:
    request_path = x_request.media_path.with_suffix(".json")
    request_path.write_text(x_request.model_dump_json())
    run_cli(capsys, ["queue", "add", str(request_path)], orchestrator)

    exit_code, payload = run_cli(
        capsys, ["publish", "next", "--dry-run"], orchestrator
    )

    assert exit_code == 0
    assert payload["status"] == "skipped"
    receipt = orchestrator.receipts()[-1]
    assert receipt["dry_run"] is True
    assert receipt["status"] == "skipped"


def test_publish_next_accepts_launchagent_schedule_marker(
    capsys, orchestrator
) -> None:
    exit_code, payload = run_cli(
        capsys,
        ["publish", "next", "--schedule", "daily-short"],
        orchestrator,
    )

    assert exit_code == 0
    assert payload["status"] == "skipped"


def test_automation_cli_uses_injected_manager_without_real_launchctl(
    capsys, tmp_path: Path
) -> None:
    executable = tmp_path / "bin" / "codex-media-ads"
    executable.parent.mkdir()
    executable.touch()
    workdir = tmp_path / "plugin"
    workdir.mkdir()
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    manager = LaunchdManager(
        LaunchdBuilder(executable=executable, working_directory=workdir),
        launch_agents_dir=tmp_path / "LaunchAgents",
        run=run,
        uid=501,
    )
    setup_config = tmp_path / "state" / "config" / "setup.json"
    setup_config.parent.mkdir(parents=True)
    setup_config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "channels": {"x": {"background_enabled": True}},
            }
        )
    )

    exit_code = cli.main(
        [
            "--state-root",
            str(tmp_path / "state"),
            "automation",
            "install",
            "daily-short",
        ],
        launchd_manager=manager,
    )
    installed = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert installed["status"] == "installed"
    assert calls[0][0][:3] == ["launchctl", "bootstrap", "gui/501"]

    exit_code = cli.main(
        ["automation", "list"], launchd_manager=manager
    )
    listed = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert listed["automations"] == ["daily-short"]


def test_automation_install_is_blocked_until_setup_proves_live_gates(
    capsys, tmp_path: Path
) -> None:
    class NeverInstall:
        def install(self, *args, **kwargs):
            raise AssertionError("LaunchAgent must not be installed")

    exit_code = cli.main(
        [
            "--state-root",
            str(tmp_path / "state"),
            "automation",
            "install",
            "daily-short",
        ],
        launchd_manager=NeverInstall(),
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 3
    assert payload["status"] == "blocked"


def test_noninjected_setup_uses_configured_runtime_probe_narration_and_dry_run(
    capsys, tmp_path: Path, monkeypatch
) -> None:
    events: list[str] = []
    account = AccountConfig(
        account_id="runtime-x",
        expected_identity="runtime-creator",
        mode="api",
    )

    class Adapter:
        def probe_auth(self, supplied):
            assert supplied is account
            events.append("probe")
            return SimpleNamespace(
                authenticated=True,
                observed_identity="runtime-creator",
                error_category=None,
                detail="",
                next_action="",
            )

        def publish(self, request):
            assert request.account == account
            assert request.platform == "x"
            assert request.dry_run is True
            events.append("dry-run")
            return PublishResult(
                status=PublishStatus.SKIPPED,
                evidence={"dry_run": True, "final_action_skipped": True},
            )

    adapter = Adapter()
    configured_chrome = tmp_path / "Configured Chrome"
    configured_chrome.write_text("chrome")
    configured_chrome.chmod(0o700)

    class Router:
        def __init__(self):
            self.browser_adapters = {
                "x": SimpleNamespace(
                    browser=SimpleNamespace(chrome_path=configured_chrome),
                    config_root=tmp_path,
                )
            }

        def select(self, supplied, platform):
            assert supplied is account
            assert platform == "x"
            return adapter

    class Narration:
        command_identity = ["/usr/bin/true"]

        def synthesize(self, text, output_path, voice):
            events.append("narration")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"audio")
            return output_path

    builder = SimpleNamespace(
        ffmpeg=Path("/usr/bin/true"),
        ffprobe=Path("/usr/bin/true"),
        image_provider=SimpleNamespace(command_identity=["/usr/bin/true"]),
        narration_provider=Narration(),
        voice="alloy",
    )

    runtime = SimpleNamespace(
        accounts={"x": account},
        router=Router(),
        builder=builder,
        probe=lambda platform: adapter.probe_auth(account),
    )

    monkeypatch.setattr(cli, "load_runtime", lambda *args, **kwargs: runtime)
    monkeypatch.setattr(
        "codex_media_ads.setup._default_tools",
        lambda: {
            name: (None if name == "chrome" else Path("/usr/bin/true"))
            for name in (
                "python",
                "ffmpeg",
                "ffprobe",
                "chrome",
                "playwright_browser",
                "codimage",
                "narration",
            )
        },
    )
    monkeypatch.setattr(
        "codex_media_ads.setup.SetupService._synthetic_render", lambda self: True
    )

    exit_code = cli.main(
        ["--state-root", str(tmp_path / "state"), "setup", "--enable", "x"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0, (payload, events)
    assert payload["status"] == "ready"
    assert payload["checks"]["chrome"]["status"] == "ok"
    assert payload["channels"]["x"]["background_enabled"] is True
    assert {"probe", "narration", "dry-run"}.issubset(events)
    saved = json.loads((tmp_path / "state" / "config" / "setup.json").read_text())
    assert saved["channels"]["x"]["expected_identity"] == "runtime-creator"


def test_setup_config_file_rejects_unknown_top_level_keys(tmp_path: Path) -> None:
    path = tmp_path / "setup.json"
    path.write_text(json.dumps({"channels": {}, "unexpected": True}))

    with pytest.raises(ValueError, match="unknown setup configuration"):
        cli._channel_config(path)


def test_publish_next_unknown_is_failed_exit_four(
    capsys, orchestrator, x_request
) -> None:
    orchestrator.adapters["x"].next_result = PublishResult(
        status=PublishStatus.UNKNOWN,
        error_category="ambiguous_submit",
        detail="final submit outcome is unknown",
    )
    request_path = x_request.media_path.with_suffix(".json")
    request_path.write_text(x_request.model_dump_json())
    run_cli(capsys, ["queue", "add", str(request_path)], orchestrator)

    exit_code, payload = run_cli(
        capsys, ["publish", "next"], orchestrator
    )

    assert exit_code == 4
    assert payload["ok"] is False
    assert payload["status"] == "failed"
    assert payload["error_category"] == "ambiguous_submit"


def test_campaign_build_summary_treats_unknown_as_failed(
    capsys, tmp_path: Path, orchestrator
) -> None:
    campaign_path = write_campaign(tmp_path / "campaign.json")
    campaign = CampaignManifest.model_validate_json(campaign_path.read_text())
    campaign_path.write_text(
        campaign.model_copy(update={"destinations": ["x"]}).model_dump_json(
            exclude_computed_fields=True
        )
    )
    orchestrator.adapters["x"].next_result = PublishResult(
        status=PublishStatus.UNKNOWN,
        error_category="ambiguous_submit",
        detail="final submit outcome is unknown",
    )

    exit_code, payload = run_cli(
        capsys, ["campaign", "build", str(campaign_path)], orchestrator
    )

    assert exit_code == 4
    assert payload["ok"] is False
    assert payload["status"] == "failed"
    assert payload["platforms"]["x"]["ok"] is False
    assert payload["platforms"]["x"]["status"] == "failed"


def test_noninjected_probe_reports_actionable_missing_runtime_config(
    capsys, tmp_path: Path
) -> None:
    state_root = tmp_path / "state"

    exit_code, payload = run_cli(
        capsys,
        [
            "--state-root",
            str(state_root),
            "publish",
            "probe",
            "--platform",
            "x",
        ],
    )

    assert exit_code == 2
    assert payload["ok"] is False
    assert payload["error_category"] == "configuration"
    assert "runtime.json" in payload["detail"]
    assert "runtime.json" in payload["next_action"]


def test_noninjected_probe_loads_actual_x_adapter_and_router(
    capsys, tmp_path: Path, monkeypatch
) -> None:
    state_root = tmp_path / "state"
    write_runtime_config(
        state_root,
        python_executable=sys.executable,
    )
    transport = RuntimeTransport()
    monkeypatch.setattr(
        "codex_media_ads.publishing.api_adapters.requests.Session",
        lambda: transport,
    )

    exit_code, payload = run_cli(
        capsys,
        [
            "--state-root",
            str(state_root),
            "publish",
            "probe",
            "--platform",
            "x",
        ],
    )

    assert exit_code == 0
    assert payload == {"ok": True, "platform": "x", "status": "ready"}
    assert len(transport.calls) == 2
    assert all("verify_credentials" in url for _, url in transport.calls)


def test_noninjected_publish_next_uses_runtime_route_without_live_submit(
    capsys, tmp_path: Path, monkeypatch
) -> None:
    state_root = tmp_path / "state"
    _, secret_file = write_runtime_config(
        state_root,
        python_executable=sys.executable,
    )
    transport = RuntimeTransport()
    monkeypatch.setattr(
        "codex_media_ads.publishing.api_adapters.requests.Session",
        lambda: transport,
    )
    media = tmp_path / "video.mp4"
    media.write_bytes(b"not-real-media")
    request = PublishRequest(
        content_id="runtime-content",
        revision=1,
        platform="x",
        account={
            "account_id": "runtime-x",
            "expected_identity": "runtime-creator",
            "mode": "api",
            "secret_file": secret_file,
        },
        media_path=media,
        metadata={"caption": "dry run"},
        idempotency_key="",
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(request.model_dump_json())
    run_cli(
        capsys,
        ["--state-root", str(state_root), "queue", "add", str(request_path)],
    )

    exit_code, payload = run_cli(
        capsys,
        ["--state-root", str(state_root), "publish", "next", "--dry-run"],
    )

    assert exit_code == 0
    assert payload["status"] == "skipped"
    assert payload["evidence"]["dry_run"] is True
    assert len(transport.calls) == 2


def test_noninjected_campaign_build_constructs_configured_creative_pipeline(
    capsys, tmp_path: Path, monkeypatch
) -> None:
    state_root = tmp_path / "state"
    write_runtime_config(
        state_root,
        python_executable=sys.executable,
        include_creative=True,
    )
    transport = RuntimeTransport()
    monkeypatch.setattr(
        "codex_media_ads.publishing.api_adapters.requests.Session",
        lambda: transport,
    )

    def fake_build(self: CreativePipeline, campaign: CampaignManifest):
        assert isinstance(self.image_provider, CodimageProvider)
        assert isinstance(self.narration_provider, CommandNarrationProvider)
        output = state_root / "generated" / campaign.campaign_id / "x.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"rendered-media")
        return SimpleNamespace(
            variant_paths={"x": output},
            dependency=None,
            failure=None,
            manifest_path=output.parent / "build-manifest.json",
        )

    monkeypatch.setattr(CreativePipeline, "build", fake_build)
    campaign_path = write_campaign(tmp_path / "campaign.json")
    campaign = CampaignManifest.model_validate_json(campaign_path.read_text())
    campaign_path.write_text(
        campaign.model_copy(update={"destinations": ["x"]}).model_dump_json(
            exclude_computed_fields=True
        )
    )

    exit_code, payload = run_cli(
        capsys,
        [
            "--state-root",
            str(state_root),
            "campaign",
            "build",
            str(campaign_path),
            "--dry-run",
        ],
    )

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["live_success_count"] == 0
    assert payload["platforms"]["x"]["status"] == "skipped"
    assert len(transport.calls) == 2


def test_noninjected_probe_reports_configured_but_unavailable_route(
    capsys, tmp_path: Path
) -> None:
    state_root = tmp_path / "state"
    runtime_file, _ = write_runtime_config(
        state_root,
        python_executable=sys.executable,
    )
    runtime = json.loads(runtime_file.read_text())
    runtime["accounts"] = {
        "tiktok": {
            "account_id": "runtime-tiktok",
            "expected_identity": "runtime-creator",
            "mode": "api",
        }
    }
    runtime_file.write_text(json.dumps(runtime))

    exit_code, payload = run_cli(
        capsys,
        [
            "--state-root",
            str(state_root),
            "publish",
            "probe",
            "--platform",
            "tiktok",
        ],
    )

    assert exit_code == 3
    assert payload["ok"] is False
    assert payload["status"] == "blocked"
    assert payload["error_category"] == "configuration"
    assert "api route is not configured" in payload["detail"]
    assert "Configure" in payload["next_action"]


def _set_runtime_secret(runtime_file: Path, secret_file: Path) -> None:
    runtime = json.loads(runtime_file.read_text())
    runtime["accounts"]["x"]["secret_file"] = str(secret_file)
    runtime_file.write_text(json.dumps(runtime))


@pytest.mark.parametrize("symlink_kind", ["leaf", "parent"])
def test_runtime_rejects_secret_symlink_without_reading_target(
    capsys, tmp_path: Path, monkeypatch, symlink_kind: str
) -> None:
    state_root = tmp_path / "state"
    runtime_file, _ = write_runtime_config(
        state_root,
        python_executable=sys.executable,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "target.json"
    target.write_text(
        json.dumps(
            {
                "consumer_key": "fake-consumer-key",
                "consumer_secret": "fake-consumer-secret",
                "access_token": "fake-access-token",
                "access_token_secret": "fake-access-secret",
            }
        )
    )
    target.chmod(0o600)
    secrets_root = state_root / "secrets"
    if symlink_kind == "leaf":
        configured_path = secrets_root / "linked.json"
        configured_path.symlink_to(target)
    else:
        linked_parent = secrets_root / "linked-parent"
        linked_parent.symlink_to(outside, target_is_directory=True)
        configured_path = linked_parent / target.name
    _set_runtime_secret(runtime_file, configured_path)

    real_read_text = Path.read_text
    target_reads = 0

    def tracked_read_text(path: Path, *args, **kwargs):
        nonlocal target_reads
        if path == target:
            target_reads += 1
        return real_read_text(path, *args, **kwargs)

    sessions = 0

    def session_factory():
        nonlocal sessions
        sessions += 1
        return RuntimeTransport()

    monkeypatch.setattr(Path, "read_text", tracked_read_text)
    monkeypatch.setattr(
        "codex_media_ads.publishing.api_adapters.requests.Session",
        session_factory,
    )

    exit_code, payload = run_cli(
        capsys,
        [
            "--state-root",
            str(state_root),
            "publish",
            "probe",
            "--platform",
            "x",
        ],
    )

    assert exit_code == 2
    assert payload["error_category"] == "configuration"
    assert "symlink" in payload["detail"].casefold()
    assert target_reads == 0
    assert sessions == 0


def test_runtime_rejects_secret_outside_private_secrets_root(
    capsys, tmp_path: Path
) -> None:
    state_root = tmp_path / "state"
    runtime_file, _ = write_runtime_config(
        state_root,
        python_executable=sys.executable,
    )
    outside = tmp_path / "outside-secret.json"
    outside.write_text('{"access_token":"not-read"}')
    outside.chmod(0o600)
    _set_runtime_secret(runtime_file, outside)

    exit_code, payload = run_cli(
        capsys,
        [
            "--state-root",
            str(state_root),
            "publish",
            "probe",
            "--platform",
            "x",
        ],
    )

    assert exit_code == 2
    assert payload["error_category"] == "configuration"
    assert "private secrets" in payload["detail"].casefold()
