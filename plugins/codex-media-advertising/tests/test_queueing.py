import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from codex_media_ads.queueing import store as queue_store_module
from codex_media_ads.models import AccountConfig, PublishRequest, PublishResult
from codex_media_ads.queueing import QueueStore, ReceiptStore, idempotency_key, retry_decision


@pytest.fixture
def publish_request(tmp_path: Path) -> PublishRequest:
    return PublishRequest(
        content_id="launch-creative",
        revision=2,
        platform="instagram",
        account=AccountConfig(
            account_id="launch-account",
            expected_identity="brand@example.test",
        ),
        media_path=tmp_path / "creative.mp4",
        metadata={"caption": "Launch day"},
        idempotency_key="caller-supplied-value-is-not-authoritative",
    )


@pytest.fixture
def receipt_store(tmp_path: Path) -> ReceiptStore:
    return ReceiptStore(tmp_path)


@pytest.fixture
def queue_store(tmp_path: Path) -> QueueStore:
    return QueueStore(tmp_path)


def test_idempotency_key_uses_exact_request_identity(publish_request):
    assert idempotency_key(
        publish_request.content_id,
        publish_request.platform,
        publish_request.account.account_id,
        publish_request.revision,
    ) == "396baf9a38ef602f7101456e8c95505925fb9f3badc0047487a53a7e36b6afdb"


def test_success_receipt_suppresses_duplicate(queue_store, publish_request):
    queue_store.enqueue(publish_request)
    queue_store.receipts.write_attempt(
        publish_request,
        status="published",
        post_url="https://example.test/post/1",
    )
    assert queue_store.enqueue(publish_request).status == "duplicate_success"


def test_status_without_positive_evidence_does_not_suppress_duplicate(
    queue_store, publish_request
):
    queue_store.enqueue(publish_request)
    claim = queue_store.claim_next(worker_id="worker-a", lease_seconds=300)
    assert claim is not None
    queue_store.fail(claim, PublishResult(status="published"))

    assert queue_store.enqueue(publish_request).status == "enqueued"


def test_active_claim_cannot_be_stolen(queue_store, publish_request):
    queue_store.enqueue(publish_request)
    first = queue_store.claim_next(worker_id="worker-a", lease_seconds=300)
    second = queue_store.claim_next(worker_id="worker-b", lease_seconds=300)
    assert first is not None
    assert second is None


def test_competing_workers_only_create_one_claim(queue_store, publish_request):
    queue_store.enqueue(publish_request)

    with ThreadPoolExecutor(max_workers=8) as workers:
        claims = list(
            workers.map(
                lambda worker_id: queue_store.claim_next(worker_id, 300),
                [f"worker-{index}" for index in range(8)],
            )
        )

    assert sum(claim is not None for claim in claims) == 1


def test_competing_store_instances_only_create_one_claim(tmp_path, publish_request):
    first_store = QueueStore(tmp_path)
    second_store = QueueStore(tmp_path)
    first_store.enqueue(publish_request)

    with ThreadPoolExecutor(max_workers=2) as workers:
        claims = list(
            workers.map(
                lambda item: item[0].claim_next(item[1], 300),
                [(first_store, "worker-a"), (second_store, "worker-b")],
            )
        )

    assert sum(claim is not None for claim in claims) == 1


def test_pending_filename_and_claim_record_use_canonical_key(
    queue_store, publish_request
):
    enqueued = queue_store.enqueue(publish_request)
    assert enqueued.path == queue_store.pending / f"{enqueued.idempotency_key}.json"

    claim = queue_store.claim_next("worker-a", 300)

    assert claim is not None
    record = json.loads(claim.path.read_text())
    assert record["worker_id"] == "worker-a"
    assert record["claimed_at"].endswith("Z")
    assert record["lease_expires_at"].endswith("Z")
    assert isinstance(record["claim_id"], str)
    assert len(record["claim_id"]) >= 32


def test_claim_metadata_is_durable_before_pending_to_claim_replace(
    queue_store, publish_request, monkeypatch
):
    enqueued = queue_store.enqueue(publish_request)
    assert enqueued.path is not None
    claim_path = queue_store.claims / enqueued.path.name
    original_replace = queue_store_module.os.replace

    def fail_claim_replace(source, destination):
        if Path(source) == enqueued.path and Path(destination) == claim_path:
            raise OSError("injected claim movement failure")
        return original_replace(source, destination)

    monkeypatch.setattr(queue_store_module.os, "replace", fail_claim_replace)

    with pytest.raises(OSError, match="injected claim movement failure"):
        queue_store.claim_next("worker-a", 60)

    record = json.loads(enqueued.path.read_text())
    assert record["worker_id"] == "worker-a"
    assert record["claim_id"]
    assert record["claimed_at"].endswith("Z")
    assert record["lease_expires_at"].endswith("Z")
    assert not claim_path.exists()


