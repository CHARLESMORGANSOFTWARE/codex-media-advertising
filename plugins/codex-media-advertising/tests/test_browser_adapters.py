from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_media_ads.models import AccountConfig, PublishRequest, PublishStatus
from codex_media_ads.publishing.base import AdapterRegistry, ErrorCategory, SUPPORTED_PLATFORMS
from codex_media_ads.publishing import browser_adapters
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
        self.confirmation_after_submit = ""
        self.permalink_after_submit = ""
        self.platform_id_after_submit = ""
        self.url_after_submit = ""
        self.submitted_action = ""
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
        if purpose in {"submit", "schedule_submit"}:
            self.submit_clicked = True
            self.submitted_action = purpose
            self.confirmation = self.confirmation_after_submit
            self.permalink = self.permalink_after_submit
            self.platform_id = self.platform_id_after_submit
            if self.url_after_submit:
                self.url = self.url_after_submit

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
    scheduled: bool = False,
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
            "filename_slug": "useful-video-file",
            "caption": "A useful caption #launch",
            "description": "A useful description",
            "visibility": "unlisted",
            "audience": "not_made_for_kids",
            "synthetic_media": True,
            "allow_comments": False,
            "allow_duet": False,
            "allow_stitch": False,
            **(
                {"scheduled_at": "2026-07-20T17:00:00-07:00"}
                if scheduled
                else {}
            ),
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


def test_selector_loader_requires_every_platform_operation_locator(tmp_path: Path) -> None:
    data = load_browser_selectors()
    del data["platforms"]["tiktok"]["locators"]["filename_slug"]
    path = tmp_path / "selectors.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="filename_slug"):
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


def test_managed_chrome_uses_real_playwright_connector_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    class ChromeStub:
        cdp_url = "http://127.0.0.1:49222"

        def close(self) -> None:
            pass

    page = FakePage()
    seen: list[str] = []
    playwright_page = getattr(browser_adapters, "PlaywrightBrowserPage")
    monkeypatch.setattr(
        playwright_page,
        "connect",
        lambda cdp_url: seen.append(cdp_url) or page,
    )
    adapter = BrowserPublisher.from_managed_chrome("x", ChromeStub())  # type: ignore[arg-type]
    assert adapter.page is page
    assert seen == ["http://127.0.0.1:49222"]


def test_register_managed_chrome_adapters_wires_all_six_to_cdp() -> None:
    class ChromeStub:
        cdp_url = "http://127.0.0.1:49222"

        def close(self) -> None:
            pass

    registry = AdapterRegistry()
    seen: list[str] = []
    browser_adapters.register_managed_chrome_adapters(
        registry,
        ChromeStub(),  # type: ignore[arg-type]
        connector=lambda cdp_url: seen.append(cdp_url) or FakePage(),
    )
    registry.require_complete()
    assert seen == ["http://127.0.0.1:49222"] * 6


