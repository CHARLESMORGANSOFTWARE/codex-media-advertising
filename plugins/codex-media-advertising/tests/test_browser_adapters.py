from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_media_ads.models import AccountConfig, PublishRequest, PublishStatus
from codex_media_ads.publishing.base import AdapterRegistry, ErrorCategory, SUPPORTED_PLATFORMS
from codex_media_ads.publishing.browser_adapters import (
    BrowserPublisher,
    load_browser_selectors,
    register_browser_adapters,
)


class FakePage:
    def __init__(self, identity: str = "creator@example.test") -> None:
        self.identity = identity
        self.body = "Creator Studio"
        self.url = "https://example.test/studio"
        self.values: dict[str, str] = {}
        self.attributes: dict[tuple[str, str], str] = {}
        self.visible: set[str] = set()
        self.actions: list[tuple[object, ...]] = []
        self.submit_clicked = False
        self.confirmation = ""
        self.permalink = ""
        self.platform_id = ""
        self.raise_on: str | None = None
        self.closed = False

    def goto(self, url: str) -> None:
        self.actions.append(("goto", url))

    def body_text(self) -> str:
        return self.body

    def current_url(self) -> str:
        return self.url

    def text(self, locator: dict[str, str]) -> str:
        purpose = locator["purpose"]
        if purpose == "identity":
            return self.identity
        if purpose == "confirmation":
            return self.confirmation
        if purpose == "platform_id":
            return self.platform_id
        return self.values.get(purpose, "")

    def attribute(self, locator: dict[str, str], name: str) -> str:
        if locator["purpose"] == "permalink" and name == "href":
            return self.permalink
        return self.attributes.get((locator["purpose"], name), "")

    def is_visible(self, locator: dict[str, str]) -> bool:
        purpose = locator["purpose"]
        if purpose == "confirmation":
            return bool(self.confirmation)
        if purpose == "permalink":
            return bool(self.permalink)
        if purpose == "platform_id":
            return bool(self.platform_id)
        return purpose in self.visible

    def click(self, locator: dict[str, str]) -> None:
        purpose = locator["purpose"]
        self.actions.append(("click", purpose))
        if self.raise_on == purpose:
            raise RuntimeError("Authorization: Bearer very-secret-token")
        if purpose == "submit":
            self.submit_clicked = True

    def fill(self, locator: dict[str, str], value: str) -> None:
        self.actions.append(("fill", locator["purpose"], value))

    def set_input_files(self, locator: dict[str, str], path: Path) -> None:
        self.actions.append(("upload", locator["purpose"], str(path)))
        if self.raise_on == locator["purpose"]:
            raise RuntimeError("Authorization: Bearer very-secret-token")

    def select_option(self, locator: dict[str, str], value: str) -> None:
        self.actions.append(("select", locator["purpose"], value))

    def check(self, locator: dict[str, str], checked: bool) -> None:
        self.actions.append(("check", locator["purpose"], checked))

    def wait_for(self, locator: dict[str, str]) -> None:
        self.actions.append(("wait", locator["purpose"]))

    def close(self) -> None:
        self.closed = True


