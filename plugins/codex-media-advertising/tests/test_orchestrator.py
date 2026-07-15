from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_media_ads.models import (
    AccountConfig,
    CampaignManifest,
    PublishRequest,
    PublishResult,
    PublishStatus,
)
from codex_media_ads.orchestrator import Orchestrator
from codex_media_ads.publishing.base import ProbeResult, ValidationResult
from codex_media_ads.queueing import QueueStore
from codex_media_ads.queueing import idempotency_key


PLATFORMS = ("instagram", "tiktok", "youtube", "x", "facebook", "threads")


class FakeAdapter:
    def __init__(self, platform: str) -> None:
        self.platform = platform
        self.next_results: list[PublishResult] = []
        self.publish_calls = 0
        self.probe_calls = 0
        self.probed_accounts: list[AccountConfig] = []
        self.validation = ValidationResult(ok=True)
        self.published_requests: list[PublishRequest] = []
        self.probe = ProbeResult(
            authenticated=True,
            observed_identity=f"{platform}-identity",
        )

    @property
    def next_result(self) -> PublishResult | None:
        return self.next_results[0] if self.next_results else None

    @next_result.setter
    def next_result(self, value: PublishResult) -> None:
        self.next_results = [value]

    def probe_auth(self, account: AccountConfig) -> ProbeResult:
        self.probe_calls += 1
        self.probed_accounts.append(account)
        return self.probe

    def validate(self, request: PublishRequest) -> ValidationResult:
        return self.validation

    def publish(self, request: PublishRequest) -> PublishResult:
        self.publish_calls += 1
        self.published_requests.append(request)
        if self.next_results:
            return self.next_results.pop(0)
        return verified(PublishStatus.PUBLISHED, self.platform)


class FakeRouter:
    def __init__(self, adapters: dict[str, FakeAdapter]) -> None:
        self.adapters = adapters
        self.select_calls: list[str] = []

    def select(self, account: AccountConfig, platform: str) -> FakeAdapter:
        self.select_calls.append(platform)
        return self.adapters[platform]


class FakeBuilder:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls = 0

    def build(self, campaign: CampaignManifest):
        self.calls += 1
        self.root.mkdir(parents=True, exist_ok=True)
        variants = {}
        for platform in campaign.destinations:
            path = self.root / f"{platform}.mp4"
            path.write_bytes(b"media")
            variants[platform] = path
        return SimpleNamespace(
            variant_paths=variants,
            dependency=None,
            failure=None,
            manifest_path=self.root / "build-manifest.json",
        )


def verified(status: PublishStatus, platform: str) -> PublishResult:
    return PublishResult(
        status=status,
        platform_id=f"{platform}-post",
        evidence={"verified": True},
    )


def failed(category: str) -> PublishResult:
    return PublishResult(
        status=PublishStatus.FAILED,
        error_category=category,
        detail=f"{category} failure",
    )