@pytest.mark.parametrize("action", ["complete", "fail", "requeue"])
def test_stale_worker_cannot_consume_recovered_claim(
    tmp_path, publish_request, action
):
    now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
    queue_store = QueueStore(tmp_path, clock=lambda: now)
    queue_store.enqueue(publish_request)
    stale = queue_store.claim_next("worker-a", 60)
    assert stale is not None

    now += timedelta(seconds=61)
    assert queue_store.recover_expired() == 1
    current = queue_store.claim_next("worker-b", 60)
    assert current is not None
    assert current.claim_id != stale.claim_id

    result = PublishResult(status="failed", error_category="network")
    with pytest.raises(PermissionError, match="claim owner"):
        if action == "requeue":
            queue_store.requeue(stale)
        else:
            getattr(queue_store, action)(stale, result)

    current_record = json.loads(current.path.read_text())
    assert current_record["worker_id"] == "worker-b"
    assert current_record["claim_id"] == current.claim_id


def test_expired_claim_is_recovered_without_success_receipt(
    tmp_path, publish_request
):
    now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
    queue_store = QueueStore(tmp_path, clock=lambda: now)
    queue_store.enqueue(publish_request)
    claim = queue_store.claim_next(worker_id="worker-a", lease_seconds=60)
    assert claim is not None

    now += timedelta(seconds=61)

    assert queue_store.recover_expired() == 1
    assert queue_store.claim_next(worker_id="worker-b", lease_seconds=60) is not None


def test_expired_claim_is_not_recovered_after_success(tmp_path, publish_request):
    now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
    queue_store = QueueStore(tmp_path, clock=lambda: now)
    queue_store.enqueue(publish_request)
    claim = queue_store.claim_next(worker_id="worker-a", lease_seconds=60)
    assert claim is not None
    queue_store.receipts.write_attempt(
        publish_request,
        status="submitted",
        evidence={"submission_id": "sub-1"},
    )

    now += timedelta(seconds=61)

    assert queue_store.recover_expired() == 0
    assert queue_store.claim_next(worker_id="worker-b", lease_seconds=60) is None


@pytest.mark.parametrize("category", ["ambiguous_submit", "failed", "blocked"])
def test_non_transient_submission_is_not_retryable(category):
    decision = retry_decision(category=category, attempt=1, max_retries=1)
    assert decision.retry is False


def test_retry_boundary_and_delay_are_explicit():
    assert retry_decision("network", attempt=1, max_retries=1).retry is True
    assert retry_decision("network", attempt=2, max_retries=1).retry is False
    assert retry_decision("rate_limit", attempt=6, max_retries=6).delay_seconds == 60


@pytest.mark.parametrize("attempt", [2, 3, 10])
def test_retry_ceiling_is_one_even_when_configured_higher(attempt):
    assert retry_decision("network", attempt=attempt, max_retries=10).retry is False


def test_daily_caps_use_configured_timezone(receipt_store, publish_request):
    receipt_store.write_attempt(
        publish_request,
        status="published",
        post_url="https://example.test/post/1",
        occurred_at="2026-07-14T06:30:00Z",
    )
    assert receipt_store.count_successes_on_date(
        "2026-07-13", "America/Los_Angeles"
    ) == 1


def test_dry_run_success_does_not_suppress_duplicate(queue_store, publish_request):
    dry_request = publish_request.model_copy(update={"dry_run": True})
    receipt = queue_store.receipts.write_attempt(
        dry_request,
        status="published",
        post_url="https://example.test/dry-run-only",
    )

    assert receipt["dry_run"] is True
    assert queue_store.receipts.latest_success(dry_request) is None
    assert queue_store.enqueue(dry_request).status == "enqueued"


def test_dry_run_success_does_not_consume_daily_cap(receipt_store, publish_request):
    dry_request = publish_request.model_copy(update={"dry_run": True})
    receipt_store.write_attempt(
        dry_request,
        status="submitted",
        platform_id="dry-submission",
        occurred_at="2026-07-14T12:00:00Z",
    )

    assert receipt_store.count_successes_on_date("2026-07-14", "UTC") == 0


def test_two_live_failures_pause_only_platform_account(
    receipt_store, publish_request
):
    receipt_store.write_attempt(
        publish_request, status="failed", error_category="platform_ui"
    )
    assert receipt_store.consecutive_failures("instagram", "launch-account") == 1
    assert not receipt_store.is_paused("instagram", "launch-account")

    receipt_store.write_attempt(
        publish_request, status="unknown", error_category="ambiguous_submit"
    )

    assert receipt_store.consecutive_failures("instagram", "launch-account") == 2
    assert receipt_store.is_paused("instagram", "launch-account")
    assert not receipt_store.is_paused("threads", "launch-account")
    assert not receipt_store.is_paused("instagram", "different-account")


def test_dry_run_failure_does_not_increment_pause_counter(
    receipt_store, publish_request
):
    dry_request = publish_request.model_copy(update={"dry_run": True})
    receipt_store.write_attempt(
        dry_request, status="failed", error_category="platform_ui"
    )

    assert receipt_store.consecutive_failures("instagram", "launch-account") == 0
    assert not receipt_store.is_paused("instagram", "launch-account")