def test_playwright_page_connects_over_cdp_and_maps_semantic_locators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    class LocatorStub:
        @property
        def first(self) -> "LocatorStub":
            return self

        def click(self) -> None:
            calls.append(("click",))

        def fill(self, value: str) -> None:
            calls.append(("fill", value))

        def inner_text(self) -> str:
            return "creator@example.test"

        def get_attribute(self, name: str) -> str:
            return f"attribute:{name}"

        def is_visible(self) -> bool:
            return True

        def set_input_files(self, path: str) -> None:
            calls.append(("files", path))

        def select_option(self, value: str) -> None:
            calls.append(("select", value))

        def check(self) -> None:
            calls.append(("check",))

        def uncheck(self) -> None:
            calls.append(("uncheck",))

        def wait_for(self, *, state: str) -> None:
            calls.append(("wait", state))

    locator = LocatorStub()

    class RawPage:
        url = "https://example.test/current"

        def goto(self, url: str) -> None:
            calls.append(("goto", url))

        def locator(self, value: str) -> LocatorStub:
            calls.append(("css", value))
            return locator

        def get_by_role(self, role: str, *, name: str) -> LocatorStub:
            calls.append(("role", role, name))
            return locator

        def get_by_text(self, value: str, *, exact: bool = False) -> LocatorStub:
            calls.append(("text", value, exact))
            return locator

        def get_by_label(self, value: str, *, exact: bool = False) -> LocatorStub:
            calls.append(("label", value, exact))
            return locator

        def close(self) -> None:
            calls.append(("page-close",))

    raw_page = RawPage()
    context = SimpleNamespace(new_page=lambda: raw_page)
    browser = SimpleNamespace(contexts=[context])

    class Chromium:
        def connect_over_cdp(self, cdp_url: str) -> object:
            calls.append(("cdp", cdp_url))
            return browser

    class Manager:
        chromium = Chromium()

        def stop(self) -> None:
            calls.append(("playwright-stop",))

    starter = SimpleNamespace(start=lambda: Manager())
    module = SimpleNamespace(sync_playwright=lambda: starter)
    monkeypatch.setattr(browser_adapters, "import_module", lambda _name: module, raising=False)

    page = browser_adapters.PlaywrightBrowserPage.connect("http://127.0.0.1:49222")
    page.goto("https://example.test")
    page.click({"kind": "role", "value": "button", "name": "Post", "purpose": "submit"})
    page.fill({"kind": "label", "value": "Caption", "purpose": "caption"}, "copy")
    page.close()
    assert ("cdp", "http://127.0.0.1:49222") in calls
    assert ("role", "button", "Post") in calls
    assert ("label", "Caption", False) in calls
    assert calls[-2:] == [("page-close",), ("playwright-stop",)]


def test_playwright_dependency_error_is_lazy_and_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_name: str) -> object:
        raise ModuleNotFoundError("No module named 'playwright'", name="playwright")

    monkeypatch.setattr(browser_adapters, "import_module", missing, raising=False)
    with pytest.raises(RuntimeError, match=r"browser.*playwright|playwright.*browser"):
        browser_adapters.PlaywrightBrowserPage.connect("http://127.0.0.1:49222")


def test_probe_detects_logged_out_surface(platform: str) -> None:
    page = FakePage()
    page.body = "Log in Password"
    result = BrowserPublisher(platform, page).probe_auth(account(platform))
    assert result.authenticated is False
    assert result.observed_identity == ""
    assert result.error_category == ErrorCategory.AUTHENTICATION


def test_probe_returns_observed_identity(platform: str) -> None:
    identity_evidence = {
        "instagram": ("", "/channel-name/", "channel-name"),
        "tiktok": ("@channel-name", "", "channel-name"),
        "youtube": ("Channel\nchannel@example.test", "", "channel@example.test"),
        "x": ("Channel Name\n@channel_name", "", "channel_name"),
        "facebook": ("", "https://www.facebook.com/channel.name", "channel.name"),
        "threads": ("", "/@channel-name", "channel-name"),
    }
    text_value, href, expected = identity_evidence[platform]
    page = FakePage(text_value)
    page.attributes[("identity", "href")] = href
    result = BrowserPublisher(platform, page).probe_auth(account(platform, expected))
    assert result.authenticated is True
    assert result.observed_identity == expected


@pytest.mark.parametrize("generic_label", ["Profile", "Account", "Account menu", "Your profile"])
def test_probe_rejects_generic_account_control_labels(
    platform: str, generic_label: str
) -> None:
    result = BrowserPublisher(platform, FakePage(generic_label)).probe_auth(
        account(platform, generic_label)
    )
    assert result.authenticated is False
    assert result.observed_identity == ""
    assert result.error_category == ErrorCategory.AUTHENTICATION


