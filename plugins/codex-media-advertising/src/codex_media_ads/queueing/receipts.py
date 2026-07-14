from __future__ import annotations

import fcntl
import json
import os
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from ..config import PRIVATE_MODES, SECRET_FILE_MODE
from ..models import PublishRequest, PublishResult, PublishStatus


SUCCESS_STATUSES = {
    PublishStatus.PUBLISHED.value,
    PublishStatus.SUBMITTED.value,
    PublishStatus.SCHEDULED.value,
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


def _idempotency_key(request: PublishRequest) -> str:
    from .store import idempotency_key

    return idempotency_key(
        request.content_id,
        request.platform,
        request.account.account_id,
        request.revision,
    )


def _positive_evidence(record: dict[str, object]) -> bool:
    def positive_id(value: object) -> bool:
        if isinstance(value, bool):
            return False
        if isinstance(value, str):
            return bool(value.strip())
        return isinstance(value, int)

    post_url = record.get("post_url")
    if positive_id(record.get("platform_id")) or (
        isinstance(post_url, str) and bool(post_url.strip())
    ):
        return True
    evidence = record.get("evidence")
    if not isinstance(evidence, dict):
        return False
    return any(evidence.get(key) is True for key in POSITIVE_EVIDENCE_FLAGS) or any(
        positive_id(evidence.get(key)) for key in POSITIVE_EVIDENCE_IDS
    )


def _is_success(record: dict[str, object]) -> bool:
    return (
        record.get("dry_run") is not True
        and str(record.get("status", "")) in SUCCESS_STATUSES
        and _positive_evidence(record)
    )


def _parse_time(value: str | datetime | None, clock) -> datetime:
    if value is None:
        parsed = clock()
    elif isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("receipt timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


class ReceiptStore:
    def __init__(self, root: Path, *, clock=None) -> None:
        self.root = Path(root)
        self.root.mkdir(mode=PRIVATE_MODES, parents=True, exist_ok=True)
        self.root.chmod(PRIVATE_MODES)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._thread_lock = threading.Lock()
        self._lock_path = self.root / ".receipts.lock"

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

    def _latest_path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def _append_record(self, record: dict[str, object]) -> None:
        encoded = (
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode()
        fd = os.open(
            self.root / "receipts.jsonl",
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            SECRET_FILE_MODE,
        )
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _records(self) -> list[dict[str, object]]:
        try:
            lines = (self.root / "receipts.jsonl").read_text().splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in lines if line.strip()]

    def _read_latest(self, key: str) -> dict[str, object] | None:
        try:
            value = json.loads(self._latest_path(key).read_text())
        except FileNotFoundError:
            return None
        return value if isinstance(value, dict) else None

    def _replace_latest(self, key: str, record: dict[str, object]) -> None:
        path = self._latest_path(key)
        temporary = self.root / (
            f".{key}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        data = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()
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
        finally:
            temporary.unlink(missing_ok=True)

    def write_attempt(
        self,
        request: PublishRequest,
        result: PublishResult | None = None,
        *,
        status: str | PublishStatus | None = None,
        platform_id: str = "",
        post_url: str = "",
        evidence: dict[str, object] | None = None,
        error_category: str = "",
        detail: str = "",
        occurred_at: str | datetime | None = None,
    ) -> dict[str, object]:
        if result is not None:
            if status is not None:
                raise TypeError("pass either result or status, not both")
            status = result.status
            platform_id = result.platform_id
            post_url = result.post_url
            evidence = result.evidence
            error_category = result.error_category
            detail = result.detail
        if status is None:
            raise TypeError("status is required")

        normalized_status = (
            status.value if isinstance(status, PublishStatus) else str(status)
        )
        when = _parse_time(occurred_at, self._clock)
        key = _idempotency_key(request)
        record: dict[str, object] = {
            "idempotency_key": key,
            "content_id": request.content_id,
            "revision": request.revision,
            "platform": request.platform,
            "account_id": request.account.account_id,
            "dry_run": request.dry_run,
            "status": normalized_status,
            "occurred_at": _timestamp(when),
            "platform_id": platform_id,
            "post_url": post_url,
            "evidence": evidence or {},
            "error_category": error_category,
            "detail": detail,
        }
        with self._locked():
            self._append_record(record)

            previous = self._read_latest(key)
            if previous is None or not (_is_success(previous) and not _is_success(record)):
                self._replace_latest(key, record)
        return record

    def _consecutive_failures_unlocked(
        self, platform: str, account_id: str
    ) -> int:
        failures = 0
        for record in self._records():
            if (
                record.get("platform") != platform
                or record.get("account_id") != account_id
            ):
                continue
            if record.get("event") == "resume":
                failures = 0
            elif record.get("dry_run") is True:
                continue
            elif _is_success(record):
                failures = 0
            elif record.get("status") != PublishStatus.SKIPPED.value:
                failures += 1
        return failures

    def consecutive_failures(self, platform: str, account_id: str) -> int:
        with self._locked():
            return self._consecutive_failures_unlocked(platform, account_id)

    def is_paused(
        self, platform: str, account_id: str, *, threshold: int = 2
    ) -> bool:
        if threshold < 1:
            raise ValueError("pause threshold must be positive")
        return self.consecutive_failures(platform, account_id) >= threshold

    def resume(self, platform: str, account_id: str) -> None:
        record: dict[str, object] = {
            "event": "resume",
            "platform": platform,
            "account_id": account_id,
            "occurred_at": _timestamp(_parse_time(None, self._clock)),
        }
        with self._locked():
            self._append_record(record)

    def latest_success(
        self, request_or_key: PublishRequest | str
    ) -> dict[str, object] | None:
        key = (
            _idempotency_key(request_or_key)
            if isinstance(request_or_key, PublishRequest)
            else request_or_key
        )
        with self._locked():
            record = self._read_latest(key)
        return record if record is not None and _is_success(record) else None

    def count_successes_on_date(self, day: str | date, timezone_name: str) -> int:
        target = date.fromisoformat(day) if isinstance(day, str) else day
        zone = ZoneInfo(timezone_name)
        with self._locked():
            records = self._records()

        count = 0
        for record in records:
            occurred_at = _parse_time(str(record["occurred_at"]), self._clock)
            if _is_success(record) and occurred_at.astimezone(zone).date() == target:
                count += 1
        return count
