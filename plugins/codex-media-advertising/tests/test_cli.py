from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from codex_media_ads import cli
from codex_media_ads.models import CampaignManifest, PublishStatus

from test_orchestrator import PLATFORMS, orchestrator, x_request


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
