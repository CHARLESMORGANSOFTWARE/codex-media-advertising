from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

from .config import SECRET_FILE_MODE, redact
from .models import (
    AccountConfig,
    CampaignManifest,
    PublishRequest,
    PublishResult,
    PublishStatus,
)
from .optimization import MetadataPack, optimize_for_platform
from .publishing.base import (
    ErrorCategory,
    ProbeResult,
    PublisherAdapter,
    normalize_adapter_error,
    probe_identity,
    redact_diagnostic,
)
from .queueing import QueueClaim, QueueStore, idempotency_key, retry_decision


SUCCESS_STATUSES = {
    PublishStatus.PUBLISHED,
    PublishStatus.SUBMITTED,
    PublishStatus.SCHEDULED,
}
POSITIVE_EVIDENCE_FLAGS = {
    "confirmed",
    "published",
    "scheduled",
    "submitted",
    "success",
    "verified",
}
POSITIVE_EVIDENCE_IDS = {
    "post_id",
    "publication_id",
    "schedule_id",
    "submission_id",
}


@dataclass
class CampaignRunResult:
    platforms: dict[str, PublishResult] = field(default_factory=dict)
    build_manifest: str = ""
    live_success_count: int = 0


class PauseStore:
    """Persistent manual pauses plus receipt-derived consecutive-failure pauses."""

    def __init__(self, queue_store: QueueStore, *, threshold: int = 2) -> None:
        self.receipts = queue_store.receipts
        self.path = queue_store.root / "platform-pauses.json"
        self.threshold = threshold
        self._lock = threading.Lock()

    def _read(self) -> set[str]:
        try:
            value = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return set()
        return {str(item) for item in value if isinstance(item, str)}

    @staticmethod
    def _key(platform: str, account_id: str) -> str:
        return json.dumps([platform, account_id], separators=(",", ":"))

    def _write(self, values: set[str]) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            SECRET_FILE_MODE,
        )
        try:
            os.write(fd, json.dumps(sorted(values)).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def is_paused(self, platform: str, account_id: str) -> bool:
        with self._lock:
            manual = self._key(platform, account_id) in self._read()
        return manual or self.receipts.is_paused(
            platform, account_id, threshold=self.threshold
        )

    def pause(self, platform: str, account_id: str) -> None:
        with self._lock:
            values = self._read()
            values.add(self._key(platform, account_id))
            self._write(values)

    def resume(self, platform: str, account_id: str) -> None:
        with self._lock:
            values = self._read()
            values.discard(self._key(platform, account_id))
            self._write(values)
        self.receipts.resume(platform, account_id)


class _StaticRouter:
    def __init__(self, adapters: Mapping[str, PublisherAdapter]) -> None:
        self.adapters = adapters

    def select(self, account: AccountConfig, platform: str) -> PublisherAdapter:
        try:
            return self.adapters[platform]
        except KeyError as exc:
            raise KeyError(f"unregistered platform: {platform}") from exc


class Orchestrator:
    def __init__(
        self,
        *,
        queue_store: QueueStore,
        adapters: Mapping[str, PublisherAdapter] | None = None,
        router=None,
        accounts: Mapping[str, AccountConfig] | None = None,
        builder=None,
        optimizer: Callable[[CampaignManifest, str], MetadataPack] = optimize_for_platform,
        pause_store: PauseStore | None = None,
        clock=None,
        daily_cap: int = 20,
        timezone_name: str = "UTC",
        retry_limit: int = 1,
        failure_pause_threshold: int = 2,
        worker_id: str = "codex-media-ads",
    ) -> None:
        self.queue_store = queue_store
        self.adapters = dict(adapters or {})
        self.router = router or _StaticRouter(self.adapters)
        self.accounts = dict(accounts or {})
        self.builder = builder
        self.optimizer = optimizer
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.daily_cap = daily_cap
        self.timezone_name = timezone_name
        self.retry_limit = min(max(0, retry_limit), 1)
        self.failure_pause_threshold = failure_pause_threshold
        self.pause_store = pause_store or PauseStore(
            queue_store, threshold=failure_pause_threshold
        )
        self.worker_id = worker_id

    def _receipt_path(self, key: str) -> Path:
        return self.queue_store.receipts.root / f"{key}.json"

    def _key(self, request: PublishRequest) -> str:
        return idempotency_key(
            request.content_id,
            request.platform,
            request.account.account_id,
            request.revision,
        )

    def _latest_attempt(self, request: PublishRequest) -> dict[str, object] | None:
        try:
            value = json.loads(self._receipt_path(self._key(request)).read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return value if isinstance(value, dict) else None

    def _attempt_count(self, key: str) -> int:
        try:
            lines = (self.queue_store.receipts.root / "receipts.jsonl").read_text().splitlines()
        except FileNotFoundError:
            return 0
        count = 0
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("idempotency_key") == key and record.get("dry_run") is not True:
                count += 1
        return count

    @staticmethod
    def _record_result(record: dict[str, object]) -> PublishResult:
        return PublishResult(
            status=str(record.get("status", PublishStatus.FAILED.value)),
            platform_id=str(record.get("platform_id", "")),
            post_url=str(record.get("post_url", "")),
            evidence=dict(record.get("evidence", {}))
            if isinstance(record.get("evidence"), dict)
            else {},
            error_category=str(record.get("error_category", "")),
            detail=str(record.get("detail", "")),
        )

    @staticmethod
    def _verified_success(result: PublishResult) -> bool:
        if result.status not in SUCCESS_STATUSES:
            return False
        if result.platform_id.strip() or result.post_url.strip():
            return True
        evidence = result.evidence
        if any(evidence.get(key) is True for key in POSITIVE_EVIDENCE_FLAGS):
            return True
        return any(
            isinstance(evidence.get(key), (str, int))
            and not isinstance(evidence.get(key), bool)
            and bool(str(evidence.get(key)).strip())
            for key in POSITIVE_EVIDENCE_IDS
        )

    def _cap_reached(self) -> bool:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("orchestrator clock must be timezone-aware")
        local_day = now.astimezone(ZoneInfo(self.timezone_name)).date()
        return (
            self.queue_store.receipts.count_successes_on_date(
                local_day, self.timezone_name
            )
            >= self.daily_cap
        )

    def _preclaim_result(self, request: PublishRequest) -> PublishResult | None:
        key = self._key(request)
        success = self.queue_store.receipts.latest_success(key)
        if success is not None:
            return PublishResult(
                status=PublishStatus.SKIPPED,
                detail="A verified success receipt already exists.",
                evidence={
                    "reason": "duplicate_success",
                    "receipt_file": str(self._receipt_path(key)),
                },
            )

        if self.pause_store.is_paused(
            request.platform, request.account.account_id
        ):
            return PublishResult(
                status=PublishStatus.BLOCKED,
                error_category="platform_paused",
                detail="Publishing is paused for this platform and account.",
            )
        if not request.dry_run and self._cap_reached():
            return PublishResult(
                status=PublishStatus.SKIPPED,
                detail="The configured daily live-publish cap has been reached.",
                evidence={"reason": "daily_cap"},
            )
        latest = self._latest_attempt(request)
        if latest is not None and latest.get("status") in {
            PublishStatus.FAILED.value,
            PublishStatus.BLOCKED.value,
        }:
            attempt = self._attempt_count(key)
            decision = retry_decision(
                str(latest.get("error_category", "")), attempt, self.retry_limit
            )
            if not decision.retry:
                result = self._record_result(latest)
                return result.model_copy(
                    update={
                        "evidence": {
                            **result.evidence,
                            "receipt_file": str(self._receipt_path(key)),
                        }
                    }
                )
        return None

    def add_to_queue(self, request: PublishRequest):
        preclaim = self._preclaim_result(request)
        if preclaim is not None:
            return preclaim
        return self.queue_store.enqueue(request)

    def _claim_for(self, request: PublishRequest) -> QueueClaim | None:
        key = self._key(request)
        pending_path = self.queue_store.pending / f"{key}.json"
        claim_path = self.queue_store.claims / pending_path.name
        with self.queue_store._locked():
            if not pending_path.is_file() or claim_path.exists():
                return None
            value = json.loads(pending_path.read_text())
            claimed_at = self.queue_store._clock()
            if claimed_at.tzinfo is None:
                raise ValueError("queue clock must return a timezone-aware datetime")
            claimed_at = claimed_at.astimezone(timezone.utc)
            lease_expires_at = claimed_at + timedelta(seconds=300)
            value.update(
                {
                    "worker_id": self.worker_id,
                    "claim_id": secrets.token_urlsafe(32),
                    "claimed_at": claimed_at.isoformat(timespec="microseconds").replace(
                        "+00:00", "Z"
                    ),
                    "lease_expires_at": lease_expires_at.isoformat(
                        timespec="microseconds"
                    ).replace("+00:00", "Z"),
                }
            )
            self.queue_store._write_json(pending_path, value)
            os.replace(pending_path, claim_path)
            return self.queue_store._claim_from_record(claim_path, value)

    @staticmethod
    def _safe_evidence(value: object) -> object:
        value = redact(value)
        if isinstance(value, dict):
            return {
                str(key): Orchestrator._safe_evidence(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [Orchestrator._safe_evidence(item) for item in value]
        if isinstance(value, str):
            return redact_diagnostic(value)
        return value

    def _safe_result(self, result: PublishResult) -> PublishResult:
        evidence = self._safe_evidence(result.evidence)
        assert isinstance(evidence, dict)
        return result.model_copy(
            update={
                "detail": redact_diagnostic(result.detail),
                "evidence": evidence,
            }
        )

    def _with_receipt(
        self, result: PublishResult, key: str
    ) -> PublishResult:
        return result.model_copy(
            update={
                "evidence": {
                    **result.evidence,
                    "receipt_file": str(self._receipt_path(key)),
                }
            }
        )

    def _terminal(self, claim: QueueClaim, result: PublishResult) -> PublishResult:
        result = self._safe_result(result)
        self.queue_store.fail(claim, result)
        return self._with_receipt(result, claim.idempotency_key)

    def _gate_result(
        self,
        *,
        category: str,
        detail: str,
        next_action: str = "",
    ) -> PublishResult:
        evidence = {"next_action": next_action} if next_action else {}
        return PublishResult(
            status=PublishStatus.BLOCKED,
            error_category=category,
            detail=redact_diagnostic(detail),
            evidence=self._safe_evidence(evidence),
        )

    def _set_claim_request(
        self, claim: QueueClaim, request: PublishRequest
    ) -> QueueClaim:
        if claim.request == request:
            return claim
        claim_path = self.queue_store.claims / f"{claim.idempotency_key}.json"
        with self.queue_store._locked():
            value = json.loads(claim_path.read_text())
            self.queue_store._assert_owner(claim, value)
            value["request"] = request.model_dump(mode="json")
            self.queue_store._write_json(claim_path, value)
        return self.queue_store._claim_from_record(claim_path, value)

    def _process_claim(self, claim: QueueClaim, *, live: bool) -> PublishResult:
        request = claim.request.model_copy(
            update={"dry_run": claim.request.dry_run or not live}
        )
        claim = self._set_claim_request(claim, request)
        try:
            adapter = self.router.select(request.account, request.platform)
        except Exception:
            return self._terminal(
                claim,
                self._gate_result(
                    category=ErrorCategory.AUTHENTICATION.value,
                    detail=f"{request.platform.title()} account route is unavailable.",
                    next_action=f"Run codex-media-ads publish probe --platform {request.platform}.",
                ),
            )

        try:
            probe = adapter.probe_auth(request.account)
        except Exception as exc:
            error = normalize_adapter_error(exc)
            return self._terminal(
                claim,
                self._gate_result(
                    category=error.category.value,
                    detail=error.detail,
                    next_action=error.next_action,
                ),
            )
        if not probe.authenticated:
            return self._terminal(
                claim,
                self._gate_result(
                    category=(
                        probe.error_category.value
                        if probe.error_category is not None
                        else ErrorCategory.AUTHENTICATION.value
                    ),
                    detail=probe.detail or f"{request.platform.title()} account probe failed.",
                    next_action=probe.next_action,
                ),
            )
        identity = probe_identity(
            request.account.expected_identity, probe.observed_identity
        )
        if not identity.ok:
            return self._terminal(
                claim,
                self._gate_result(
                    category=(
                        identity.error_category.value
                        if identity.error_category is not None
                        else ErrorCategory.IDENTITY_MISMATCH.value
                    ),
                    detail=identity.detail,
                    next_action=identity.next_action,
                ),
            )
        if not request.media_path.is_file() or not isinstance(request.metadata, dict) or not request.metadata:
            return self._terminal(
                claim,
                self._gate_result(
                    category=ErrorCategory.VALIDATION.value,
                    detail="Media and metadata must be present before publishing.",
                ),
            )
        try:
            validation = adapter.validate(request)
        except Exception as exc:
            error = normalize_adapter_error(exc)
            return self._terminal(
                claim,
                self._gate_result(
                    category=error.category.value,
                    detail=error.detail,
                    next_action=error.next_action,
                ),
            )
        if not validation.ok:
            return self._terminal(
                claim,
                self._gate_result(
                    category=(
                        validation.error_category.value
                        if validation.error_category is not None
                        else ErrorCategory.VALIDATION.value
                    ),
                    detail=validation.detail,
                    next_action=validation.next_action,
                ),
            )

        if request.dry_run:
            result = PublishResult(
                status=PublishStatus.SKIPPED,
                detail="Dry run completed without submitting.",
                evidence={"dry_run": True},
            )
            self.queue_store.complete(claim, result)
            return self._with_receipt(result, claim.idempotency_key)

        try:
            result = adapter.publish(request)
        except Exception as exc:
            error = normalize_adapter_error(exc)
            result = PublishResult(
                status=PublishStatus.FAILED,
                error_category=error.category.value,
                detail=error.detail,
                evidence={"next_action": error.next_action},
            )

        if result.status in SUCCESS_STATUSES and not self._verified_success(result):
            result = PublishResult(
                status=PublishStatus.FAILED,
                error_category=ErrorCategory.AMBIGUOUS_SUBMIT.value,
                detail="The platform returned a success status without verifiable evidence.",
            )
        if self._verified_success(result):
            result = self._safe_result(result)
            self.queue_store.complete(claim, result)
            return self._with_receipt(result, claim.idempotency_key)

        key = claim.idempotency_key
        attempt = self._attempt_count(key) + 1
        decision = retry_decision(result.error_category, attempt, self.retry_limit)
        if result.status == PublishStatus.FAILED and decision.retry:
            result = self._safe_result(result).model_copy(
                update={
                    "evidence": {
                        **result.evidence,
                        "queue_action": "requeued",
                        "retry_delay_seconds": decision.delay_seconds,
                    }
                }
            )
            self.queue_store.receipts.write_attempt(request, result)
            self.queue_store.requeue(claim)
            return self._with_receipt(result, claim.idempotency_key)
        return self._terminal(claim, result)

    def publish(self, request: PublishRequest) -> PublishResult:
        preclaim = self._preclaim_result(request)
        if preclaim is not None:
            return preclaim
        enqueue = self.queue_store.enqueue(request)
        if enqueue.status == "duplicate_success":
            return self._preclaim_result(request) or PublishResult(
                status=PublishStatus.SKIPPED
            )
        if enqueue.status == "duplicate_claim":
            return self._gate_result(
                category="claimed",
                detail="This destination is already claimed by another worker.",
            )
        claim = self._claim_for(request)
        if claim is None:
            return PublishResult(
                status=PublishStatus.SKIPPED,
                detail="No queued work is due.",
                evidence={"reason": "idle"},
            )
        return self._process_claim(claim, live=not request.dry_run)

    def process_next(self, *, live: bool = True) -> PublishResult:
        pending = sorted(self.queue_store.pending.glob("*.json"))
        if not pending:
            return PublishResult(
                status=PublishStatus.SKIPPED,
                detail="No queued work is due.",
                evidence={"reason": "idle"},
            )
        value = json.loads(pending[0].read_text())
        request = PublishRequest.model_validate(value["request"])
        request = request.model_copy(update={"dry_run": not live})
        preclaim = self._preclaim_result(request)
        if preclaim is not None:
            return preclaim
        claim = self.queue_store.claim_next(self.worker_id)
        if claim is None:
            return PublishResult(
                status=PublishStatus.SKIPPED,
                evidence={"reason": "idle"},
            )
        return self._process_claim(claim, live=live)

    def probe(self, platform: str) -> ProbeResult:
        account = self.accounts.get(platform)
        if account is None:
            return ProbeResult(
                authenticated=False,
                error_category=ErrorCategory.CONFIGURATION,
                detail=f"{platform.title()} account is not configured.",
                next_action="Configure the platform account before publishing.",
            )
        try:
            adapter = self.router.select(account, platform)
            probe = adapter.probe_auth(account)
        except Exception:
            return ProbeResult(
                authenticated=False,
                error_category=ErrorCategory.AUTHENTICATION,
                detail=f"{platform.title()} account probe failed.",
                next_action="Reconnect the configured account and probe again.",
            )
        if not probe.authenticated:
            return probe
        identity = probe_identity(account.expected_identity, probe.observed_identity)
        if identity.ok:
            return probe
        return ProbeResult(
            authenticated=False,
            observed_identity="",
            error_category=identity.error_category,
            detail=identity.detail,
            next_action=identity.next_action,
        )

    def run_campaign(
        self, campaign: CampaignManifest, *, live: bool = False
    ) -> CampaignRunResult:
        campaign = CampaignManifest.model_validate(campaign)
        self.daily_cap = campaign.daily_cap
        self.timezone_name = campaign.timezone
        self.retry_limit = min(campaign.retry_limit, 1)
        self.failure_pause_threshold = campaign.failure_pause_threshold
        self.pause_store.threshold = campaign.failure_pause_threshold
        if self.builder is None:
            raise ValueError("creative builder is not configured")
        build = self.builder.build(campaign)
        if getattr(build, "dependency", None) is not None:
            raise RuntimeError("creative build is blocked by a dependency")
        if getattr(build, "failure", None) is not None:
            raise RuntimeError("creative build failed")

        requests: list[PublishRequest] = []
        results = CampaignRunResult(
            build_manifest=str(getattr(build, "manifest_path", ""))
        )
        for platform in campaign.destinations:
            try:
                account = self.accounts[platform]
                metadata = self.optimizer(campaign, platform)
                media_path = build.variant_paths[platform]
                request = PublishRequest(
                    content_id=campaign.content_id,
                    revision=1,
                    platform=platform,
                    account=account,
                    media_path=media_path,
                    metadata=metadata.model_dump(mode="json"),
                    idempotency_key="",
                    dry_run=not live,
                )
                preclaim = self._preclaim_result(request)
                if preclaim is not None:
                    results.platforms[platform] = preclaim
                    continue
                enqueue = self.queue_store.enqueue(request)
                if enqueue.status == "duplicate_success":
                    results.platforms[platform] = self._preclaim_result(request) or PublishResult(
                        status=PublishStatus.SKIPPED
                    )
                    continue
                requests.append(request)
            except Exception:
                results.platforms[platform] = PublishResult(
                    status=PublishStatus.FAILED,
                    error_category=ErrorCategory.CONFIGURATION.value,
                    detail=f"{platform.title()} destination could not be prepared.",
                )

        for request in requests:
            try:
                preclaim = self._preclaim_result(request)
                if preclaim is not None:
                    results.platforms[request.platform] = preclaim
                    continue
                claim = self._claim_for(request)
                if claim is None:
                    results.platforms[request.platform] = PublishResult(
                        status=PublishStatus.BLOCKED,
                        error_category="claimed",
                        detail="This destination is already claimed by another worker.",
                    )
                    continue
                results.platforms[request.platform] = self._process_claim(
                    claim, live=live
                )
            except Exception:
                results.platforms[request.platform] = PublishResult(
                    status=PublishStatus.FAILED,
                    error_category=ErrorCategory.INTERNAL.value,
                    detail=f"{request.platform.title()} destination failed independently.",
                )
        results.live_success_count = sum(
            1
            for result in results.platforms.values()
            if live and self._verified_success(result)
        )
        return results

    def queue_status(self) -> dict[str, int | str]:
        counts = {
            "pending": len(list(self.queue_store.pending.glob("*.json"))),
            "claimed": len(list(self.queue_store.claims.glob("*.json"))),
            "completed": len(list(self.queue_store.completed.glob("*.json"))),
            "failed": len(list(self.queue_store.failed.glob("*.json"))),
        }
        return {"status": "idle" if not counts["pending"] else "queued", **counts}

    def receipts(self) -> list[dict[str, object]]:
        path = self.queue_store.receipts.root / "receipts.jsonl"
        try:
            lines = path.read_text().splitlines()
        except FileNotFoundError:
            return []
        output = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                output.append(value)
        return output