@pytest.fixture
def campaign() -> CampaignManifest:
    return CampaignManifest(
        schema_version="1",
        brand="Example",
        campaign_id="launch",
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


@pytest.fixture
def orchestrator(tmp_path: Path):
    now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
    adapters = {platform: FakeAdapter(platform) for platform in PLATFORMS}
    accounts = {
        platform: AccountConfig(
            account_id=f"{platform}-account",
            expected_identity=f"{platform}-identity",
        )
        for platform in PLATFORMS
    }
    router = FakeRouter(adapters)
    service = Orchestrator(
        queue_store=QueueStore(tmp_path / "state", clock=lambda: now),
        router=router,
        adapters=adapters,
        accounts=accounts,
        builder=FakeBuilder(tmp_path / "generated"),
        clock=lambda: now,
        daily_cap=20,
        retry_limit=1,
        failure_pause_threshold=2,
    )
    return service


@pytest.fixture
def x_request(orchestrator: Orchestrator, tmp_path: Path) -> PublishRequest:
    media = tmp_path / "x.mp4"
    media.write_bytes(b"media")
    return PublishRequest(
        content_id="content-x",
        revision=1,
        platform="x",
        account=orchestrator.accounts["x"],
        media_path=media,
        metadata={"caption": "hello"},
        idempotency_key="",
    )


def test_one_platform_failure_does_not_block_others(
    orchestrator: Orchestrator, campaign: CampaignManifest
) -> None:
    orchestrator.adapters["tiktok"].next_result = failed("platform_ui")

    result = orchestrator.run_campaign(campaign, live=True)

    assert result.platforms["tiktok"].status == PublishStatus.FAILED
    assert result.platforms["youtube"].status in {
        PublishStatus.PUBLISHED,
        PublishStatus.SUBMITTED,
        PublishStatus.SCHEDULED,
    }


def test_two_live_failures_pause_only_platform_account(
    orchestrator: Orchestrator, x_request: PublishRequest
) -> None:
    orchestrator.adapters["x"].next_results = [failed("validation"), failed("validation")]

    orchestrator.publish(x_request)
    orchestrator.publish(x_request.model_copy(update={"revision": 2}))

    account_id = x_request.account.account_id
    assert orchestrator.pause_store.is_paused("x", account_id)
    assert not orchestrator.pause_store.is_paused("threads", account_id)


def test_prior_verified_success_prevents_duplicate_publish(
    orchestrator: Orchestrator, x_request: PublishRequest
) -> None:
    first = orchestrator.publish(x_request)
    second = orchestrator.publish(x_request)

    assert first.status == PublishStatus.PUBLISHED
    assert second.status == PublishStatus.SKIPPED
    assert orchestrator.adapters["x"].publish_calls == 1


def test_pause_is_checked_before_claim(
    orchestrator: Orchestrator, x_request: PublishRequest
) -> None:
    orchestrator.pause_store.pause("x", x_request.account.account_id)

    result = orchestrator.publish(x_request)

    assert result.status == PublishStatus.BLOCKED
    assert not list(orchestrator.queue_store.claims.glob("*.json"))
    assert orchestrator.adapters["x"].publish_calls == 0


def test_daily_cap_is_checked_before_claim(
    orchestrator: Orchestrator, x_request: PublishRequest
) -> None:
    orchestrator.daily_cap = 1
    earlier = x_request.model_copy(update={"content_id": "earlier"})
    orchestrator.publish(earlier)

    result = orchestrator.publish(x_request)

    assert result.status == PublishStatus.SKIPPED
    assert not list(orchestrator.queue_store.claims.glob("*.json"))
    assert orchestrator.adapters["x"].publish_calls == 1


def test_route_selected_once_then_identity_and_metadata_are_gated(
    orchestrator: Orchestrator, x_request: PublishRequest
) -> None:
    orchestrator.adapters["x"].probe = ProbeResult(
        authenticated=True,
        observed_identity="wrong-account",
    )

    result = orchestrator.publish(x_request)

    assert result.status == PublishStatus.BLOCKED
    assert result.error_category == "identity_mismatch"
    assert orchestrator.router.select_calls == ["x"]
    assert orchestrator.adapters["x"].publish_calls == 0
    assert list(orchestrator.queue_store.failed.glob("*.json"))


def test_attempt_receipt_exists_before_claim_transition(
    orchestrator: Orchestrator, x_request: PublishRequest, monkeypatch
) -> None:
    original_fail = orchestrator.queue_store.fail

    def checking_fail(claim, result):
        receipt = original_fail(claim, result)
        assert receipt["status"] == "failed"
        assert orchestrator.queue_store.receipts._latest_path(
            claim.idempotency_key
        ).is_file()
        return receipt

    monkeypatch.setattr(orchestrator.queue_store, "fail", checking_fail)
    orchestrator.adapters["x"].next_result = failed("validation")

    orchestrator.publish(x_request)


def test_success_status_without_positive_evidence_is_failed(
    orchestrator: Orchestrator, x_request: PublishRequest
) -> None:
    orchestrator.adapters["x"].next_result = PublishResult(
        status=PublishStatus.PUBLISHED
    )

    result = orchestrator.publish(x_request)

    assert result.status == PublishStatus.FAILED
    assert result.error_category == "ambiguous_submit"
    assert list(orchestrator.queue_store.failed.glob("*.json"))


def test_classified_transient_failure_retries_at_most_once(
    orchestrator: Orchestrator, x_request: PublishRequest
) -> None:
    orchestrator.adapters["x"].next_results = [failed("network"), failed("network")]

    first = orchestrator.publish(x_request)
    second = orchestrator.publish(x_request)
    third = orchestrator.publish(x_request)

    assert first.evidence["queue_action"] == "requeued"
    assert second.status == PublishStatus.FAILED
    assert third.status == PublishStatus.BLOCKED
    assert third.error_category == "platform_paused"
    assert orchestrator.adapters["x"].publish_calls == 2
    assert not list(orchestrator.queue_store.pending.glob("*.json"))


def test_ambiguous_submit_is_never_retried(
    orchestrator: Orchestrator, x_request: PublishRequest
) -> None:
    orchestrator.adapters["x"].next_result = failed("ambiguous_submit")

    first = orchestrator.publish(x_request)
    second = orchestrator.publish(x_request)

    assert first.status == PublishStatus.FAILED
    assert second.status == PublishStatus.FAILED
    assert orchestrator.adapters["x"].publish_calls == 1
    assert not list(orchestrator.queue_store.pending.glob("*.json"))


def test_dry_run_never_counts_success_or_changes_pause_or_cap(
    orchestrator: Orchestrator, x_request: PublishRequest
) -> None:
    request = x_request.model_copy(update={"dry_run": True})
    before = orchestrator.queue_store.receipts.count_successes_on_date(
        "2026-07-14", "UTC"
    )

    result = orchestrator.publish(request)

    assert result.status == PublishStatus.SKIPPED
    assert result.evidence["dry_run"] is True
    assert orchestrator.adapters["x"].publish_calls == 0
    assert orchestrator.queue_store.receipts.count_successes_on_date(
        "2026-07-14", "UTC"
    ) == before
    assert not orchestrator.pause_store.is_paused(
        "x", x_request.account.account_id
    )


def test_publish_atomically_claims_the_requested_record(
    orchestrator: Orchestrator, x_request: PublishRequest
) -> None:
    x_key = idempotency_key(
        x_request.content_id,
        x_request.platform,
        x_request.account.account_id,
        x_request.revision,
    )
    for index in range(1000):
        other = x_request.model_copy(
            update={
                "content_id": f"other-{index}",
                "platform": "instagram",
                "account": orchestrator.accounts["instagram"],
            }
        )
        other_key = idempotency_key(
            other.content_id,
            other.platform,
            other.account.account_id,
            other.revision,
        )
        if other_key < x_key:
            break
    else:
        raise AssertionError("could not construct a lower-sorting queue key")
    orchestrator.queue_store.enqueue(other)

    result = orchestrator.publish(x_request)

    assert result.status == PublishStatus.PUBLISHED
    assert orchestrator.adapters["x"].publish_calls == 1
    assert orchestrator.adapters["instagram"].publish_calls == 0
    assert (orchestrator.queue_store.pending / f"{other_key}.json").is_file()


def test_campaign_does_not_consume_its_requeued_retry_in_the_same_run(
    orchestrator: Orchestrator, campaign: CampaignManifest
) -> None:
    orchestrator.adapters["tiktok"].next_result = failed("network")

    result = orchestrator.run_campaign(campaign, live=True)

    assert result.platforms["tiktok"].status == PublishStatus.FAILED
    assert result.platforms["tiktok"].evidence["queue_action"] == "requeued"
    assert orchestrator.adapters["tiktok"].publish_calls == 1
    assert any(
        json.loads(path.read_text())["request"]["platform"] == "tiktok"
        for path in orchestrator.queue_store.pending.glob("*.json")
    )


def test_daily_cap_uses_the_configured_local_date(tmp_path: Path) -> None:
    now = datetime(2026, 7, 15, 1, tzinfo=timezone.utc)
    adapter = FakeAdapter("x")
    account = AccountConfig(
        account_id="x-account", expected_identity="x-identity"
    )
    service = Orchestrator(
        queue_store=QueueStore(tmp_path / "state", clock=lambda: now),
        adapters={"x": adapter},
        accounts={"x": account},
        clock=lambda: now,
        daily_cap=1,
        timezone_name="America/Los_Angeles",
    )
    media = tmp_path / "x.mp4"
    media.write_bytes(b"media")
    request = PublishRequest(
        content_id="first",
        revision=1,
        platform="x",
        account=account,
        media_path=media,
        metadata={"caption": "hello"},
        idempotency_key="",
    )
    service.publish(request)

    result = service.publish(request.model_copy(update={"content_id": "second"}))

    assert result.status == PublishStatus.SKIPPED
    assert result.evidence["reason"] == "daily_cap"


def test_unknown_ambiguous_result_is_terminal_failed_work(
    orchestrator: Orchestrator, x_request: PublishRequest
) -> None:
    orchestrator.adapters["x"].next_results = [
        PublishResult(
            status=PublishStatus.UNKNOWN,
            error_category="ambiguous_submit",
            detail="final submit outcome is unknown",
        ),
        verified(PublishStatus.PUBLISHED, "x"),
    ]

    first = orchestrator.publish(x_request)
    second = orchestrator.publish(x_request)

    assert first.status == PublishStatus.FAILED
    assert first.error_category == "ambiguous_submit"
    assert second.status == PublishStatus.FAILED
    assert orchestrator.adapters["x"].publish_calls == 1
    assert not list(orchestrator.queue_store.pending.glob("*.json"))


def _lower_sorting_request(
    orchestrator: Orchestrator,
    request: PublishRequest,
    *,
    platform: str,
) -> tuple[PublishRequest, str]:
    target_key = idempotency_key(
        request.content_id,
        request.platform,
        request.account.account_id,
        request.revision,
    )
    for index in range(2000):
        candidate = request.model_copy(
            update={
                "content_id": f"race-{index}",
                "platform": platform,
                "account": orchestrator.accounts[platform],
            }
        )
        candidate_key = idempotency_key(
            candidate.content_id,
            candidate.platform,
            candidate.account.account_id,
            candidate.revision,
        )
        if candidate_key < target_key:
            return candidate, candidate_key
    raise AssertionError("could not construct a lower-sorting queue key")


def test_process_next_does_not_publish_record_inserted_after_precheck(
    orchestrator: Orchestrator, x_request: PublishRequest, monkeypatch
) -> None:
    orchestrator.queue_store.enqueue(x_request)
    inserted, _ = _lower_sorting_request(
        orchestrator, x_request, platform="instagram"
    )
    orchestrator.pause_store.pause(
        inserted.platform, inserted.account.account_id
    )
    original_preclaim = orchestrator._preclaim_result
    calls = 0

    def insert_during_precheck(request: PublishRequest):
        nonlocal calls
        calls += 1
        result = original_preclaim(request)
        if calls == 1:
            orchestrator.queue_store.enqueue(inserted)
        return result

    monkeypatch.setattr(orchestrator, "_preclaim_result", insert_during_precheck)

    result = orchestrator.process_next(live=True)

    assert result.status == PublishStatus.PUBLISHED
    assert orchestrator.adapters["x"].publish_calls == 1
    assert orchestrator.adapters["instagram"].publish_calls == 0


def test_paused_first_record_does_not_starve_next_platform(
    orchestrator: Orchestrator, x_request: PublishRequest
) -> None:
    paused, _ = _lower_sorting_request(
        orchestrator, x_request, platform="instagram"
    )
    orchestrator.pause_store.pause(paused.platform, paused.account.account_id)
    orchestrator.queue_store.enqueue(paused)
    orchestrator.queue_store.enqueue(x_request)

    result = orchestrator.process_next(live=True)

    assert result.status == PublishStatus.PUBLISHED
    assert orchestrator.adapters["x"].publish_calls == 1
    assert orchestrator.adapters["instagram"].publish_calls == 0


def test_process_next_rechecks_record_changed_between_precheck_and_claim(
    orchestrator: Orchestrator, x_request: PublishRequest, monkeypatch
) -> None:
    enqueue = orchestrator.queue_store.enqueue(x_request)
    assert enqueue.path is not None
    prechecked_captions: list[str] = []
    original_preclaim = orchestrator._preclaim_result
    original_claim = orchestrator._claim_for
    mutated = False

    def record_precheck(request: PublishRequest):
        prechecked_captions.append(str(request.metadata["caption"]))
        return original_preclaim(request)

    def mutate_before_claim(request: PublishRequest):
        nonlocal mutated
        if not mutated:
            value = json.loads(enqueue.path.read_text())
            value["request"]["metadata"]["caption"] = "changed-after-precheck"
            orchestrator.queue_store._write_json(enqueue.path, value)
            mutated = True
        return original_claim(request)

    monkeypatch.setattr(orchestrator, "_preclaim_result", record_precheck)
    monkeypatch.setattr(orchestrator, "_claim_for", mutate_before_claim)

    result = orchestrator.process_next(live=True)

    assert result.status == PublishStatus.PUBLISHED
    assert prechecked_captions == ["hello", "changed-after-precheck"]
    assert orchestrator.adapters["x"].published_requests[0].metadata["caption"] == (
        "changed-after-precheck"
    )


def test_successful_identity_probe_clears_only_receipt_failure_pause(
    orchestrator: Orchestrator, x_request: PublishRequest
) -> None:
    failures = orchestrator.queue_store.receipts
    failures.write_attempt(x_request, failed("validation"))
    failures.write_attempt(
        x_request.model_copy(update={"revision": 2}), failed("validation")
    )
    assert orchestrator.pause_store.is_paused(
        "x", x_request.account.account_id
    )

    probe = orchestrator.probe("x")

    assert probe.authenticated is True
    assert not orchestrator.pause_store.is_paused(
        "x", x_request.account.account_id
    )


def test_successful_identity_probe_does_not_clear_manual_pause(
    orchestrator: Orchestrator, x_request: PublishRequest
) -> None:
    orchestrator.pause_store.pause("x", x_request.account.account_id)

    probe = orchestrator.probe("x")

    assert probe.authenticated is True
    assert orchestrator.pause_store.is_paused("x", x_request.account.account_id)


def test_queue_add_canonicalizes_all_account_authority_fields(
    orchestrator: Orchestrator, x_request: PublishRequest, tmp_path: Path
) -> None:
    malicious = x_request.model_copy(
        update={
            "account": x_request.account.model_copy(
                update={
                    "expected_identity": "attacker-identity",
                    "mode": "api",
                    "secret_file": tmp_path / "attacker-secret.json",
                    "chrome_profile": "Attacker Profile",
                    "cdp_url": "http://127.0.0.1:65535",
                }
            )
        }
    )

    enqueue = orchestrator.add_to_queue(malicious)

    assert enqueue.status == "enqueued"
    queued = PublishRequest.model_validate(
        json.loads(enqueue.path.read_text())["request"]
    )
    assert queued.account == orchestrator.accounts["x"]
    assert queued.account.secret_file is None
    assert "attacker" not in enqueue.path.read_text()


def test_direct_malicious_queue_record_uses_configured_account_only(
    orchestrator: Orchestrator, x_request: PublishRequest, tmp_path: Path
) -> None:
    malicious = x_request.model_copy(
        update={
            "account": x_request.account.model_copy(
                update={
                    "expected_identity": "attacker-identity",
                    "mode": "api",
                    "secret_file": tmp_path / "attacker-secret.json",
                    "chrome_profile": "Attacker Profile",
                }
            )
        }
    )
    orchestrator.queue_store.enqueue(malicious)

    result = orchestrator.process_next(live=True)

    configured = orchestrator.accounts["x"]
    assert result.status == PublishStatus.PUBLISHED
    assert orchestrator.adapters["x"].probed_accounts == [configured]
    assert orchestrator.adapters["x"].published_requests[0].account == configured
    completed = next(orchestrator.queue_store.completed.glob("*.json"))
    stored = json.loads(completed.read_text())
    assert PublishRequest.model_validate(stored["request"]).account == configured


def test_mismatched_queued_account_id_blocks_before_claim_or_adapter(
    orchestrator: Orchestrator, x_request: PublishRequest
) -> None:
    malicious = x_request.model_copy(
        update={
            "account": x_request.account.model_copy(
                update={"account_id": "attacker-account"}
            )
        }
    )
    orchestrator.queue_store.enqueue(malicious)

    result = orchestrator.process_next(live=True)

    assert result.status == PublishStatus.BLOCKED
    assert result.error_category == "configuration"
    assert "account" in result.detail.casefold()
    assert orchestrator.adapters["x"].probe_calls == 0
    assert orchestrator.adapters["x"].publish_calls == 0
    assert not list(orchestrator.queue_store.claims.glob("*.json"))