def test_generic_identity_hard_blocks_before_upload(tmp_path: Path) -> None:
    page = FakePage("Account")
    result = BrowserPublisher("youtube", page).publish(
        publish_request(tmp_path, "youtube", expected_identity="Account")
    )
    assert result.status == PublishStatus.BLOCKED
    assert not any(action[0] == "upload" for action in page.actions)


def test_identity_mismatch_blocks_before_upload(tmp_path: Path, platform: str) -> None:
    page = FakePage("other@example.test")
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
    page.confirmation_after_submit = {
        "instagram": "Your post has been shared",
        "tiktok": "Your video is being uploaded",
        "youtube": "Video published",
        "x": "Your post was sent",
        "facebook": "Your reel is being published",
        "threads": "Your thread was posted",
    }[platform]
    result = BrowserPublisher(platform, page).publish(publish_request(tmp_path, platform))
    expected = (
        PublishStatus.SUBMITTED
        if platform in {"tiktok", "facebook"}
        else PublishStatus.PUBLISHED
    )
    assert result.status == expected
    assert result.evidence["confirmation"] == page.confirmation_after_submit


def test_generic_confirmation_never_counts_as_positive_evidence(
    tmp_path: Path, platform: str
) -> None:
    page = FakePage()
    page.confirmation_after_submit = "done"
    result = BrowserPublisher(platform, page).publish(publish_request(tmp_path, platform))
    assert result.status == PublishStatus.UNKNOWN
    assert result.error_category == ErrorCategory.AMBIGUOUS_SUBMIT


def test_stale_platform_confirmation_never_counts_as_positive_evidence(
    tmp_path: Path,
) -> None:
    page = FakePage()
    page.confirmation = "Your post was sent"
    page.confirmation_after_submit = "Your post was sent"
    result = BrowserPublisher("x", page).publish(publish_request(tmp_path, "x"))
    assert result.status == PublishStatus.UNKNOWN
    assert result.error_category == ErrorCategory.AMBIGUOUS_SUBMIT