def test_verified_live_success_resets_pause_counter(receipt_store, publish_request):
    for _ in range(2):
        receipt_store.write_attempt(
            publish_request, status="failed", error_category="network"
        )
    assert receipt_store.is_paused("instagram", "launch-account")

    receipt_store.write_attempt(
        publish_request,
        status="published",
        post_url="https://example.test/post/recovery",
    )

    assert receipt_store.consecutive_failures("instagram", "launch-account") == 0
    assert not receipt_store.is_paused("instagram", "launch-account")


def test_manual_resume_clears_pause_counter(receipt_store, publish_request):
    for _ in range(2):
        receipt_store.write_attempt(
            publish_request, status="blocked", error_category="authentication"
        )
    assert receipt_store.is_paused("instagram", "launch-account")

    receipt_store.resume("instagram", "launch-account")

    assert receipt_store.consecutive_failures("instagram", "launch-account") == 0
    assert not receipt_store.is_paused("instagram", "launch-account")


def test_failed_attempt_is_appended_without_counting_toward_cap(
    receipt_store, publish_request
):
    receipt_store.write_attempt(
        publish_request,
        status="failed",
        error_category="network",
        occurred_at="2026-07-14T12:00:00Z",
    )

    lines = (receipt_store.root / "receipts.jsonl").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["status"] == "failed"
    assert receipt_store.count_successes_on_date("2026-07-14", "UTC") == 0


def test_diagnostic_evidence_does_not_turn_status_into_success(
    receipt_store, publish_request
):
    receipt_store.write_attempt(
        publish_request,
        status="submitted",
        evidence={"screenshot": "composer.png", "detail": "button was clicked"},
        occurred_at="2026-07-14T12:00:00Z",
    )

    assert receipt_store.latest_success(publish_request) is None
    assert receipt_store.count_successes_on_date("2026-07-14", "UTC") == 0


@pytest.mark.parametrize("invalid_id", [None, False, "", [], {}])
def test_invalid_platform_ids_are_not_positive_evidence(
    receipt_store, publish_request, invalid_id
):
    receipt_store.write_attempt(
        publish_request,
        status="submitted",
        platform_id=invalid_id,
        occurred_at="2026-07-14T12:00:00Z",
    )

    assert receipt_store.latest_success(publish_request) is None
    assert receipt_store.count_successes_on_date("2026-07-14", "UTC") == 0


@pytest.mark.parametrize("invalid_id", [None, False, "", [], {}])
def test_invalid_nested_evidence_ids_are_not_positive(
    receipt_store, publish_request, invalid_id
):
    receipt_store.write_attempt(
        publish_request,
        status="scheduled",
        evidence={"schedule_id": invalid_id},
        occurred_at="2026-07-14T12:00:00Z",
    )

    assert receipt_store.latest_success(publish_request) is None


@pytest.mark.parametrize(
    "evidence_kwargs",
    [
        {"platform_id": 12345},
        {"evidence": {"submission_id": 67890}},
        {"evidence": {"confirmed": True}},
    ],
)
def test_integer_ids_and_explicit_true_flags_are_positive_evidence(
    receipt_store, publish_request, evidence_kwargs
):
    receipt_store.write_attempt(
        publish_request,
        status="submitted",
        occurred_at="2026-07-14T12:00:00Z",
        **evidence_kwargs,
    )

    assert receipt_store.latest_success(publish_request) is not None
    assert receipt_store.count_successes_on_date("2026-07-14", "UTC") == 1


def test_later_failure_does_not_replace_latest_success(
    receipt_store, publish_request
):
    receipt_store.write_attempt(
        publish_request,
        status="scheduled",
        evidence={"schedule_id": "scheduled-1"},
        occurred_at="2026-07-14T12:00:00Z",
    )
    receipt_store.write_attempt(
        publish_request,
        status="failed",
        error_category="platform_ui",
        occurred_at="2026-07-14T12:01:00Z",
    )

    latest = receipt_store.latest_success(publish_request)
    assert latest is not None
    assert latest["status"] == "scheduled"
    assert latest["evidence"] == {"schedule_id": "scheduled-1"}


def test_complete_and_fail_move_claims_and_write_receipts(queue_store, publish_request):
    queue_store.enqueue(publish_request)
    first = queue_store.claim_next("worker-a", 60)
    assert first is not None
    queue_store.fail(
        first,
        PublishResult(
            status="failed", error_category="network", detail="connection reset"
        ),
    )

    queue_store.enqueue(publish_request)
    second = queue_store.claim_next("worker-b", 60)
    assert second is not None
    queue_store.complete(
        second,
        PublishResult(
            status="published",
            post_url="https://example.test/post/1",
            evidence={"screenshot": "proof.png"},
        ),
    )

    key = idempotency_key(
        publish_request.content_id,
        publish_request.platform,
        publish_request.account.account_id,
        publish_request.revision,
    )
    assert (queue_store.failed / f"{key}.json").is_file()
    assert (queue_store.completed / f"{key}.json").is_file()
    assert queue_store.receipts.latest_success(publish_request) is not None
