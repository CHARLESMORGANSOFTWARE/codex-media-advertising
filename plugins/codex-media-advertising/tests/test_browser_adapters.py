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
        self.text_by_locator: dict[str, str] = {}
        self.attributes_by_locator: dict[tuple[str, str], str] = {}
        self.text_after_submit: dict[str, str] = {}
        self.attributes_after_submit: dict[tuple[str, str], str] = {}
        self.url_after_submit = ""
        self.submitted_action = ""
        self.raise_on: str | None = None
        self.closed = False
        self.reveal_after_polls = 0
        self.evidence_polls = 0
        self.schedule_mode_after_upload = False
        self.schedule_mode_after_wizard_clicks = 0
        self.wizard_clicks = 0
        self.visibility_after_wizard_clicks = 3

    def goto(self, url: str) -> None:
        self.actions.append(("goto", url))

    def body_text(self) -> str:
        return self.body

    def current_url(self) -> str:
        if self.submit_clicked:
            self.evidence_polls += 1
        return self.url

    def text(self, locator: dict[str, str]) -> str:
        purpose = locator["purpose"]
        if purpose == "identity":
            return self.identity
        self._reveal_submit_evidence()
        return self.text_by_locator.get(locator["value"], self.values.get(purpose, ""))

    def attribute(self, locator: dict[str, str], name: str) -> str:
        self._reveal_submit_evidence()
        return self.attributes_by_locator.get(
            (locator["value"], name),
            self.attributes.get((locator["purpose"], name), ""),
        )

    def is_visible(self, locator: dict[str, str]) -> bool:
        purpose = locator["purpose"]
        self._reveal_submit_evidence()
        if locator["value"] in self.text_by_locator:
            return bool(self.text_by_locator[locator["value"]])
        if any(key[0] == locator["value"] for key in self.attributes_by_locator):
            return True
        return purpose in self.visible

    def click(self, locator: dict[str, str]) -> None:
        purpose = locator["purpose"]
        self.actions.append(("click", purpose))
        if self.raise_on == purpose:
            raise RuntimeError("Authorization: Bearer very-secret-token")
        if purpose == "wizard_next":
            self.wizard_clicks += 1
            if self.wizard_clicks >= self.visibility_after_wizard_clicks:
                self.visible.add("visibility")
            if self.wizard_clicks >= self.schedule_mode_after_wizard_clicks > 0:
                self.visible.add("schedule_mode")
        if purpose == "schedule_mode" and purpose in self.visible:
            self.visible.update({"schedule_date", "schedule_time", "schedule_submit"})
        if purpose in {"submit", "schedule_submit"}:
            self.submit_clicked = True
            self.submitted_action = purpose
            self.evidence_polls = 0
            self._reveal_submit_evidence()

    def fill(self, locator: dict[str, str], value: str) -> None:
        self.actions.append(("fill", locator["purpose"], value))

    def set_input_files(self, locator: dict[str, str], path: Path) -> None:
        self.actions.append(("upload", locator["purpose"], str(path)))
        if self.raise_on == locator["purpose"]:
            raise RuntimeError("Authorization: Bearer very-secret-token")
        if self.schedule_mode_after_upload:
            self.visible.add("schedule_mode")

    def select_option(self, locator: dict[str, str], value: str) -> None:
        if locator["purpose"] == "visibility" and "visibility" not in self.visible:
            raise RuntimeError("visibility phase is not exposed")
        self.actions.append(("select", locator["purpose"], value))

    def check(self, locator: dict[str, str], checked: bool) -> None:
        self.actions.append(("check", locator["purpose"], checked))

    def wait_for(self, locator: dict[str, str]) -> None:
        self.actions.append(("wait", locator["purpose"]))

    def close(self) -> None:
        self.closed = True

    def _reveal_submit_evidence(self) -> None:
        if not self.submit_clicked or self.evidence_polls < self.reveal_after_polls:
            return
        self.text_by_locator.update(self.text_after_submit)
        self.attributes_by_locator.update(self.attributes_after_submit)
        if self.url_after_submit:
            self.url = self.url_after_submit


