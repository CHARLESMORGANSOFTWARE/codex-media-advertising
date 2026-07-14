from __future__ import annotations

from pathlib import Path

import pytest

from codex_media_ads.models import AccountConfig, PublishRequest, PublishResult, PublishStatus
from codex_media_ads.publishing.base import (
    AdapterRegistry,
    ErrorCategory,
    ProbeResult,
    ValidationResult,
    normalize_adapter_error,
    probe_identity,
)


PLATFORMS = {"instagram", "tiktok", "youtube", "x", "facebook", "threads"}


class FakeAdapter:
    def __init__(self, platform: str) -> None:
        self.platform = platform
        self.observed_identity = "owner@example.com"
        self.publish_called = False

    def probe_auth(self, account: AccountConfig) -> ProbeResult:
        return ProbeResult(authenticated=True, observed_identity=account.expected_identity)

    def validate(self, request: PublishRequest) -> ValidationResult:
        return probe_identity(request.account.expected_identity, self.observed_identity)

    def publish(self, request: PublishRequest) -> PublishResult:
        self.publish_called = True
        return PublishResult(status=PublishStatus.SUBMITTED, platform_id="post-1")


def make_request(platform: str = "youtube") -> PublishRequest:
    return PublishRequest(
        content_id="content-1",
        revision=1,
        platform=platform,
        account=AccountConfig(account_id="acct-1", expected_identity="owner@example.com"),
        media_path=Path("/tmp/video.mp4"),
        metadata={},
        idempotency_key="key-1",
    )


def complete_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    for platform in sorted(PLATFORMS):
        registry.register(FakeAdapter(platform))
    return registry


def test_registry_requires_all_six_platforms() -> None:
    registry = complete_registry()
    assert set(registry.names()) == PLATFORMS
    registry.require_complete()


def test_registry_rejects_duplicate_and_unknown_platforms() -> None:
    registry = complete_registry()
    with pytest.raises(ValueError, match="already registered"):
        registry.register(FakeAdapter("youtube"))
    with pytest.raises(KeyError, match="unregistered platform"):
        registry.get("mastodon")
    with pytest.raises(KeyError, match="unregistered platform"):
        registry.publish(make_request("mastodon"))


def test_registry_rejects_invalid_adapter_name_and_incomplete_set() -> None:
    registry = AdapterRegistry()
    with pytest.raises(ValueError, match="unsupported platform"):
        registry.register(FakeAdapter("mastodon"))
    registry.register(FakeAdapter("youtube"))
    with pytest.raises(ValueError, match="missing adapters"):
        registry.require_complete()


def test_identity_mismatch_is_hard_block() -> None:
    result = probe_identity("owner@example.com", "wrong-account")
    assert result.ok is False
    assert result.error_category == ErrorCategory.IDENTITY_MISMATCH
    assert result.retryable is False
    assert "wrong-account" not in result.detail


def test_registry_never_calls_publish_after_identity_validation_fails() -> None:
    adapter = FakeAdapter("youtube")
    adapter.observed_identity = "wrong-account"
    registry = AdapterRegistry()
    registry.register(adapter)
    result = registry.publish(make_request())
    assert result.status == PublishStatus.BLOCKED
    assert result.error_category == ErrorCategory.IDENTITY_MISMATCH
    assert adapter.publish_called is False


def test_identity_comparison_is_normalized_but_not_fuzzy() -> None:
    assert probe_identity("Owner@Example.com ", " owner@example.com").ok is True
    assert probe_identity("@owner", "owner").ok is False


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (TimeoutError("token=secret-value timed out"), ErrorCategory.NETWORK),
        (PermissionError("cookie: private-cookie"), ErrorCategory.AUTHENTICATION),
        (ValueError("bad input"), ErrorCategory.VALIDATION),
        (RuntimeError("unexpected"), ErrorCategory.INTERNAL),
    ],
)
def test_adapter_exceptions_are_stable_and_redacted(exception: Exception, expected: ErrorCategory) -> None:
    result = normalize_adapter_error(exception)
    assert result.category == expected
    assert "secret-value" not in result.detail
    assert "private-cookie" not in result.detail