@pytest.fixture(params=SUPPORTED_PLATFORMS)
def platform(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def account(platform: str, identity: str = "creator@example.test") -> AccountConfig:
    return AccountConfig(account_id=f"{platform}-primary", expected_identity=identity)


def publish_request(
    tmp_path: Path,
    platform: str,
    *,
    dry_run: bool = False,
    expected_identity: str = "creator@example.test",
) -> PublishRequest:
    media = tmp_path / f"creative-{platform}.mp4"
    media.write_bytes(b"media")
    return PublishRequest(
        content_id="content-123",
        revision=1,
        platform=platform,
        account=account(platform, expected_identity),
        media_path=media,
        metadata={
            "title": "A useful title",
            "caption": "A useful caption #launch",
            "description": "A useful description",
            "visibility": "unlisted",
            "audience": "not_made_for_kids",
            "synthetic_media": True,
            "allow_comments": False,
            "allow_duet": False,
            "allow_stitch": False,
            "scheduled_at": "2026-07-20T17:00:00-07:00",
        },
        idempotency_key=f"content-123:{platform}:1",
        dry_run=dry_run,
    )


def test_selector_data_is_versioned_and_exactly_six_platforms() -> None:
    data = load_browser_selectors()
    assert data["schema_version"] == "1"
    assert set(data["platforms"]) == set(SUPPORTED_PLATFORMS)


def test_selector_loader_rejects_missing_platform(tmp_path: Path) -> None:
    data = load_browser_selectors()
    del data["platforms"]["threads"]
    path = tmp_path / "selectors.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="exactly"):
        load_browser_selectors(path)


def test_selector_loader_rejects_css_for_semantic_action(tmp_path: Path) -> None:
    data = load_browser_selectors()
    data["platforms"]["x"]["locators"]["submit"] = {
        "kind": "css", "value": ".submit", "purpose": "submit"
    }
    path = tmp_path / "selectors.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="CSS"):
        load_browser_selectors(path)


def test_selector_loader_rejects_unknown_platform_field(tmp_path: Path) -> None:
    data = load_browser_selectors()
    data["platforms"]["x"]["legacy_selector"] = "unsafe"
    path = tmp_path / "selectors.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="unknown fields"):
        load_browser_selectors(path)


def test_registration_is_complete() -> None:
    registry = AdapterRegistry()
    register_browser_adapters(registry, lambda _platform: FakePage())
    registry.require_complete()
    assert set(registry.names()) == set(SUPPORTED_PLATFORMS)


def test_managed_chrome_connector_uses_loopback_cdp_and_closes_owned_runtime() -> None:
    class ChromeStub:
        cdp_url = "http://127.0.0.1:49222"
        closed = False

        def close(self) -> None:
            self.closed = True

    chrome = ChromeStub()
    page = FakePage()
    seen: list[str] = []
    adapter = BrowserPublisher.from_managed_chrome(
        "x",
        chrome,  # type: ignore[arg-type]
        lambda cdp_url: seen.append(cdp_url) or page,
    )
    adapter.close()
    assert seen == ["http://127.0.0.1:49222"]
    assert page.closed is True
    assert chrome.closed is True


def test_probe_detects_logged_out_surface(platform: str) -> None:
    page = FakePage()
    page.body = "Log in Password"
    result = BrowserPublisher(platform, page).probe_auth(account(platform))
    assert result.authenticated is False
    assert result.observed_identity == ""
    assert result.error_category == ErrorCategory.AUTHENTICATION


def test_probe_returns_observed_identity(platform: str) -> None:
    result = BrowserPublisher(platform, FakePage("channel-name")).probe_auth(
        account(platform, "channel-name")
    )
    assert result.authenticated is True
    assert result.observed_identity == "channel-name"


def test_identity_mismatch_blocks_before_upload(tmp_path: Path, platform: str) -> None:
    page = FakePage("other-account")
    result = BrowserPublisher(platform, page).publish(
        publish_request(tmp_path, platform)
    )
    assert result.status == PublishStatus.BLOCKED
    assert result.error_category == ErrorCategory.IDENTITY_MISMATCH
    assert not any(action[0] == "upload" for action in page.actions)


def test_missing_media_blocks_before_upload(tmp_path: Path, platform: str) -> None:
    request = publish_request(tmp_path, platform)
    request.media_path.unlink()
    page = FakePage()
    result = BrowserPublisher(platform, page).publish(request)
    assert result.status == PublishStatus.BLOCKED
    assert result.error_category == ErrorCategory.VALIDATION
    assert not any(action[0] == "upload" for action in page.actions)


def test_dry_run_never_submits_and_returns_evidence(tmp_path: Path, platform: str) -> None:
    page = FakePage()
    result = BrowserPublisher(platform, page).publish(
        publish_request(tmp_path, platform, dry_run=True)
    )
    assert result.status == PublishStatus.SKIPPED
    assert result.evidence["dry_run"] is True
    assert not page.submit_clicked
    assert not any(action[0] == "upload" for action in page.actions)


def test_publish_requires_positive_submit_evidence(tmp_path: Path, platform: str) -> None:
    page = FakePage()
    result = BrowserPublisher(platform, page).publish(publish_request(tmp_path, platform))
    assert page.submit_clicked is True
    assert result.status == PublishStatus.UNKNOWN
    assert result.error_category == ErrorCategory.AMBIGUOUS_SUBMIT
    assert result.evidence["submit_clicked"] is True
    assert result.evidence["retry_safe"] is False


def test_explicit_confirmation_is_success(tmp_path: Path, platform: str) -> None:
    page = FakePage()
    page.confirmation = "Published successfully"
    result = BrowserPublisher(platform, page).publish(publish_request(tmp_path, platform))
    assert result.status == PublishStatus.PUBLISHED
    assert result.evidence["confirmation"] == "Published successfully"


def test_permalink_is_authoritative_success(tmp_path: Path, platform: str) -> None:
    page = FakePage()
    page.permalink = {
        "instagram": "https://www.instagram.com/reel/abc123/",
        "tiktok": "https://www.tiktok.com/@creator/video/123456",
        "youtube": "https://www.youtube.com/watch?v=abc123",
        "x": "https://x.com/creator/status/123456",
        "facebook": "https://www.facebook.com/reel/abc123/",
        "threads": "https://www.threads.net/@creator/post/abc123/",
    }[platform]
    result = BrowserPublisher(platform, page).publish(publish_request(tmp_path, platform))
    expected = (
        PublishStatus.SUBMITTED
        if platform in {"tiktok", "youtube", "facebook"}
        else PublishStatus.PUBLISHED
    )
    assert result.status == expected
    assert result.post_url == page.permalink


def test_platform_id_is_authoritative_success(tmp_path: Path, platform: str) -> None:
    page = FakePage()
    page.platform_id = "abc123"
    result = BrowserPublisher(platform, page).publish(publish_request(tmp_path, platform))
    expected = (
        PublishStatus.SUBMITTED
        if platform in {"tiktok", "youtube", "facebook"}
        else PublishStatus.PUBLISHED
    )
    assert result.status == expected
    assert result.platform_id == "abc123"


def test_matching_current_url_is_authoritative_success(tmp_path: Path) -> None:
    page = FakePage()
    page.url = "https://x.com/creator/status/123456"
    result = BrowserPublisher("x", page).publish(publish_request(tmp_path, "x"))
    assert result.status == PublishStatus.PUBLISHED
    assert result.post_url == page.url


@pytest.mark.parametrize(
    ("platform", "expected_actions"),
    [
        ("instagram", {("fill", "caption"), ("click", "advance")}),
        ("tiktok", {("fill", "caption"), ("check", "allow_comments")}),
        ("youtube", {("fill", "title"), ("select", "visibility"), ("check", "synthetic_media")}),
        ("x", {("fill", "caption"), ("wait", "processing")}),
        ("facebook", {("fill", "caption"), ("fill", "scheduled_at")}),
        ("threads", {("fill", "caption")}),
    ],
)
def test_platform_metadata_contracts(
    tmp_path: Path, platform: str, expected_actions: set[tuple[str, str]]
) -> None:
    page = FakePage()
    page.confirmation = "done"
    BrowserPublisher(platform, page).publish(publish_request(tmp_path, platform))
    simplified = {(str(action[0]), str(action[1])) for action in page.actions}
    assert expected_actions <= simplified


def test_scheduled_metadata_changes_confirmed_status(tmp_path: Path) -> None:
    page = FakePage()
    page.confirmation = "scheduled"
    request = publish_request(tmp_path, "youtube")
    result = BrowserPublisher("youtube", page).publish(request)
    assert result.status == PublishStatus.SCHEDULED


def test_unscheduled_confirmation_is_published(tmp_path: Path) -> None:
    page = FakePage()
    page.confirmation = "published"
    request = publish_request(tmp_path, "threads")
    request.metadata.pop("scheduled_at")
    result = BrowserPublisher("threads", page).publish(request)
    assert result.status == PublishStatus.PUBLISHED


def test_browser_exception_is_redacted(tmp_path: Path) -> None:
    page = FakePage()
    page.raise_on = "submit"
    result = BrowserPublisher("x", page).publish(publish_request(tmp_path, "x"))
    assert result.status == PublishStatus.UNKNOWN
    assert result.error_category == ErrorCategory.AMBIGUOUS_SUBMIT
    assert "very-secret-token" not in result.detail


def test_pre_submit_browser_exception_is_redacted(tmp_path: Path) -> None:
    page = FakePage()
    page.raise_on = "upload"
    result = BrowserPublisher("x", page).publish(publish_request(tmp_path, "x"))
    assert result.status == PublishStatus.FAILED
    assert "very-secret-token" not in result.detail
    assert "[REDACTED]" in result.detail


def test_processing_confirmation_is_submitted(tmp_path: Path) -> None:
    page = FakePage()
    page.confirmation = "Your video is processing"
    request = publish_request(tmp_path, "tiktok")
    request.metadata.pop("scheduled_at")
    result = BrowserPublisher("tiktok", page).publish(request)
    assert result.status == PublishStatus.SUBMITTED
