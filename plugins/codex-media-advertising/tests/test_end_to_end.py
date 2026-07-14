from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_media_ads.models import AccountConfig, CampaignManifest, PublishResult, PublishStatus
from codex_media_ads.orchestrator import Orchestrator
from codex_media_ads.publishing.base import ProbeResult, ValidationResult
from codex_media_ads.queueing import QueueStore


PLATFORMS = ("instagram", "tiktok", "youtube", "x", "facebook", "threads")


class _Adapter:
    def __init__(self, platform: str) -> None:
        self.platform = platform

    def probe_auth(self, account: AccountConfig) -> ProbeResult:
        return ProbeResult(authenticated=True, observed_identity=account.expected_identity)

    def validate(self, request) -> ValidationResult:
        return ValidationResult(ok=True)

    def publish(self, request) -> PublishResult:
        if self.platform == "tiktok":
            return PublishResult(
                status=PublishStatus.FAILED,
                error_category="validation",
                detail="synthetic fixture rejection",
            )
        return PublishResult(
            status=PublishStatus.PUBLISHED,
            platform_id=f"synthetic-{self.platform}-id",
            evidence={"confirmed": True},
        )


class _Builder:
    def __init__(self, root: Path) -> None:
        self.root = root

    def build(self, campaign: CampaignManifest):
        build = self.root / campaign.campaign_id
        build.mkdir(parents=True)
        master = build / "master.mp4"
        master.write_bytes(b"synthetic-master")
        variants = {}
        for platform in campaign.destinations:
            path = build / f"{platform}.mp4"
            path.write_bytes(f"synthetic-{platform}".encode())
            variants[platform] = path
        manifest = build / "build-manifest.json"
        manifest.write_text('{"synthetic":true}\n')
        return SimpleNamespace(master_path=master, variant_paths=variants, manifest_path=manifest)


@pytest.fixture
def campaign_path(tmp_path: Path) -> Path:
    path = tmp_path / "campaign.json"
    path.write_text(
        '{"schema_version":"1","brand":"Example","campaign_id":"e2e",'
        '"rights_confirmed":true,"audience":"builders","offer":"Try it",'
        '"proof_points":["fast"],"calls_to_action":["Learn more"],'
        '"visual_prompts":["A clean synthetic product scene"],'
        '"narration":"A synthetic test campaign.","duration_seconds":15,'
        '"destinations":["instagram","tiktok","youtube","x","facebook","threads"],'
        '"timezone":"UTC","schedule":["2026-07-14T12:00:00Z"],"daily_cap":20,'
        '"retry_limit":0,"failure_pause_threshold":2}\n'
    )
    return path


@pytest.fixture
def e2e_runtime(tmp_path: Path):
    accounts = {
        platform: AccountConfig(
            account_id=f"synthetic-{platform}",
            expected_identity=f"{platform}@example.test",
        )
        for platform in PLATFORMS
    }
    adapters = {platform: _Adapter(platform) for platform in PLATFORMS}
    service = Orchestrator(
        queue_store=QueueStore(tmp_path / "state"),
        adapters=adapters,
        accounts=accounts,
        builder=_Builder(tmp_path / "generated"),
        clock=lambda: datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
        retry_limit=0,
    )

    class Runtime:
        receipt_root = service.queue_store.receipts.root

        def run(self, path: Path, *, live: bool):
            campaign = CampaignManifest.model_validate_json(path.read_text())
            result = service.run_campaign(campaign, live=live)
            result.build = SimpleNamespace(master_path=Path(result.build_manifest).parent / "master.mp4")
            return result

    return Runtime()


def test_campaign_builds_and_publishes_six_independent_records(e2e_runtime, campaign_path):
    result = e2e_runtime.run(campaign_path, live=True)

    assert result.build.master_path.exists()
    assert set(result.platforms) == set(PLATFORMS)
    assert result.platforms["tiktok"].status == PublishStatus.FAILED
    assert all(
        result.platforms[name].status in {
            PublishStatus.PUBLISHED,
            PublishStatus.SUBMITTED,
            PublishStatus.SCHEDULED,
        }
        for name in {"instagram", "youtube", "x", "facebook", "threads"}
    )
    assert len(list(e2e_runtime.receipt_root.glob("*.json"))) == 6
