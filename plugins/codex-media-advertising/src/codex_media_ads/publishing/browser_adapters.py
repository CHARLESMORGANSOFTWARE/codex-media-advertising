"""Best-effort, receipt-safe browser publishers for the six supported destinations.

The page object is deliberately a small protocol.  A Playwright/CDP bridge can
implement it without making these adapters depend on Playwright at import time,
and tests can exercise every publication outcome without a live account.
"""

from __future__ import annotations

import json
import re
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
    def click(self, locator: Locator) -> None: ...
    def fill(self, locator: Locator, value: str) -> None: ...
    def set_input_files(self, locator: Locator, path: Path) -> None: ...
    def select_option(self, locator: Locator, value: str) -> None: ...
    def check(self, locator: Locator, checked: bool) -> None: ...
    def wait_for(self, locator: Locator) -> None: ...


_SELECTOR_RESOURCE = "browser_selectors.v1.json"
_SEMANTIC_KINDS = {"role", "text", "label"}
_CSS_PURPOSES = {"upload", "stable_control"}
_REQUIRED_LOCATORS = {
    "identity",
    "upload",
    "caption",
    "submit",
    "confirmation",
    "permalink",
    "platform_id",
}
_GENERIC_LOGGED_OUT = re.compile(r"\b(?:log\s*in|sign\s*in)\b", re.IGNORECASE)
_PLATFORM_FIELDS = {
    "url",
    "login_markers",
    "post_link_pattern",
    "supports_schedule",
    "before_upload",
    "after_upload",
    "locators",
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
        unknown_fields = set(raw_config) - _PLATFORM_FIELDS
        if unknown_fields:
            raise ValueError(
                f"{platform} selector configuration has unknown fields: "
                f"{', '.join(sorted(unknown_fields))}"
            )
        if not _valid_https_url(raw_config.get("url")):
            raise ValueError(f"{platform} browser URL must be HTTPS")
        markers = raw_config.get("login_markers")
        if not isinstance(markers, list) or not markers or not all(
            isinstance(marker, str) and marker.strip() for marker in markers
        ):
            raise ValueError(f"{platform} login markers must be non-empty strings")
        pattern = raw_config.get("post_link_pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(f"{platform} post link pattern is required")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"{platform} post link pattern is invalid") from exc
        if not isinstance(raw_config.get("supports_schedule"), bool):
            raise ValueError(f"{platform} supports_schedule must be boolean")
        locators = raw_config.get("locators")
        if not isinstance(locators, dict) or not _REQUIRED_LOCATORS <= set(locators):
            missing = sorted(_REQUIRED_LOCATORS - set(locators or {}))
            raise ValueError(f"{platform} selectors are missing locators: {', '.join(missing)}")
        for purpose, raw_locator in locators.items():
            _validate_locator(platform, str(purpose), raw_locator)
        for key in ("before_upload", "after_upload"):
            sequence = raw_config.get(key, [])
            if not isinstance(sequence, list) or not all(
                isinstance(name, str) and name in locators for name in sequence
            ):
                raise ValueError(f"{platform} {key} must reference known locators")
    return cast(dict[str, object], data)


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


class BrowserPublisher:
    """Data-driven browser adapter with conservative publication evidence rules."""

    def __init__(
        self,
        platform: str,
        page: BrowserPage,
        selectors: Mapping[str, object] | None = None,
        *,
        managed_chrome: ManagedChrome | None = None,
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
        self._managed_chrome = managed_chrome
        self._config = cast(
            dict[str, object],
            cast(dict[str, object], selector_data["platforms"])[normalized],
        )
        self._locators = cast(dict[str, Locator], self._config["locators"])

    @classmethod
    def from_managed_chrome(
        cls,
        platform: str,
        chrome: ManagedChrome,
        connector: Callable[[str], BrowserPage],
        selectors: Mapping[str, object] | None = None,
    ) -> BrowserPublisher:
        """Connect a page abstraction to Task 6's isolated loopback CDP runtime."""

        page = connector(chrome.cdp_url)
        return cls(
            platform,
            page,
            selectors,
            managed_chrome=chrome,
        )

    def close(self) -> None:
        """Release the page bridge and only this adapter's managed Chrome clone."""

        try:
            close_page = getattr(self.page, "close", None)
            if callable(close_page):
                close_page()
        finally:
            if self._managed_chrome is not None:
                self._managed_chrome.close()

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
            observed = self.page.text(self._locator("identity")).strip()
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
            try:
                controls = {
                    purpose: self.page.is_visible(locator)
                    for purpose, locator in self._locators.items()
                    if purpose in {"upload", "submit"}
                }
            except Exception as exc:
                return self._failure(exc)
            return PublishResult(
                status=PublishStatus.SKIPPED,
                evidence={
                    "dry_run": True,
                    "observed_identity": probe.observed_identity,
                    "media_validated": True,
                    "controls": controls,
                },
                detail="dry run completed without uploading or submitting",
            )

        submit_clicked = False
        try:
            for purpose in cast(list[str], self._config.get("before_upload", [])):
                self.page.click(self._locator(purpose))
            self.page.set_input_files(self._locator("upload"), request.media_path)
            for purpose in cast(list[str], self._config.get("after_upload", [])):
                if purpose == "processing":
                    self.page.wait_for(self._locator(purpose))
                else:
                    self.page.click(self._locator(purpose))
            self._apply_metadata(request.metadata)
            submit_clicked = True
            # Mark the boundary before invoking the browser. A click can reach
            # the platform even when the automation bridge raises afterward.
            self.page.click(self._locator("submit"))
            return self._confirmed_result(request, probe.observed_identity)
        except Exception as exc:
            if submit_clicked:
                return self._ambiguous_result(probe.observed_identity)
            return self._failure(exc)

    def _apply_metadata(self, metadata: Mapping[str, object]) -> None:
        caption = str(metadata.get("caption", metadata.get("description", "")))
        self.page.fill(self._locator("caption"), caption)
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
            self._select_if_text("visibility", metadata.get("visibility"))
            self._fill_if_text("scheduled_at", metadata.get("scheduled_at"))
        elif self.platform == "tiktok":
            self._fill_if_text("filename_slug", metadata.get("title"))
            for key in ("allow_comments", "allow_duet", "allow_stitch"):
                if isinstance(metadata.get(key), bool):
                    self.page.check(self._locator(key), cast(bool, metadata[key]))
            self._fill_if_text("scheduled_at", metadata.get("scheduled_at"))
        elif self.platform == "facebook":
            self._fill_if_text("scheduled_at", metadata.get("scheduled_at"))

    def _fill_if_text(self, purpose: str, value: object) -> None:
        if purpose in self._locators and isinstance(value, str) and value.strip():
            self.page.fill(self._locator(purpose), value.strip())

    def _select_if_text(self, purpose: str, value: object) -> None:
        if purpose in self._locators and isinstance(value, str) and value.strip():
            self.page.select_option(self._locator(purpose), value.strip())

    def _confirmed_result(
        self, request: PublishRequest, observed_identity: str
    ) -> PublishResult:
        confirmation = ""
        permalink = ""
        platform_id = ""
        confirmation_locator = self._locator("confirmation")
        if self.page.is_visible(confirmation_locator):
            confirmation = self.page.text(confirmation_locator).strip()
        permalink_locator = self._locator("permalink")
        if self.page.is_visible(permalink_locator):
            candidate = self.page.attribute(permalink_locator, "href").strip()
            pattern = cast(str, self._config["post_link_pattern"])
            if re.fullmatch(pattern, candidate):
                permalink = candidate
        if not permalink:
            candidate = self.page.current_url().strip()
            pattern = cast(str, self._config["post_link_pattern"])
            if re.fullmatch(pattern, candidate):
                permalink = candidate
        platform_id_locator = self._locator("platform_id")
        if self.page.is_visible(platform_id_locator):
            platform_id = self.page.text(platform_id_locator).strip()
        if not (confirmation or permalink or platform_id):
            return self._ambiguous_result(observed_identity)

        lowered_confirmation = confirmation.casefold()
        schedule_requested = bool(self._config.get("supports_schedule", False)) and bool(
            request.metadata.get("scheduled_at")
        )
        is_scheduled = schedule_requested and "schedul" in lowered_confirmation
        is_submitted = any(
            marker in lowered_confirmation
            for marker in ("uploading", "processing", "under review", "being published")
        )
        explicit_live = any(
            marker in lowered_confirmation
            for marker in ("published", "shared", "posted", "was sent")
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
                if is_scheduled
                else PublishStatus.SUBMITTED
                if is_submitted or (schedule_requested and not explicit_live)
                else PublishStatus.PUBLISHED
            ),
            platform_id=platform_id,
            post_url=permalink,
            evidence=evidence,
        )

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
        error = normalize_adapter_error(exc)
        return PublishResult(
            status=PublishStatus.FAILED,
            error_category=error.category.value,
            detail=error.detail,
            evidence={"next_action": error.next_action, "retry_safe": error.retryable},
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
