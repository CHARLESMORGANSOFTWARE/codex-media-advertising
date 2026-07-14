"""Best-effort, receipt-safe browser publishers for the six supported destinations.

The page object is deliberately a small protocol.  A Playwright/CDP bridge can
implement it without making these adapters depend on Playwright at import time,
and tests can exercise every publication outcome without a live account.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from importlib import import_module
from importlib.resources import files
from pathlib import Path
from typing import Callable, Mapping, Protocol, cast

from ..models import AccountConfig, PublishRequest, PublishResult, PublishStatus
from .base import (
    SUPPORTED_PLATFORMS,
    AdapterRegistry,
    ErrorCategory,
    ProbeResult,
    ValidationResult,
    normalize_adapter_error,
    probe_identity,
    redact_diagnostic,
)
from .chrome import ManagedChrome


Locator = dict[str, str]


class BrowserPage(Protocol):
    """The semantic page operations required by browser publishers."""

    def goto(self, url: str) -> None: ...
    def body_text(self) -> str: ...
    def current_url(self) -> str: ...
    def text(self, locator: Locator) -> str: ...
    def attribute(self, locator: Locator, name: str) -> str: ...
    def is_visible(self, locator: Locator) -> bool: ...
    def is_enabled(self, locator: Locator) -> bool: ...
    def click(self, locator: Locator) -> None: ...
    def fill(self, locator: Locator, value: str) -> None: ...
    def set_input_files(self, locator: Locator, path: Path) -> None: ...
    def select_option(self, locator: Locator, value: str) -> None: ...
    def check(self, locator: Locator, checked: bool) -> None: ...
    def wait_for(self, locator: Locator) -> None: ...


class BrowserDependencyError(RuntimeError):
    """Raised when the optional browser runtime is not installed."""


class BrowserUIError(RuntimeError):
    """Raised when a required, pre-submit platform control is unavailable."""


class PlaywrightBrowserPage:
    """Synchronous Playwright page attached to an isolated Chrome CDP endpoint.

    Playwright is imported only when a browser connection is requested, so the
    core package and non-browser adapters remain usable without the optional
    ``browser`` dependency group.
    """

    def __init__(self, page: object, playwright: object, browser: object) -> None:
        self._page = page
        self._playwright = playwright
        self._browser = browser
        self._closed = False

    @classmethod
    def connect(cls, cdp_url: str) -> PlaywrightBrowserPage:
        try:
            sync_api = import_module("playwright.sync_api")
        except ModuleNotFoundError as exc:
            if exc.name not in {"playwright", "playwright.sync_api"}:
                raise
            raise BrowserDependencyError(
                "browser dependency playwright is unavailable; install "
                "codex-media-advertising[browser]"
            ) from exc
        manager = sync_api.sync_playwright().start()
        try:
            browser = manager.chromium.connect_over_cdp(cdp_url)
            contexts = list(browser.contexts)
            if not contexts:
                raise BrowserUIError("managed Chrome exposed no browser context over CDP")
            page = contexts[0].new_page()
        except BaseException:
            manager.stop()
            raise
        return cls(page, manager, browser)

    def goto(self, url: str) -> None:
        self._page.goto(url)

    def body_text(self) -> str:
        return str(self._page.locator("body").inner_text())

    def current_url(self) -> str:
        return str(self._page.url)

    def text(self, locator: Locator) -> str:
        return str(self._resolve(locator).inner_text() or "")

    def attribute(self, locator: Locator, name: str) -> str:
        return str(self._resolve(locator).get_attribute(name) or "")

    def is_visible(self, locator: Locator) -> bool:
        return bool(self._resolve(locator).is_visible())

    def is_enabled(self, locator: Locator) -> bool:
        return bool(self._resolve(locator).is_enabled())

    def click(self, locator: Locator) -> None:
        self._resolve(locator).click()

    def fill(self, locator: Locator, value: str) -> None:
        self._resolve(locator).fill(value)

    def set_input_files(self, locator: Locator, path: Path) -> None:
        self._resolve(locator).set_input_files(str(path))

    def select_option(self, locator: Locator, value: str) -> None:
        self._resolve(locator).select_option(value)

    def check(self, locator: Locator, checked: bool) -> None:
        target = self._resolve(locator)
        if checked:
            target.check()
        else:
            target.uncheck()

    def wait_for(self, locator: Locator) -> None:
        self._resolve(locator).wait_for(state="visible")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._page.close()
        finally:
            # Stopping Playwright disconnects this client. ManagedChrome owns
            # and terminates the isolated Chrome process itself.
            self._playwright.stop()

    def _resolve(self, locator: Locator) -> object:
        kind = locator["kind"]
        value = locator["value"]
        if kind == "role":
            target = self._page.get_by_role(value, name=locator["name"])
        elif kind == "text":
            target = self._page.get_by_text(value, exact=False)
        elif kind == "label":
            target = self._page.get_by_label(value, exact=False)
        else:
            target = self._page.locator(value)
        return target.first


_SELECTOR_RESOURCE = "browser_selectors.v1.json"
_SEMANTIC_KINDS = {"role", "text", "label"}
_CSS_PURPOSES = {"upload", "stable_control"}
_COMMON_LOCATORS = {
    "identity",
    "upload",
    "caption",
    "submit",
    "confirmation",
    "permalink",
    "platform_id",
}
_SCHEDULE_LOCATORS = {
    "schedule_mode",
    "schedule_date",
    "schedule_time",
    "schedule_submit",
    "schedule_confirmation",
    "schedule_permalink",
    "schedule_platform_id",
}
_PLATFORM_LOCATORS = {
    "instagram": _COMMON_LOCATORS | {"create", "advance"},
    "tiktok": {
        *_COMMON_LOCATORS,
        "filename_slug",
        "allow_comments",
        "allow_duet",
        "allow_stitch",
        *_SCHEDULE_LOCATORS,
    },
    "youtube": {
        *_COMMON_LOCATORS,
        "identity_menu",
        "create",
        "upload_videos",
        "wizard_next",
        "title",
        "description",
        "audience_made_for_kids",
        "audience_not_made_for_kids",
        "synthetic_media",
        "visibility",
        *_SCHEDULE_LOCATORS,
    },
    "x": _COMMON_LOCATORS | {"processing"},
    "facebook": {
        *_COMMON_LOCATORS,
        *_SCHEDULE_LOCATORS,
    },
    "threads": _COMMON_LOCATORS | {"create"},
}
_SCHEDULE_PLATFORMS = {"tiktok", "youtube", "facebook"}


@dataclass(frozen=True)
class _BrowserFlowPlan:
    """Ordered controls that move one platform between publishing phases."""

    probe: tuple[str, ...] = ()
    open_upload: tuple[str, ...] = ()
    after_upload: tuple[str, ...] = ()
    after_details: tuple[str, ...] = ()


_PLATFORM_FLOWS = {
    "instagram": _BrowserFlowPlan(
        open_upload=("create",), after_upload=("advance", "advance")
    ),
    "tiktok": _BrowserFlowPlan(),
    "youtube": _BrowserFlowPlan(
        probe=("identity_menu",),
        open_upload=("create", "upload_videos"),
        after_details=("wizard_next", "wizard_next", "wizard_next"),
    ),
    "x": _BrowserFlowPlan(after_upload=("processing",)),
    "facebook": _BrowserFlowPlan(),
    "threads": _BrowserFlowPlan(open_upload=("create",)),
}
_FLOW_CONFIG_FIELDS = {
    "probe_operations": "probe",
    "open_upload_operations": "open_upload",
    "after_upload_operations": "after_upload",
    "after_details_operations": "after_details",
}
_GENERIC_LOGGED_OUT = re.compile(r"\b(?:log\s*in|sign\s*in)\b", re.IGNORECASE)
_GENERIC_IDENTITIES = {
    "account",
    "account menu",
    "profile",
    "your account",
    "your profile",
}
_BASE_PLATFORM_FIELDS = {
    "url",
    "login_markers",
    "post_link_pattern",
    "supports_schedule",
    "identity_evidence",
    "confirmation_pattern",
    "platform_id_pattern",
    *_FLOW_CONFIG_FIELDS,
    "locators",
}
_SCHEDULE_PLATFORM_FIELDS = {
    "schedule_confirmation_pattern",
    "schedule_post_link_pattern",
    "schedule_platform_id_pattern",
}


def load_browser_selectors(path: Path | None = None) -> dict[str, object]:
    """Load and strictly validate the versioned browser selector contract."""

    if path is None:
        raw = files("codex_media_ads.publishing").joinpath(_SELECTOR_RESOURCE).read_text(
            encoding="utf-8"
        )
    else:
        raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    return _validate_selector_data(data)


def _validate_selector_data(data: object) -> dict[str, object]:
    if not isinstance(data, dict) or data.get("schema_version") != "1":
        raise ValueError("browser selectors must use schema_version 1")
    platforms = data.get("platforms")
    if not isinstance(platforms, dict) or set(platforms) != set(SUPPORTED_PLATFORMS):
        raise ValueError("browser selectors must define exactly the six supported platforms")
    for platform, raw_config in platforms.items():
        if not isinstance(raw_config, dict):
            raise ValueError(f"{platform} selector configuration must be an object")
        supports_schedule = platform in _SCHEDULE_PLATFORMS
        if raw_config.get("supports_schedule") is not supports_schedule:
            raise ValueError(
                f"{platform} supports_schedule must be {str(supports_schedule).lower()}"
            )
        expected_fields = _BASE_PLATFORM_FIELDS | (
            _SCHEDULE_PLATFORM_FIELDS if supports_schedule else set()
        )
        if set(raw_config) != expected_fields:
            unknown_fields = sorted(set(raw_config) - expected_fields)
            missing_fields = sorted(expected_fields - set(raw_config))
            parts = []
            if unknown_fields:
                parts.append(f"unknown fields: {', '.join(unknown_fields)}")
            if missing_fields:
                parts.append(f"missing fields: {', '.join(missing_fields)}")
            raise ValueError(
                f"{platform} selector configuration must match its exact schema; "
                + "; ".join(parts)
            )
        if not _valid_https_url(raw_config.get("url")):
            raise ValueError(f"{platform} browser URL must be HTTPS")
        markers = raw_config.get("login_markers")
        if not isinstance(markers, list) or not markers or not all(
            isinstance(marker, str) and marker.strip() for marker in markers
        ):
            raise ValueError(f"{platform} login markers must be non-empty strings")
        pattern = raw_config.get("post_link_pattern")
        _validate_pattern(platform, "post_link_pattern", pattern)
        _validate_pattern(
            platform, "confirmation_pattern", raw_config.get("confirmation_pattern")
        )
        _validate_pattern(
            platform, "platform_id_pattern", raw_config.get("platform_id_pattern")
        )
        if supports_schedule:
            _validate_pattern(
                platform,
                "schedule_confirmation_pattern",
                raw_config.get("schedule_confirmation_pattern"),
            )
            _validate_pattern(
                platform,
                "schedule_post_link_pattern",
                raw_config.get("schedule_post_link_pattern"),
            )
            _validate_pattern(
                platform,
                "schedule_platform_id_pattern",
                raw_config.get("schedule_platform_id_pattern"),
            )
        locators = raw_config.get("locators")
        expected_locators = _PLATFORM_LOCATORS[platform]
        if not isinstance(locators, dict) or set(locators) != expected_locators:
            actual_locators = set(locators or {})
            missing = sorted(expected_locators - actual_locators)
            extra = sorted(actual_locators - expected_locators)
            parts = []
            if missing:
                parts.append(f"missing locators: {', '.join(missing)}")
            if extra:
                parts.append(f"unknown locators: {', '.join(extra)}")
            raise ValueError(
                f"{platform} selectors must match the exact intended locator schema; "
                + "; ".join(parts)
            )
        for purpose, raw_locator in locators.items():
            _validate_locator(platform, str(purpose), raw_locator)
        flow = _PLATFORM_FLOWS[platform]
        for key, phase in _FLOW_CONFIG_FIELDS.items():
            sequence = raw_config.get(key, [])
            if sequence != list(getattr(flow, phase)):
                raise ValueError(
                    f"{platform} {key} operations must match the intended sequence"
                )
        if supports_schedule:
            for immediate, scheduled in (
                ("confirmation", "schedule_confirmation"),
                ("permalink", "schedule_permalink"),
                ("platform_id", "schedule_platform_id"),
            ):
                if locators[immediate]["value"] == locators[scheduled]["value"]:
                    raise ValueError(
                        f"{platform} {scheduled} must be distinct from {immediate}"
                    )
        identity_evidence = raw_config.get("identity_evidence")
        if not isinstance(identity_evidence, list) or not identity_evidence:
            raise ValueError(f"{platform} identity evidence is required")
        for item in identity_evidence:
            _validate_identity_evidence(platform, item)
    return cast(dict[str, object], data)


def _validate_pattern(platform: str, field: str, value: object) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{platform} {field.replace('_', ' ')} is required")
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"{platform} {field.replace('_', ' ')} is invalid") from exc


def _validate_identity_evidence(platform: str, value: object) -> None:
    if not isinstance(value, dict) or set(value) - {"source", "attribute", "pattern"}:
        raise ValueError(f"{platform} identity evidence entry is invalid")
    source = value.get("source")
    if source not in {"text", "attribute"}:
        raise ValueError(f"{platform} identity evidence source is invalid")
    if source == "attribute" and not isinstance(value.get("attribute"), str):
        raise ValueError(f"{platform} identity attribute is required")
    if source == "text" and "attribute" in value:
        raise ValueError(f"{platform} text identity evidence cannot name an attribute")
    _validate_pattern(platform, "identity_pattern", value.get("pattern"))
    compiled = re.compile(cast(str, value["pattern"]))
    if "identity" not in compiled.groupindex:
        raise ValueError(f"{platform} identity pattern must define an identity group")


def _valid_https_url(value: object) -> bool:
    return isinstance(value, str) and value.startswith("https://") and " " not in value


def _validate_locator(platform: str, purpose: str, raw: object) -> None:
    if not isinstance(raw, dict):
        raise ValueError(f"{platform}.{purpose} locator must be an object")
    kind = raw.get("kind")
    value = raw.get("value")
    declared_purpose = raw.get("purpose")
    if declared_purpose != purpose:
        raise ValueError(f"{platform}.{purpose} locator purpose must match its key")
    if kind not in _SEMANTIC_KINDS | {"css"}:
        raise ValueError(f"{platform}.{purpose} locator kind is unsupported")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{platform}.{purpose} locator value is required")
    if kind == "css" and purpose not in _CSS_PURPOSES:
        raise ValueError(f"CSS is not allowed for semantic locator {platform}.{purpose}")
    if kind != "css" and purpose == "upload":
        raise ValueError(f"{platform}.upload must use the file input CSS locator")
    if kind == "role" and (
        not isinstance(raw.get("name"), str) or not str(raw["name"]).strip()
    ):
        raise ValueError(f"{platform}.{purpose} role locator requires a name")
    allowed = {"kind", "value", "purpose", "name"}
    if set(raw) - allowed:
        raise ValueError(f"{platform}.{purpose} locator has unknown fields")


class _SharedManagedRuntime:
    """Reference-counted ownership for pages sharing one ManagedChrome run."""

    def __init__(self, chrome: ManagedChrome, leases: int) -> None:
        if leases <= 0:
            raise ValueError("shared browser runtime requires at least one lease")
        self._chrome = chrome
        self._leases = leases
        self._closed = False

    def release(self) -> None:
        if self._closed:
            return
        self._leases -= 1
        if self._leases == 0:
            self._closed = True
            self._chrome.close()


def _cleanup_failed_browser_setup(
    original: BaseException,
    chrome: ManagedChrome,
    pages: tuple[BrowserPage, ...] = (),
) -> None:
    """Clean partial setup without replacing its decisive exception."""

    diagnostics: list[str] = []
    for index, page in enumerate(reversed(pages), start=1):
        close_page = getattr(page, "close", None)
        if callable(close_page):
            try:
                close_page()
            except Exception as cleanup_error:
                diagnostics.append(
                    f"page[{index}]={redact_diagnostic(str(cleanup_error))}"
                )
    try:
        chrome.close()
    except Exception as cleanup_error:
        diagnostics.append(
            f"managed_chrome={redact_diagnostic(str(cleanup_error))}"
        )
    if diagnostics:
        original.add_note("cleanup diagnostic: " + "; ".join(diagnostics))


class BrowserPublisher:
    """Data-driven browser adapter with conservative publication evidence rules."""

    def __init__(
        self,
        platform: str,
        page: BrowserPage,
        selectors: Mapping[str, object] | None = None,
        *,
        managed_chrome: ManagedChrome | None = None,
        runtime_release: Callable[[], None] | None = None,
        evidence_timeout: float = 15.0,
        evidence_poll_interval: float = 0.25,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        normalized = platform.strip().lower()
        if normalized not in SUPPORTED_PLATFORMS:
            raise ValueError(f"unsupported platform: {normalized or '<empty>'}")
        selector_data = (
            _validate_selector_data(dict(selectors))
            if selectors is not None
            else load_browser_selectors()
        )
        self.platform = normalized
        self.page = page
        if managed_chrome is not None and runtime_release is not None:
            raise ValueError("browser runtime ownership must have one release path")
        if evidence_timeout <= 0 or evidence_poll_interval <= 0:
            raise ValueError("browser evidence polling bounds must be positive")
        self._runtime_release = (
            managed_chrome.close if managed_chrome is not None else runtime_release
        )
        self._closed = False
        self._evidence_timeout = evidence_timeout
        self._evidence_poll_interval = evidence_poll_interval
        self._monotonic = monotonic
        self._sleep = sleep
        self._config = cast(
            dict[str, object],
            cast(dict[str, object], selector_data["platforms"])[normalized],
        )
        self._locators = cast(dict[str, Locator], self._config["locators"])
        self._flow = _PLATFORM_FLOWS[normalized]

    @classmethod
    def from_managed_chrome(
        cls,
        platform: str,
        chrome: ManagedChrome,
        connector: Callable[[str], BrowserPage] | None = None,
        selectors: Mapping[str, object] | None = None,
    ) -> BrowserPublisher:
        """Connect a page abstraction to Task 6's isolated loopback CDP runtime."""

        if connector is None:
            connector = PlaywrightBrowserPage.connect
        try:
            page = connector(chrome.cdp_url)
        except BaseException as exc:
            _cleanup_failed_browser_setup(exc, chrome)
            raise
        try:
            return cls(
                platform,
                page,
                selectors,
                managed_chrome=chrome,
            )
        except BaseException as exc:
            _cleanup_failed_browser_setup(exc, chrome, (page,))
            raise

    def close(self) -> None:
        """Release the page bridge and only this adapter's managed Chrome clone."""

        if self._closed:
            return
        self._closed = True
        try:
            close_page = getattr(self.page, "close", None)
            if callable(close_page):
                close_page()
        finally:
            if self._runtime_release is not None:
                self._runtime_release()

    def probe_auth(self, account: AccountConfig) -> ProbeResult:
        try:
            self.page.goto(cast(str, self._config["url"]))
            body = self.page.body_text()
            if _GENERIC_LOGGED_OUT.search(body) or any(
                marker.casefold() in body.casefold()
                for marker in cast(list[str], self._config["login_markers"])
            ):
                return ProbeResult(
                    authenticated=False,
                    error_category=ErrorCategory.AUTHENTICATION,
                    detail="the browser session is logged out",
                    next_action="Sign in to the configured profile and probe again.",
                )
            self._run_phase(self._flow.probe)
            observed = self._observed_identity()
            if not observed:
                return ProbeResult(
                    authenticated=False,
                    error_category=ErrorCategory.AUTHENTICATION,
                    detail="the browser session did not expose an account identity",
                    next_action="Sign in and confirm the account identity is visible.",
                )
            return ProbeResult(authenticated=True, observed_identity=observed)
        except Exception as exc:
            error = normalize_adapter_error(exc)
            return ProbeResult(
                authenticated=False,
                error_category=error.category,
                detail=error.detail,
                next_action=error.next_action,
            )

    def validate(self, request: PublishRequest) -> ValidationResult:
        if request.platform.strip().lower() != self.platform:
            return ValidationResult(
                ok=False,
                error_category=ErrorCategory.CONFIGURATION,
                detail="publish request platform does not match the browser adapter",
                next_action="Route the request to its matching destination adapter.",
            )
        if not request.media_path.is_file():
            return ValidationResult(
                ok=False,
                error_category=ErrorCategory.VALIDATION,
                detail="media file does not exist",
                next_action="Build or select an existing media file before publishing.",
            )
        if request.media_path.stat().st_size <= 0:
            return ValidationResult(
                ok=False,
                error_category=ErrorCategory.VALIDATION,
                detail="media file is empty",
                next_action="Render a non-empty media artifact before publishing.",
            )
        caption = request.metadata.get("caption", request.metadata.get("description", ""))
        if not isinstance(caption, str) or not caption.strip():
            return ValidationResult(
                ok=False,
                error_category=ErrorCategory.VALIDATION,
                detail="publish metadata requires caption or description text",
                next_action="Generate and validate the platform metadata pack.",
            )
        scheduled_at = request.metadata.get("scheduled_at")
        if scheduled_at is not None:
            if not self._config.get("supports_schedule", False):
                return ValidationResult(
                    ok=False,
                    error_category=ErrorCategory.CONFIGURATION,
                    detail=f"{self.platform} browser publishing does not support scheduling",
                    next_action="Remove scheduled_at or use a scheduling-capable destination.",
                )
            try:
                self._schedule_fields(scheduled_at)
            except ValueError as exc:
                return ValidationResult(
                    ok=False,
                    error_category=ErrorCategory.VALIDATION,
                    detail=f"scheduled_at {exc}",
                    next_action="Provide a timezone-qualified ISO-8601 schedule timestamp.",
                )
        return ValidationResult(ok=True)

    def publish(self, request: PublishRequest) -> PublishResult:
        validation = self.validate(request)
        if not validation.ok:
            return self._blocked(validation)
        probe = self.probe_auth(request.account)
        if not probe.authenticated:
            return PublishResult(
                status=PublishStatus.BLOCKED,
                error_category=(
                    probe.error_category.value
                    if probe.error_category is not None
                    else ErrorCategory.AUTHENTICATION.value
                ),
                detail=probe.detail,
                evidence={"next_action": probe.next_action},
            )
        identity = probe_identity(
            request.account.expected_identity, probe.observed_identity
        )
        if not identity.ok:
            return self._blocked(identity, observed_identity=probe.observed_identity)

        if request.dry_run:
            required_controls = ("upload", "submit")
            controls = {purpose: False for purpose in required_controls}
            controls_enabled = {purpose: False for purpose in required_controls}
            try:
                for purpose in required_controls:
                    locator = self._locator(purpose)
                    controls[purpose] = bool(self.page.is_visible(locator))
                    controls_enabled[purpose] = bool(self.page.is_enabled(locator))
            except Exception as exc:
                return PublishResult(
                    status=PublishStatus.FAILED,
                    error_category=ErrorCategory.PLATFORM_UI.value,
                    evidence={
                        "dry_run": True,
                        "final_action_skipped": False,
                        "observed_identity": probe.observed_identity,
                        "media_validated": True,
                        "controls_ready": False,
                        "controls": controls,
                        "controls_enabled": controls_enabled,
                    },
                    detail=f"browser control readiness check failed: {type(exc).__name__}",
                )
            controls_ready = all(controls.values()) and all(controls_enabled.values())
            if not controls_ready:
                return PublishResult(
                    status=PublishStatus.BLOCKED,
                    error_category=ErrorCategory.PLATFORM_UI.value,
                    evidence={
                        "dry_run": True,
                        "final_action_skipped": False,
                        "observed_identity": probe.observed_identity,
                        "media_validated": True,
                        "controls_ready": False,
                        "controls": controls,
                        "controls_enabled": controls_enabled,
                        "next_action": (
                            "Open the configured composer and verify the upload "
                            "and final submit controls."
                        ),
                    },
                    detail=(
                        "required browser publishing controls are not visible and enabled"
                    ),
                )
            return PublishResult(
                status=PublishStatus.SKIPPED,
                evidence={
                    "dry_run": True,
                    "final_action_skipped": True,
                    "observed_identity": probe.observed_identity,
                    "media_validated": True,
                    "controls_ready": True,
                    "controls": controls,
                    "controls_enabled": controls_enabled,
                },
                detail="dry run completed without uploading or submitting",
            )

        submit_clicked = False
        try:
            schedule_requested = bool(request.metadata.get("scheduled_at"))
            self._run_phase(self._flow.open_upload)
            self.page.set_input_files(self._locator("upload"), request.media_path)
            self._run_phase(self._flow.after_upload)
            self._apply_details(request.metadata)
            self._run_phase(self._flow.after_details)
            self._apply_visibility(request.metadata)
            if schedule_requested:
                self._apply_schedule(request.metadata["scheduled_at"])
            previous_evidence = self._raw_evidence(
                schedule_requested=schedule_requested
            )
            submit_purpose = "schedule_submit" if schedule_requested else "submit"
            submit_clicked = True
            # Mark the boundary before invoking the browser. A click can reach
            # the platform even when the automation bridge raises afterward.
            self.page.click(self._locator(submit_purpose))
            return self._wait_for_confirmed_result(
                probe.observed_identity,
                previous_evidence,
                schedule_requested=schedule_requested,
            )
        except Exception as exc:
            if submit_clicked:
                return self._ambiguous_result(probe.observed_identity)
            return self._failure(exc)

    def _observed_identity(self) -> str:
        locator = self._locator("identity")
        for raw_rule in cast(list[dict[str, str]], self._config["identity_evidence"]):
            if raw_rule["source"] == "attribute":
                raw = self.page.attribute(locator, raw_rule["attribute"]).strip()
            else:
                raw = self.page.text(locator).strip()
            match = re.search(raw_rule["pattern"], raw, re.IGNORECASE)
            if not match:
                continue
            observed = match.group("identity").strip().lstrip("@").rstrip("/")
            if observed.casefold() not in _GENERIC_IDENTITIES:
                return observed
        return ""

    def _run_phase(self, operations: tuple[str, ...]) -> None:
        for purpose in operations:
            if purpose == "processing":
                self.page.wait_for(self._locator(purpose))
            else:
                self.page.click(self._locator(purpose))

    def _apply_details(self, metadata: Mapping[str, object]) -> None:
        caption = str(metadata.get("caption", metadata.get("description", "")))
        if self.platform == "youtube":
            self._fill_if_text("title", metadata.get("title"))
            self._fill_if_text("description", metadata.get("description"))
            audience = metadata.get("audience")
            audience_purpose = {
                "made_for_kids": "audience_made_for_kids",
                "not_made_for_kids": "audience_not_made_for_kids",
            }.get(audience)
            if audience_purpose and audience_purpose in self._locators:
                self.page.check(self._locator(audience_purpose), True)
            if isinstance(metadata.get("synthetic_media"), bool):
                self.page.check(
                    self._locator("synthetic_media"),
                    cast(bool, metadata["synthetic_media"]),
                )
        elif self.platform == "tiktok":
            self._fill_if_text("filename_slug", metadata.get("filename_slug"))
            self.page.fill(self._locator("caption"), caption)
            for key in ("allow_comments", "allow_duet", "allow_stitch"):
                if isinstance(metadata.get(key), bool):
                    self.page.check(self._locator(key), cast(bool, metadata[key]))
        else:
            self.page.fill(self._locator("caption"), caption)

    def _apply_visibility(self, metadata: Mapping[str, object]) -> None:
        if self.platform != "youtube":
            return
        visibility = metadata.get("visibility")
        if not isinstance(visibility, str) or not visibility.strip():
            return
        if not self.page.is_visible(self._locator("visibility")):
            raise BrowserUIError("visibility controls are unavailable: visibility")
        self.page.select_option(self._locator("visibility"), visibility.strip())

    def _apply_schedule(self, scheduled_at: object) -> None:
        if not self.page.is_visible(self._locator("schedule_mode")):
            raise BrowserUIError(
                "scheduling controls are unavailable: schedule_mode"
            )
        self.page.click(self._locator("schedule_mode"))
        remaining = ("schedule_date", "schedule_time", "schedule_submit")
        missing = [
            purpose
            for purpose in remaining
            if not self.page.is_visible(self._locator(purpose))
        ]
        if missing:
            raise BrowserUIError(
                "scheduling controls are unavailable: " + ", ".join(missing)
            )
        schedule_date, schedule_time = self._schedule_fields(scheduled_at)
        self.page.fill(self._locator("schedule_date"), schedule_date)
        self.page.fill(self._locator("schedule_time"), schedule_time)

    @staticmethod
    def _schedule_fields(value: object) -> tuple[str, str]:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must include an ISO-8601 date-time")
        if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", value.strip()):
            raise ValueError("must include an ISO-8601 date-time")
        normalized = value.strip().replace("Z", "+00:00")
        try:
            scheduled = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("must include an ISO-8601 date-time") from exc
        if scheduled.tzinfo is None or scheduled.utcoffset() is None:
            raise ValueError("must include a timezone offset")
        return scheduled.date().isoformat(), scheduled.strftime("%H:%M")

    def _fill_if_text(self, purpose: str, value: object) -> None:
        if purpose in self._locators and isinstance(value, str) and value.strip():
            self.page.fill(self._locator(purpose), value.strip())

    def _wait_for_confirmed_result(
        self,
        observed_identity: str,
        previous: Mapping[str, str],
        *,
        schedule_requested: bool,
    ) -> PublishResult:
        deadline = self._monotonic() + self._evidence_timeout
        while True:
            result = self._positive_result(
                observed_identity,
                previous,
                schedule_requested=schedule_requested,
            )
            if result is not None:
                return result
            now = self._monotonic()
            if now >= deadline:
                return self._ambiguous_result(observed_identity)
            self._sleep(min(self._evidence_poll_interval, deadline - now))

    def _positive_result(
        self,
        observed_identity: str,
        previous: Mapping[str, str],
        *,
        schedule_requested: bool,
    ) -> PublishResult | None:
        current = self._raw_evidence(schedule_requested=schedule_requested)
        fresh = {
            key: value
            for key, value in current.items()
            if value and value != previous.get(key, "")
        }
        confirmation_pattern = cast(
            str,
            self._config[
                "schedule_confirmation_pattern"
                if schedule_requested
                else "confirmation_pattern"
            ],
        )
        raw_confirmation = fresh.get("confirmation", "")
        confirmation = (
            raw_confirmation
            if re.search(confirmation_pattern, raw_confirmation, re.IGNORECASE)
            else ""
        )
        post_pattern = cast(
            str,
            self._config[
                "schedule_post_link_pattern"
                if schedule_requested
                else "post_link_pattern"
            ],
        )
        permalink = ""
        for key in ("permalink", "current_url"):
            candidate = fresh.get(key, "")
            if re.fullmatch(post_pattern, candidate, re.IGNORECASE):
                permalink = candidate
                break
        raw_platform_id = fresh.get("platform_id", "")
        id_pattern = cast(
            str,
            self._config[
                "schedule_platform_id_pattern"
                if schedule_requested
                else "platform_id_pattern"
            ],
        )
        platform_id = (
            raw_platform_id
            if re.fullmatch(
                id_pattern,
                raw_platform_id,
                re.IGNORECASE,
            )
            else ""
        )
        if not (confirmation or permalink or platform_id):
            return None

        lowered_confirmation = confirmation.casefold()
        is_submitted = any(
            marker in lowered_confirmation
            for marker in (
                "uploading",
                "being uploaded",
                "processing",
                "under review",
                "being published",
            )
        )
        evidence: dict[str, object] = {
            "observed_identity": observed_identity,
            "submit_clicked": True,
            "retry_safe": False,
        }
        if confirmation:
            evidence["confirmation"] = confirmation
        if permalink:
            evidence["permalink"] = permalink
        if platform_id:
            evidence["platform_id"] = platform_id
        return PublishResult(
            status=(
                PublishStatus.SCHEDULED
                if schedule_requested
                else PublishStatus.SUBMITTED
                if is_submitted
                or (
                    not confirmation
                    and self.platform in {"tiktok", "youtube", "facebook"}
                )
                else PublishStatus.PUBLISHED
            ),
            platform_id=platform_id,
            post_url=permalink,
            evidence=evidence,
        )

    def _raw_evidence(self, *, schedule_requested: bool) -> dict[str, str]:
        prefix = "schedule_" if schedule_requested else ""
        confirmation = ""
        confirmation_locator = self._locator(f"{prefix}confirmation")
        if self.page.is_visible(confirmation_locator):
            confirmation = self.page.text(confirmation_locator).strip()
        permalink = ""
        permalink_locator = self._locator(f"{prefix}permalink")
        if self.page.is_visible(permalink_locator):
            permalink = self.page.attribute(permalink_locator, "href").strip()
        platform_id = ""
        platform_id_locator = self._locator(f"{prefix}platform_id")
        if self.page.is_visible(platform_id_locator):
            platform_id = self.page.text(platform_id_locator).strip()
        return {
            "confirmation": confirmation,
            "permalink": permalink,
            "platform_id": platform_id,
            "current_url": (
                "" if schedule_requested else self.page.current_url().strip()
            ),
        }

    def _ambiguous_result(self, observed_identity: str) -> PublishResult:
        return PublishResult(
            status=PublishStatus.UNKNOWN,
            error_category=ErrorCategory.AMBIGUOUS_SUBMIT.value,
            detail="the final submit action was clicked without positive publication evidence",
            evidence={
                "submit_clicked": True,
                "retry_safe": False,
                "observed_identity": observed_identity,
            },
        )

    def _blocked(
        self, validation: ValidationResult, **evidence: object
    ) -> PublishResult:
        return PublishResult(
            status=PublishStatus.BLOCKED,
            error_category=(
                validation.error_category.value
                if validation.error_category is not None
                else ErrorCategory.VALIDATION.value
            ),
            detail=validation.detail,
            evidence={"next_action": validation.next_action, **evidence},
        )

    def _failure(self, exc: Exception) -> PublishResult:
        if isinstance(exc, BrowserDependencyError):
            category = ErrorCategory.DEPENDENCY
            detail = str(exc)
            next_action = "Install the browser dependency group and retry the probe."
            retry_safe = False
        elif isinstance(exc, BrowserUIError):
            category = ErrorCategory.PLATFORM_UI
            detail = str(exc)
            next_action = "Refresh the platform selector contract before retrying."
            retry_safe = False
        else:
            error = normalize_adapter_error(exc)
            category = error.category
            detail = error.detail
            next_action = error.next_action
            retry_safe = error.retryable
        return PublishResult(
            status=PublishStatus.FAILED,
            error_category=category.value,
            detail=detail,
            evidence={"next_action": next_action, "retry_safe": retry_safe},
        )

    def _locator(self, purpose: str) -> Locator:
        return self._locators[purpose]


