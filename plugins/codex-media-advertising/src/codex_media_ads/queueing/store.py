from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from ..config import PRIVATE_MODES, SECRET_FILE_MODE
from ..models import PublishRequest, PublishResult
from .receipts import ReceiptStore


def idempotency_key(
    content_id: str, platform: str, account_id: str, revision: int
) -> str:
    raw = f"{content_id}\0{platform}\0{account_id}\0{revision}".encode()
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    delay_seconds: int


def retry_decision(category: str, attempt: int, max_retries: int) -> RetryDecision:
    transient = {"network", "rate_limit", "platform_ui"}
    effective_max_retries = min(max(0, max_retries), 1)
    retry = (
        category in transient
        and category != "ambiguous_submit"
        and attempt <= effective_max_retries
    )
    return RetryDecision(
        retry=retry,
        delay_seconds=min(60, 5 * (2 ** max(0, attempt - 1))),
    )


@dataclass(frozen=True)
class EnqueueResult:
    status: str
    idempotency_key: str
    path: Path | None = None


@dataclass(frozen=True)
class QueueClaim:
    idempotency_key: str
    request: PublishRequest
    worker_id: str
    claim_id: str
    claimed_at: datetime
    lease_expires_at: datetime
    path: Path


class ClaimOwnershipError(PermissionError):
    """Raised when a stale or different worker tries to mutate a claim."""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("queue clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _utc(parsed)


class QueueStore:
    def __init__(self, root: Path, *, clock=None) -> None:
        self.root = Path(root)
        self.queue_root = self.root / "queue"
        self.pending = self.queue_root / "pending"
        self.claims = self.queue_root / "claims"
        self.completed = self.queue_root / "completed"
        self.failed = self.queue_root / "failed"
        for path in (
            self.root,
            self.queue_root,
            self.pending,
            self.claims,
            self.completed,
            self.failed,
        ):
            path.mkdir(mode=PRIVATE_MODES, parents=True, exist_ok=True)
            path.chmod(PRIVATE_MODES)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.receipts = ReceiptStore(self.root / "receipts", clock=self._clock)
        self._thread_lock = threading.Lock()
        self._lock_path = self.queue_root / ".queue.lock"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            fd = os.open(
                self._lock_path,
                os.O_CREAT | os.O_RDWR,
                SECRET_FILE_MODE,
            )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _key(self, request: PublishRequest) -> str:
        return idempotency_key(
            request.content_id,
            request.platform,
            request.account.account_id,
            request.revision,
        )

    def _write_json(self, path: Path, value: dict[str, object]) -> None:
        temporary = path.parent / (
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        fd = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            SECRET_FILE_MODE,
        )
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def enqueue(self, request: PublishRequest) -> EnqueueResult:
        key = self._key(request)
        pending_path = self.pending / f"{key}.json"
        claim_path = self.claims / f"{key}.json"
        with self._locked():
            if self.receipts.latest_success(key) is not None:
                return EnqueueResult("duplicate_success", key)
            if claim_path.exists():
                return EnqueueResult("duplicate_claim", key, claim_path)
            if pending_path.exists():
                return EnqueueResult("duplicate_pending", key, pending_path)

            normalized = request.model_copy(update={"idempotency_key": key})
            value: dict[str, object] = {
                "idempotency_key": key,
                "enqueued_at": _timestamp(_utc(self._clock())),
                "request": normalized.model_dump(mode="json"),
            }
            data = json.dumps(
                value, ensure_ascii=False, separators=(",", ":")
            ).encode()
            fd = os.open(
                pending_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                SECRET_FILE_MODE,
            )
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            return EnqueueResult("enqueued", key, pending_path)

    def _claim_from_record(self, path: Path, value: dict[str, object]) -> QueueClaim:
        request = PublishRequest.model_validate(value["request"])
        return QueueClaim(
            idempotency_key=str(value["idempotency_key"]),
            request=request,
            worker_id=str(value["worker_id"]),
            claim_id=str(value["claim_id"]),
            claimed_at=_parse_time(str(value["claimed_at"])),
            lease_expires_at=_parse_time(str(value["lease_expires_at"])),
            path=path,
        )

    def claim_next(
        self, worker_id: str, lease_seconds: int = 300
    ) -> QueueClaim | None:
        if not worker_id:
            raise ValueError("worker_id is required")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")

        with self._locked():
            for pending_path in sorted(self.pending.glob("*.json")):
                claim_path = self.claims / pending_path.name
                if claim_path.exists():
                    continue
                value = json.loads(pending_path.read_text())
                claimed_at = _utc(self._clock())
                value.update(
                    {
                        "worker_id": worker_id,
                        "claim_id": secrets.token_urlsafe(32),
                        "claimed_at": _timestamp(claimed_at),
                        "lease_expires_at": _timestamp(
                            claimed_at + timedelta(seconds=lease_seconds)
                        ),
                    }
                )
                self._write_json(pending_path, value)
                try:
                    os.replace(pending_path, claim_path)
                except FileNotFoundError:
                    continue
                return self._claim_from_record(claim_path, value)
        return None

    def _claim_path(self, claim: QueueClaim) -> Path:
        return self.claims / f"{claim.idempotency_key}.json"

    def _assert_owner(
        self, claim: QueueClaim, value: dict[str, object]
    ) -> None:
        if (
            value.get("worker_id") != claim.worker_id
            or value.get("claim_id") != claim.claim_id
        ):
            raise ClaimOwnershipError("claim owner does not match current lease")

    def _transition(
        self,
        claim: QueueClaim,
        result: PublishResult,
        destination: Path,
    ) -> dict[str, object]:
        claim_path = self._claim_path(claim)
        with self._locked():
            value = json.loads(claim_path.read_text())
            self._assert_owner(claim, value)
            request = PublishRequest.model_validate(value["request"])
            receipt = self.receipts.write_attempt(request, result)
            value["result"] = result.model_dump(mode="json")
            value["finished_at"] = receipt["occurred_at"]
            self._write_json(claim_path, value)
            os.replace(claim_path, destination / claim_path.name)
            return receipt

    def complete(
        self, claim: QueueClaim, result: PublishResult
    ) -> dict[str, object]:
        return self._transition(claim, result, self.completed)

    def fail(
        self, claim: QueueClaim, result: PublishResult
    ) -> dict[str, object]:
        return self._transition(claim, result, self.failed)

    def requeue(self, claim: QueueClaim) -> Path:
        claim_path = self._claim_path(claim)
        pending_path = self.pending / claim_path.name
        with self._locked():
            value = json.loads(claim_path.read_text())
            self._assert_owner(claim, value)
            os.replace(claim_path, pending_path)
        return pending_path

    def recover_expired(self) -> int:
        now = _utc(self._clock())
        recovered = 0
        with self._locked():
            for claim_path in sorted(self.claims.glob("*.json")):
                value = json.loads(claim_path.read_text())
                if _parse_time(str(value["lease_expires_at"])) > now:
                    continue
                key = str(value["idempotency_key"])
                if self.receipts.latest_success(key) is not None:
                    continue
                pending_path = self.pending / claim_path.name
                if pending_path.exists():
                    continue
                os.replace(claim_path, pending_path)
                recovered += 1
        return recovered