def test_permalink_is_authoritative_success(tmp_path: Path, platform: str) -> None:
    page = FakePage()
    page.permalink_after_submit = {
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
    assert result.post_url == page.permalink_after_submit


def test_platform_id_is_authoritative_success(tmp_path: Path, platform: str) -> None:
    page = FakePage()
    page.platform_id_after_submit = {
        "instagram": "C9abc_123",
        "tiktok": "7391234567890123456",
        "youtube": "aB3_defGh-I",
        "x": "1812345678901234567",
        "facebook": "123456789012345",
        "threads": "C9abc_123",
    }[platform]
    result = BrowserPublisher(platform, page).publish(publish_request(tmp_path, platform))
    expected = (
        PublishStatus.SUBMITTED
        if platform in {"tiktok", "youtube", "facebook"}
        else PublishStatus.PUBLISHED
    )
    assert result.status == expected
    assert result.platform_id == page.platform_id_after_submit


def test_generic_platform_id_never_counts_as_positive_evidence(
    tmp_path: Path, platform: str
) -> None:
    page = FakePage()
    page.platform_id_after_submit = "done"
    result = BrowserPublisher(platform, page).publish(publish_request(tmp_path, platform))
    assert result.status == PublishStatus.UNKNOWN


def test_matching_current_url_is_authoritative_success(tmp_path: Path) -> None:
    page = FakePage()
    page.url_after_submit = "https://x.com/creator/status/123456"
    result = BrowserPublisher("x", page).publish(publish_request(tmp_path, "x"))
    assert result.status == PublishStatus.PUBLISHED
    assert result.post_url == page.url_after_submit


@pytest.mark.parametrize(
    ("platform", "expected_actions"),
    [
        ("instagram", {("fill", "caption"), ("click", "advance")}),
        ("tiktok", {("fill", "caption"), ("check", "allow_comments")}),
        ("youtube", {("fill", "title"), ("select", "visibility"), ("check", "synthetic_media")}),
        ("x", {("fill", "caption"), ("wait", "processing")}),
        ("facebook", {("fill", "caption")}),
        ("threads", {("fill", "caption")}),
    ],
)
def test_platform_metadata_contracts(
    tmp_path: Path, platform: str, expected_actions: set[tuple[str, str]]
) -> None:
    page = FakePage()
    BrowserPublisher(platform, page).publish(publish_request(tmp_path, platform))
    simplified = {(str(action[0]), str(action[1])) for action in page.actions}
    assert expected_actions <= simplified


@pytest.mark.parametrize("platform", ["tiktok", "youtube", "facebook"])
def test_scheduling_selects_mode_date_time_and_conditional_action(
    tmp_path: Path, platform: str
) -> None:
    page = FakePage()
    page.visible.update(
        {"schedule_mode", "schedule_date", "schedule_time", "schedule_submit"}
    )
    page.confirmation_after_submit = {
        "tiktok": "Video scheduled for Jul 20, 2026",
        "youtube": "Video scheduled for Jul 20, 2026",
        "facebook": "Reel scheduled for Jul 20, 2026",
    }[platform]
    request = publish_request(tmp_path, platform, scheduled=True)
    result = BrowserPublisher(platform, page).publish(request)
    assert result.status == PublishStatus.SCHEDULED
    assert ("click", "schedule_mode") in page.actions
    assert ("fill", "schedule_date", "2026-07-20") in page.actions
    assert ("fill", "schedule_time", "17:00") in page.actions
    assert page.submitted_action == "schedule_submit"
    assert ("click", "submit") not in page.actions


@pytest.mark.parametrize("platform", ["tiktok", "youtube", "facebook"])
def test_unavailable_schedule_controls_never_fall_back_to_immediate_publish(
    tmp_path: Path, platform: str
) -> None:
    page = FakePage()
    result = BrowserPublisher(platform, page).publish(
        publish_request(tmp_path, platform, scheduled=True)
    )
    assert result.status in {PublishStatus.FAILED, PublishStatus.UNKNOWN}
    assert page.submit_clicked is False
    assert ("click", "submit") not in page.actions


@pytest.mark.parametrize("platform", ["tiktok", "youtube", "facebook"])
def test_schedule_action_requires_schedule_specific_positive_evidence(
    tmp_path: Path, platform: str
) -> None:
    page = FakePage()
    page.visible.update(
        {"schedule_mode", "schedule_date", "schedule_time", "schedule_submit"}
    )
    page.confirmation_after_submit = {
        "tiktok": "Your video is being uploaded",
        "youtube": "Video published",
        "facebook": "Your reel is being published",
    }[platform]
    result = BrowserPublisher(platform, page).publish(
        publish_request(tmp_path, platform, scheduled=True)
    )
    assert page.submitted_action == "schedule_submit"
    assert result.status == PublishStatus.UNKNOWN
    assert result.error_category == ErrorCategory.AMBIGUOUS_SUBMIT


def test_unscheduled_confirmation_is_published(tmp_path: Path) -> None:
    page = FakePage()
    page.confirmation_after_submit = "Your thread was posted"
    request = publish_request(tmp_path, "threads")
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
    page.confirmation_after_submit = "Your video is being uploaded"
    request = publish_request(tmp_path, "tiktok")
    result = BrowserPublisher("tiktok", page).publish(request)
    assert result.status == PublishStatus.SUBMITTED


def test_tiktok_filename_slug_and_caption_are_distinct_operations(tmp_path: Path) -> None:
    data = load_browser_selectors()
    locators = data["platforms"]["tiktok"]["locators"]
    assert locators["filename_slug"]["value"] != locators["caption"]["value"]

    page = FakePage()
    BrowserPublisher("tiktok", page).publish(publish_request(tmp_path, "tiktok"))
    assert ("fill", "filename_slug", "useful-video-file") in page.actions
    assert ("fill", "caption", "A useful caption #launch") in page.actions