@pytest.fixture(params=SUPPORTED_PLATFORMS)
def platform(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def account(platform: str, identity: str = "creator@example.test") -> AccountConfig:
    return AccountConfig(account_id=f"{platform}-primary", expected_identity=identity)


def locator_for(platform: str, purpose: str) -> dict[str, str]:
    data = load_browser_selectors()
    return data["platforms"][platform]["locators"][purpose]


def set_text_evidence(
    page: FakePage, platform: str, purpose: str, value: str, *, after_submit: bool = True
) -> None:
    target = page.text_after_submit if after_submit else page.text_by_locator
    target[locator_for(platform, purpose)["value"]] = value


def set_link_evidence(
    page: FakePage, platform: str, purpose: str, value: str, *, after_submit: bool = True
) -> None:
    target = (
        page.attributes_after_submit if after_submit else page.attributes_by_locator
    )
    target[(locator_for(platform, purpose)["value"], "href")] = value


def expose_schedule_controls(page: FakePage, platform: str) -> None:
    if platform == "youtube":
        page.schedule_mode_after_wizard_clicks = 3
    else:
        page.schedule_mode_after_upload = True


def assert_actions_in_order(
    actions: list[tuple[object, ...]], expected: list[tuple[object, ...]]
) -> None:
    cursor = 0
    for action in expected:
        cursor = actions.index(action, cursor) + 1


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_publisher(platform: str, page: FakePage) -> BrowserPublisher:
    clock = FakeClock()
    return BrowserPublisher(
        platform,
        page,
        evidence_timeout=0.01,
        evidence_poll_interval=0.01,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


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


def test_selector_loader_rejects_unknown_extra_locator(tmp_path: Path) -> None:
    data = load_browser_selectors()
    data["platforms"]["x"]["locators"]["spare_control"] = {
        "kind": "role",
        "value": "button",
        "name": "Spare",
        "purpose": "spare_control",
    }
    path = tmp_path / "selectors.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="spare_control|exact"):
        load_browser_selectors(path)


def test_selector_loader_rejects_platform_schedule_capability_mismatch(
    tmp_path: Path,
) -> None:
    data = load_browser_selectors()
    data["platforms"]["instagram"]["supports_schedule"] = True
    data["platforms"]["instagram"]["schedule_confirmation_pattern"] = "Scheduled"
    path = tmp_path / "selectors.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="supports_schedule"):
        load_browser_selectors(path)


def test_selector_loader_rejects_changed_platform_operation_sequence(
    tmp_path: Path,
) -> None:
    data = load_browser_selectors()
    data["platforms"]["youtube"]["open_upload_operations"] = []
    path = tmp_path / "selectors.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="open_upload_operations|operations"):
        load_browser_selectors(path)


@pytest.mark.parametrize("platform", ["tiktok", "youtube", "facebook"])
def test_scheduling_schema_requires_all_schedule_evidence_locators(
    tmp_path: Path, platform: str
) -> None:
    data = load_browser_selectors()
    assert "schedule_permalink" in data["platforms"][platform]["locators"]
    del data["platforms"][platform]["locators"]["schedule_permalink"]
    path = tmp_path / "selectors.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="schedule_permalink"):
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


def test_single_managed_adapter_construction_failure_closes_page_and_runtime() -> None:
    class ChromeStub:
        cdp_url = "http://127.0.0.1:49222"
        close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    chrome = ChromeStub()
    page = FakePage()
    selectors = load_browser_selectors()
    selectors["platforms"]["x"]["locators"]["unexpected"] = {
        "kind": "label",
        "value": "Unexpected",
        "purpose": "unexpected",
    }
    with pytest.raises(ValueError, match="unexpected"):
        BrowserPublisher.from_managed_chrome(
            "x",
            chrome,  # type: ignore[arg-type]
            connector=lambda _cdp_url: page,
            selectors=selectors,
        )
    assert page.closed is True
    assert chrome.close_calls == 1


def test_connector_error_survives_failing_runtime_cleanup_with_redacted_note() -> None:
    class ChromeStub:
        cdp_url = "http://127.0.0.1:49222"

        def close(self) -> None:
            raise RuntimeError("Authorization: Bearer cleanup-secret")

    def connector(_cdp_url: str) -> FakePage:
        raise RuntimeError("decisive CDP connector error")

    with pytest.raises(RuntimeError, match="decisive CDP connector error") as caught:
        BrowserPublisher.from_managed_chrome(
            "x",
            ChromeStub(),  # type: ignore[arg-type]
            connector=connector,
        )
    notes = "\n".join(getattr(caught.value, "__notes__", []))
    assert "cleanup diagnostic" in notes
    assert "managed_chrome" in notes
    assert "cleanup-secret" not in notes
    assert "[REDACTED]" in notes


def test_construction_error_survives_page_and_runtime_cleanup_failures() -> None:
    class ChromeStub:
        cdp_url = "http://127.0.0.1:49222"

        def close(self) -> None:
            raise RuntimeError("Authorization: Bearer chrome-cleanup-secret")

    class PageStub(FakePage):
        def close(self) -> None:
            raise RuntimeError("Cookie: page-cleanup-secret")

    selectors = load_browser_selectors()
    selectors["platforms"]["x"]["locators"]["unexpected"] = {
        "kind": "label",
        "value": "Unexpected",
        "purpose": "unexpected",
    }
    with pytest.raises(ValueError, match="unexpected") as caught:
        BrowserPublisher.from_managed_chrome(
            "x",
            ChromeStub(),  # type: ignore[arg-type]
            connector=lambda _cdp_url: PageStub(),
            selectors=selectors,
        )
    notes = "\n".join(getattr(caught.value, "__notes__", []))
    assert "cleanup diagnostic" in notes
    assert "page" in notes and "managed_chrome" in notes
    assert "page-cleanup-secret" not in notes
    assert "chrome-cleanup-secret" not in notes
    assert notes.count("[REDACTED]") >= 2


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


def test_managed_registry_keeps_runtime_alive_until_last_adapter_closes() -> None:
    class ChromeStub:
        cdp_url = "http://127.0.0.1:49222"
        close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    chrome = ChromeStub()
    registry = AdapterRegistry()
    pages: dict[str, FakePage] = {}

    def connector(_cdp_url: str) -> FakePage:
        page = FakePage()
        pages[str(len(pages))] = page
        return page

    browser_adapters.register_managed_chrome_adapters(
        registry,
        chrome,  # type: ignore[arg-type]
        connector=connector,
    )
    first = registry.get("instagram")
    first.close()  # type: ignore[attr-defined]
    assert chrome.close_calls == 0
    assert registry.get("x").probe_auth(account("x")).authenticated is True

    for platform in SUPPORTED_PLATFORMS:
        if platform != "instagram":
            registry.get(platform).close()  # type: ignore[attr-defined]
    assert chrome.close_calls == 1
    assert all(page.closed for page in pages.values())


def test_managed_registry_partial_connection_failure_cleans_pages_and_runtime() -> None:
    class ChromeStub:
        cdp_url = "http://127.0.0.1:49222"
        close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    chrome = ChromeStub()
    registry = AdapterRegistry()
    pages: list[FakePage] = []

    def connector(_cdp_url: str) -> FakePage:
        if len(pages) == 2:
            raise RuntimeError("CDP page construction failed")
        page = FakePage()
        pages.append(page)
        return page

    with pytest.raises(RuntimeError, match="construction failed"):
        browser_adapters.register_managed_chrome_adapters(
            registry,
            chrome,  # type: ignore[arg-type]
            connector=connector,
        )
    assert registry.names() == ()
    assert pages and all(page.closed for page in pages)
    assert chrome.close_calls == 1


def test_managed_registry_preserves_connector_error_when_cleanup_raises() -> None:
    class ChromeStub:
        cdp_url = "http://127.0.0.1:49222"

        def close(self) -> None:
            raise RuntimeError("Authorization: Bearer registry-chrome-secret")

    class PageStub(FakePage):
        def close(self) -> None:
            raise RuntimeError("Cookie: registry-page-secret")

    calls = 0

    def connector(_cdp_url: str) -> FakePage:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("decisive registry connector error")
        return PageStub()

    with pytest.raises(RuntimeError, match="decisive registry connector error") as caught:
        browser_adapters.register_managed_chrome_adapters(
            AdapterRegistry(),
            ChromeStub(),  # type: ignore[arg-type]
            connector=connector,
        )
    notes = "\n".join(getattr(caught.value, "__notes__", []))
    assert "cleanup diagnostic" in notes
    assert "registry-page-secret" not in notes
    assert "registry-chrome-secret" not in notes
    assert notes.count("[REDACTED]") >= 2


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
    result = make_publisher(platform, page).probe_auth(account(platform))
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
    result = make_publisher(platform, page).probe_auth(account(platform, expected))
    assert result.authenticated is True
    assert result.observed_identity == expected


@pytest.mark.parametrize("generic_label", ["Profile", "Account", "Account menu", "Your profile"])
def test_probe_rejects_generic_account_control_labels(
    platform: str, generic_label: str
) -> None:
    result = make_publisher(platform, FakePage(generic_label)).probe_auth(
        account(platform, generic_label)
    )
    assert result.authenticated is False
    assert result.observed_identity == ""
    assert result.error_category == ErrorCategory.AUTHENTICATION


def test_generic_identity_hard_blocks_before_upload(tmp_path: Path) -> None:
    page = FakePage("Account")
    result = make_publisher("youtube", page).publish(
        publish_request(tmp_path, "youtube", expected_identity="Account")
    )
    assert result.status == PublishStatus.BLOCKED
    assert not any(action[0] == "upload" for action in page.actions)


def test_identity_mismatch_blocks_before_upload(tmp_path: Path, platform: str) -> None:
    page = FakePage("other@example.test")
    result = make_publisher(platform, page).publish(
        publish_request(tmp_path, platform)
    )
    assert result.status == PublishStatus.BLOCKED
    assert result.error_category == ErrorCategory.IDENTITY_MISMATCH
    assert not any(action[0] == "upload" for action in page.actions)


def test_missing_media_blocks_before_upload(tmp_path: Path, platform: str) -> None:
    request = publish_request(tmp_path, platform)
    request.media_path.unlink()
    page = FakePage()
    result = make_publisher(platform, page).publish(request)
    assert result.status == PublishStatus.BLOCKED
    assert result.error_category == ErrorCategory.VALIDATION
    assert not any(action[0] == "upload" for action in page.actions)


def test_dry_run_never_submits_and_returns_evidence(tmp_path: Path, platform: str) -> None:
    page = FakePage()
    result = make_publisher(platform, page).publish(
        publish_request(tmp_path, platform, dry_run=True)
    )
    assert result.status == PublishStatus.SKIPPED
    assert result.evidence["dry_run"] is True
    assert not page.submit_clicked
    assert not any(action[0] == "upload" for action in page.actions)


def test_publish_requires_positive_submit_evidence(tmp_path: Path, platform: str) -> None:
    page = FakePage()
    result = make_publisher(platform, page).publish(publish_request(tmp_path, platform))
    assert page.submit_clicked is True
    assert result.status == PublishStatus.UNKNOWN
    assert result.error_category == ErrorCategory.AMBIGUOUS_SUBMIT
    assert result.evidence["submit_clicked"] is True
    assert result.evidence["retry_safe"] is False


def test_explicit_confirmation_is_success(tmp_path: Path, platform: str) -> None:
    page = FakePage()
    confirmation = {
        "instagram": "Your post has been shared",
        "tiktok": "Your video is being uploaded",
        "youtube": "Video published",
        "x": "Your post was sent",
        "facebook": "Your reel is being published",
        "threads": "Your thread was posted",
    }[platform]
    set_text_evidence(page, platform, "confirmation", confirmation)
    result = make_publisher(platform, page).publish(publish_request(tmp_path, platform))
    expected = (
        PublishStatus.SUBMITTED
        if platform in {"tiktok", "facebook"}
        else PublishStatus.PUBLISHED
    )
    assert result.status == expected
    assert result.evidence["confirmation"] == confirmation


def test_generic_confirmation_never_counts_as_positive_evidence(
    tmp_path: Path, platform: str
) -> None:
    page = FakePage()
    set_text_evidence(page, platform, "confirmation", "done")
    result = make_publisher(platform, page).publish(publish_request(tmp_path, platform))
    assert result.status == PublishStatus.UNKNOWN
    assert result.error_category == ErrorCategory.AMBIGUOUS_SUBMIT


def test_stale_platform_confirmation_never_counts_as_positive_evidence(
    tmp_path: Path,
) -> None:
    page = FakePage()
    set_text_evidence(page, "x", "confirmation", "Your post was sent", after_submit=False)
    set_text_evidence(page, "x", "confirmation", "Your post was sent")
    result = make_publisher("x", page).publish(publish_request(tmp_path, "x"))
    assert result.status == PublishStatus.UNKNOWN
    assert result.error_category == ErrorCategory.AMBIGUOUS_SUBMIT


def test_permalink_is_authoritative_success(tmp_path: Path, platform: str) -> None:
    page = FakePage()
    permalink = {
        "instagram": "https://www.instagram.com/reel/abc123/",
        "tiktok": "https://www.tiktok.com/@creator/video/123456",
        "youtube": "https://www.youtube.com/watch?v=abc123",
        "x": "https://x.com/creator/status/123456",
        "facebook": "https://www.facebook.com/reel/abc123/",
        "threads": "https://www.threads.net/@creator/post/abc123/",
    }[platform]
    set_link_evidence(page, platform, "permalink", permalink)
    result = make_publisher(platform, page).publish(publish_request(tmp_path, platform))
    expected = (
        PublishStatus.SUBMITTED
        if platform in {"tiktok", "youtube", "facebook"}
        else PublishStatus.PUBLISHED
    )
    assert result.status == expected
    assert result.post_url == permalink


def test_platform_id_is_authoritative_success(tmp_path: Path, platform: str) -> None:
    page = FakePage()
    platform_id = {
        "instagram": "C9abc_123",
        "tiktok": "7391234567890123456",
        "youtube": "aB3_defGh-I",
        "x": "1812345678901234567",
        "facebook": "123456789012345",
        "threads": "C9abc_123",
    }[platform]
    set_text_evidence(page, platform, "platform_id", platform_id)
    result = make_publisher(platform, page).publish(publish_request(tmp_path, platform))
    expected = (
        PublishStatus.SUBMITTED
        if platform in {"tiktok", "youtube", "facebook"}
        else PublishStatus.PUBLISHED
    )
    assert result.status == expected
    assert result.platform_id == platform_id


def test_generic_platform_id_never_counts_as_positive_evidence(
    tmp_path: Path, platform: str
) -> None:
    page = FakePage()
    set_text_evidence(page, platform, "platform_id", "done")
    result = make_publisher(platform, page).publish(publish_request(tmp_path, platform))
    assert result.status == PublishStatus.UNKNOWN


def test_matching_current_url_is_authoritative_success(tmp_path: Path) -> None:
    page = FakePage()
    page.url_after_submit = "https://x.com/creator/status/123456"
    result = make_publisher("x", page).publish(publish_request(tmp_path, "x"))
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
    make_publisher(platform, page).publish(publish_request(tmp_path, platform))
    simplified = {(str(action[0]), str(action[1])) for action in page.actions}
    assert expected_actions <= simplified


@pytest.mark.parametrize("platform", ["tiktok", "youtube", "facebook"])
def test_scheduling_selects_mode_date_time_and_conditional_action(
    tmp_path: Path, platform: str
) -> None:
    page = FakePage()
    expose_schedule_controls(page, platform)
    scheduled_confirmation = {
        "tiktok": "Video scheduled for Jul 20, 2026",
        "youtube": "Video scheduled for Jul 20, 2026",
        "facebook": "Reel scheduled for Jul 20, 2026",
    }[platform]
    set_text_evidence(page, platform, "schedule_confirmation", scheduled_confirmation)
    request = publish_request(tmp_path, platform, scheduled=True)
    result = make_publisher(platform, page).publish(request)
    assert result.status == PublishStatus.SCHEDULED
    assert ("click", "schedule_mode") in page.actions
    assert ("fill", "schedule_date", "2026-07-20") in page.actions
    assert ("fill", "schedule_time", "17:00") in page.actions
    assert page.submitted_action == "schedule_submit"
    assert ("click", "submit") not in page.actions


@pytest.mark.parametrize("platform", ["tiktok", "youtube", "facebook"])
def test_schedule_evidence_uses_distinct_locator_values(platform: str) -> None:
    data = load_browser_selectors()
    locators = data["platforms"][platform]["locators"]
    for immediate, scheduled in (
        ("confirmation", "schedule_confirmation"),
        ("permalink", "schedule_permalink"),
        ("platform_id", "schedule_platform_id"),
    ):
        assert locators[immediate]["value"] != locators[scheduled]["value"]


@pytest.mark.parametrize(
    ("platform", "purpose", "value"),
    [
        ("tiktok", "schedule_confirmation", "Video scheduled for Jul 20, 2026"),
        ("youtube", "schedule_confirmation", "Video scheduled for Jul 20, 2026"),
        ("facebook", "schedule_confirmation", "Reel scheduled for Jul 20, 2026"),
        (
            "tiktok",
            "schedule_permalink",
            "https://www.tiktok.com/@creator/video/7391234567890123456",
        ),
        (
            "youtube",
            "schedule_permalink",
            "https://www.youtube.com/watch?v=aB3_defGh-I",
        ),
        (
            "facebook",
            "schedule_permalink",
            "https://www.facebook.com/reel/123456789012345/",
        ),
        ("tiktok", "schedule_platform_id", "7391234567890123456"),
        ("youtube", "schedule_platform_id", "aB3_defGh-I"),
        ("facebook", "schedule_platform_id", "123456789012345"),
    ],
)
def test_schedule_specific_confirmation_permalink_or_id_proves_schedule(
    tmp_path: Path, platform: str, purpose: str, value: str
) -> None:
    page = FakePage()
    expose_schedule_controls(page, platform)
    if purpose.endswith("permalink"):
        set_link_evidence(page, platform, purpose, value)
    else:
        set_text_evidence(page, platform, purpose, value)
    result = make_publisher(platform, page).publish(
        publish_request(tmp_path, platform, scheduled=True)
    )
    assert result.status == PublishStatus.SCHEDULED
    if purpose.endswith("permalink"):
        assert result.post_url == value
    if purpose.endswith("platform_id"):
        assert result.platform_id == value


def test_youtube_create_upload_and_wizard_steps_precede_schedule_controls(
    tmp_path: Path,
) -> None:
    page = FakePage()
    expose_schedule_controls(page, "youtube")
    set_text_evidence(
        page,
        "youtube",
        "schedule_confirmation",
        "Video scheduled for Jul 20, 2026",
    )
    result = make_publisher("youtube", page).publish(
        publish_request(tmp_path, "youtube", scheduled=True)
    )
    assert result.status == PublishStatus.SCHEDULED
    operation_names = [
        str(action[1])
        for action in page.actions
        if action[0] in {"click", "upload"}
    ]
    expected = [
        "identity_menu",
        "create",
        "upload_videos",
        "upload",
        "wizard_next",
        "wizard_next",
        "wizard_next",
        "schedule_mode",
        "schedule_submit",
    ]
    cursor = 0
    for name in expected:
        cursor = operation_names.index(name, cursor) + 1


def test_youtube_details_are_applied_before_advancing_wizard(tmp_path: Path) -> None:
    page = FakePage()
    expose_schedule_controls(page, "youtube")
    set_text_evidence(
        page,
        "youtube",
        "schedule_confirmation",
        "Video scheduled for Jul 20, 2026",
    )
    result = make_publisher("youtube", page).publish(
        publish_request(tmp_path, "youtube", scheduled=True)
    )
    assert result.status == PublishStatus.SCHEDULED
    first_next = page.actions.index(("click", "wizard_next"))
    assert page.actions.index(("fill", "title", "A useful title")) < first_next
    assert page.actions.index(("check", "audience_not_made_for_kids", True)) < first_next


def test_youtube_immediate_flow_respects_details_wizard_visibility_phases(
    tmp_path: Path,
) -> None:
    page = FakePage()
    set_text_evidence(page, "youtube", "confirmation", "Video published")
    request = publish_request(tmp_path, "youtube")
    result = make_publisher("youtube", page).publish(request)
    assert result.status == PublishStatus.PUBLISHED
    assert_actions_in_order(
        page.actions,
        [
            ("click", "create"),
            ("click", "upload_videos"),
            ("upload", "upload", str(request.media_path)),
            ("fill", "title", "A useful title"),
            ("fill", "description", "A useful description"),
            ("check", "audience_not_made_for_kids", True),
            ("check", "synthetic_media", True),
            ("click", "wizard_next"),
            ("click", "wizard_next"),
            ("click", "wizard_next"),
            ("select", "visibility", "unlisted"),
            ("click", "submit"),
        ],
    )


def test_youtube_scheduled_flow_respects_details_wizard_visibility_phases(
    tmp_path: Path,
) -> None:
    page = FakePage()
    expose_schedule_controls(page, "youtube")
    set_text_evidence(
        page,
        "youtube",
        "schedule_confirmation",
        "Video scheduled for Jul 20, 2026",
    )
    request = publish_request(tmp_path, "youtube", scheduled=True)
    result = make_publisher("youtube", page).publish(request)
    assert result.status == PublishStatus.SCHEDULED
    assert_actions_in_order(
        page.actions,
        [
            ("click", "create"),
            ("click", "upload_videos"),
            ("upload", "upload", str(request.media_path)),
            ("fill", "title", "A useful title"),
            ("fill", "description", "A useful description"),
            ("check", "audience_not_made_for_kids", True),
            ("check", "synthetic_media", True),
            ("click", "wizard_next"),
            ("click", "wizard_next"),
            ("click", "wizard_next"),
            ("select", "visibility", "unlisted"),
            ("click", "schedule_mode"),
            ("fill", "schedule_date", "2026-07-20"),
            ("fill", "schedule_time", "17:00"),
            ("click", "schedule_submit"),
        ],
    )


@pytest.mark.parametrize("platform", ["tiktok", "youtube", "facebook"])
def test_unavailable_schedule_controls_never_fall_back_to_immediate_publish(
    tmp_path: Path, platform: str
) -> None:
    page = FakePage()
    result = make_publisher(platform, page).publish(
        publish_request(tmp_path, platform, scheduled=True)
    )
    assert result.status in {PublishStatus.FAILED, PublishStatus.UNKNOWN}
    assert any(action[0] == "upload" for action in page.actions)
    assert page.submit_clicked is False
    assert ("click", "submit") not in page.actions


@pytest.mark.parametrize("platform", ["tiktok", "youtube", "facebook"])
def test_schedule_action_requires_schedule_specific_positive_evidence(
    tmp_path: Path, platform: str
) -> None:
    page = FakePage()
    expose_schedule_controls(page, platform)
    immediate_confirmation = {
        "tiktok": "Your video is being uploaded",
        "youtube": "Video published",
        "facebook": "Your reel is being published",
    }[platform]
    set_text_evidence(page, platform, "confirmation", immediate_confirmation)
    result = make_publisher(platform, page).publish(
        publish_request(tmp_path, platform, scheduled=True)
    )
    assert page.submitted_action == "schedule_submit"
    assert result.status == PublishStatus.UNKNOWN
    assert result.error_category == ErrorCategory.AMBIGUOUS_SUBMIT


@pytest.mark.parametrize(
    ("scheduled_at", "expected_detail"),
    [
        ("2026-07-20", "date-time"),
        ("2026-07-20T17:00:00", "timezone"),
    ],
)
@pytest.mark.parametrize("platform", ["tiktok", "youtube", "facebook"])
def test_schedule_requires_timezone_aware_date_and_time_before_upload(
    tmp_path: Path, platform: str, scheduled_at: str, expected_detail: str
) -> None:
    page = FakePage()
    request = publish_request(tmp_path, platform, scheduled=True)
    request.metadata["scheduled_at"] = scheduled_at
    result = make_publisher(platform, page).publish(request)
    assert result.status == PublishStatus.BLOCKED
    assert result.error_category == ErrorCategory.VALIDATION
    assert expected_detail in result.detail
    assert not any(action[0] == "upload" for action in page.actions)
    assert not page.submit_clicked


def test_unscheduled_confirmation_is_published(tmp_path: Path) -> None:
    page = FakePage()
    set_text_evidence(page, "threads", "confirmation", "Your thread was posted")
    request = publish_request(tmp_path, "threads")
    result = make_publisher("threads", page).publish(request)
    assert result.status == PublishStatus.PUBLISHED


@pytest.mark.parametrize(
    ("purpose", "value"),
    [
        ("confirmation", "Your post was sent"),
        ("permalink", "https://x.com/creator/status/1812345678901234567"),
        ("platform_id", "1812345678901234567"),
    ],
)
def test_final_action_polls_until_async_positive_evidence(
    tmp_path: Path, purpose: str, value: str
) -> None:
    page = FakePage()
    page.reveal_after_polls = 2
    if purpose == "permalink":
        set_link_evidence(page, "x", purpose, value)
    else:
        set_text_evidence(page, "x", purpose, value)
    clock = FakeClock()
    adapter = BrowserPublisher(
        "x",
        page,
        evidence_timeout=2.0,
        evidence_poll_interval=0.25,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    result = adapter.publish(publish_request(tmp_path, "x"))
    assert result.status == PublishStatus.PUBLISHED
    assert clock.sleeps == [0.25, 0.25]


def test_final_action_poll_timeout_is_ambiguous_and_never_retry_safe(
    tmp_path: Path,
) -> None:
    page = FakePage()
    clock = FakeClock()
    adapter = BrowserPublisher(
        "x",
        page,
        evidence_timeout=1.0,
        evidence_poll_interval=0.25,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    result = adapter.publish(publish_request(tmp_path, "x"))
    assert result.status == PublishStatus.UNKNOWN
    assert result.error_category == ErrorCategory.AMBIGUOUS_SUBMIT
    assert result.evidence["retry_safe"] is False
    assert clock.now == 1.0
    assert page.actions.count(("click", "submit")) == 1


def test_browser_exception_is_redacted(tmp_path: Path) -> None:
    page = FakePage()
    page.raise_on = "submit"
    result = make_publisher("x", page).publish(publish_request(tmp_path, "x"))
    assert result.status == PublishStatus.UNKNOWN
    assert result.error_category == ErrorCategory.AMBIGUOUS_SUBMIT
    assert "very-secret-token" not in result.detail


def test_pre_submit_browser_exception_is_redacted(tmp_path: Path) -> None:
    page = FakePage()
    page.raise_on = "upload"
    result = make_publisher("x", page).publish(publish_request(tmp_path, "x"))
    assert result.status == PublishStatus.FAILED
    assert "very-secret-token" not in result.detail
    assert "[REDACTED]" in result.detail


def test_processing_confirmation_is_submitted(tmp_path: Path) -> None:
    page = FakePage()
    set_text_evidence(page, "tiktok", "confirmation", "Your video is being uploaded")
    request = publish_request(tmp_path, "tiktok")
    result = make_publisher("tiktok", page).publish(request)
    assert result.status == PublishStatus.SUBMITTED


def test_tiktok_filename_slug_and_caption_are_distinct_operations(tmp_path: Path) -> None:
    data = load_browser_selectors()
    locators = data["platforms"]["tiktok"]["locators"]
    assert locators["filename_slug"]["value"] != locators["caption"]["value"]

    page = FakePage()
    make_publisher("tiktok", page).publish(publish_request(tmp_path, "tiktok"))
    assert ("fill", "filename_slug", "useful-video-file") in page.actions
    assert ("fill", "caption", "A useful caption #launch") in page.actions