def register_browser_adapters(
    registry: AdapterRegistry,
    page_factory: Callable[[str], BrowserPage],
    selectors: Mapping[str, object] | None = None,
) -> None:
    """Register one independently mockable browser adapter per destination."""

    selector_data = (
        _validate_selector_data(dict(selectors))
        if selectors is not None
        else load_browser_selectors()
    )
    for platform in SUPPORTED_PLATFORMS:
        registry.register(BrowserPublisher(platform, page_factory(platform), selector_data))


def register_managed_chrome_adapters(
    registry: AdapterRegistry,
    chrome: ManagedChrome,
    selectors: Mapping[str, object] | None = None,
    *,
    connector: Callable[[str], BrowserPage] | None = None,
) -> None:
    """Attach and register all six browser adapters against one managed CDP run."""

    existing = set(registry.names()).intersection(SUPPORTED_PLATFORMS)
    if existing:
        raise ValueError(
            "managed browser registry already contains adapters: "
            + ", ".join(sorted(existing))
        )
    selector_data = (
        _validate_selector_data(dict(selectors))
        if selectors is not None
        else load_browser_selectors()
    )
    if connector is None:
        connector = PlaywrightBrowserPage.connect
    pages: list[BrowserPage] = []
    try:
        for _platform in SUPPORTED_PLATFORMS:
            pages.append(connector(chrome.cdp_url))
    except BaseException as exc:
        _cleanup_failed_browser_setup(exc, chrome, tuple(pages))
        raise

    shared_runtime = _SharedManagedRuntime(chrome, len(pages))
    adapters = [
        BrowserPublisher(
            platform,
            page,
            selector_data,
            runtime_release=shared_runtime.release,
        )
        for platform, page in zip(SUPPORTED_PLATFORMS, pages, strict=True)
    ]
    for adapter in adapters:
        registry.register(adapter)
