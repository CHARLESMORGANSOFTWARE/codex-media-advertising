"""Best-effort, receipt-safe browser publishers for the six supported destinations.

The page object is deliberately a small protocol.  A Playwright/CDP bridge can
implement it without making these adapters depend on Playwright at import time,
and tests can exercise every publication outcome without a live account.
"""

from __future__ import annotations

import json
import re
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
_REQUIRED_LOCATORS = {
    "identity",
    "upload",
    "caption",
    "submit",
    "confirmation",
    "permalink",
    "platform_id",
}
_PLATFORM_REQUIRED_LOCATORS = {
    "instagram": {"create", "advance"},
    "tiktok": {
        "filename_slug",
        "allow_comments",
        "allow_duet",
        "allow_stitch",
        "schedule_mode",
        "schedule_date",
        "schedule_time",
        "schedule_submit",
    },
    "youtube": {
        "identity_menu",
        "create",
        "title",
        "description",
        "audience_made_for_kids",
        "audience_not_made_for_kids",
        "synthetic_media",
        "visibility",
        "schedule_mode",
        "schedule_date",
        "schedule_time",
        "schedule_submit",
    },
    "x": {"processing"},
    "facebook": {
        "schedule_mode",
        "schedule_date",
        "schedule_time",
        "schedule_submit",
    },
    "threads": {"create"},
}
_GENERIC_LOGGED_OUT = re.compile(r"\b(?:log\s*in|sign\s*in)\b", re.IGNORECASE)
_GENERIC_IDENTITIES = {
    "account",
    "account menu",
    "profile",
    "your account",
    "your profile",
}
_PLATFORM_FIELDS = {
    "url",
    "login_markers",
    "post_link_pattern",
    "supports_schedule",
    "identity_evidence",
    "confirmation_pattern",
    "platform_id_pattern",
    "schedule_confirmation_pattern",
    "before_probe",
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
        _validate_pattern(platform, "post_link_pattern", pattern)
        _validate_pattern(
            platform, "confirmation_pattern", raw_config.get("confirmation_pattern")
        )
        _validate_pattern(
            platform, "platform_id_pattern", raw_config.get("platform_id_pattern")
        )
        if not isinstance(raw_config.get("supports_schedule"), bool):
            raise ValueError(f"{platform} supports_schedule must be boolean")
        if raw_config["supports_schedule"]:
            _validate_pattern(
                platform,
                "schedule_confirmation_pattern",
                raw_config.get("schedule_confirmation_pattern"),
            )
        elif raw_config.get("schedule_confirmation_pattern") is not None:
            raise ValueError(
                f"{platform} schedule confirmation is only valid when scheduling is supported"
            )
        locators = raw_config.get("locators")
        required_locators = _REQUIRED_LOCATORS | _PLATFORM_REQUIRED_LOCATORS[platform]
        if not isinstance(locators, dict) or not required_locators <= set(locators):
            missing = sorted(required_locators - set(locators or {}))
            raise ValueError(f"{platform} selectors are missing locators: {', '.join(missing)}")
        for purpose, raw_locator in locators.items():
            _validate_locator(platform, str(purpose), raw_locator)
        for key in ("before_probe", "before_upload", "after_upload"):
            sequence = raw_config.get(key, [])
            if not isinstance(sequence, list) or not all(
                isinstance(name, str) and name in locators for name in sequence
            ):
                raise ValueError(f"{platform} {key} must reference known locators")
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
        connector: Callable[[str], BrowserPage] | None = None,
        selectors: Mapping[str, object] | None = None,
    ) -> BrowserPublisher:
        """Connect a page abstraction to Task 6's isolated loopback CDP runtime."""

        if connector is None:
            connector = PlaywrightBrowserPage.connect
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
            for purpose in cast(list[str], self._config.get("before_probe", [])):
                self.page.click(self._locator(purpose))
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
            except ValueError:
                return ValidationResult(
                    ok=False,
                    error_category=ErrorCategory.VALIDATION,
                    detail="scheduled_at must be an ISO-8601 date-time",
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
            schedule_requested = bool(request.metadata.get("scheduled_at"))
            if schedule_requested:
                self._require_schedule_controls()
            for purpose in cast(list[str], self._config.get("before_upload", [])):
                self.page.click(self._locator(purpose))
            self.page.set_input_files(self._locator("upload"), request.media_path)
            for purpose in cast(list[str], self._config.get("after_upload", [])):
                if purpose == "processing":
                    self.page.wait_for(self._locator(purpose))
                else:
                    self.page.click(self._locator(purpose))
            self._apply_metadata(request.metadata)
            previous_evidence = self._raw_evidence()
            submit_purpose = "schedule_submit" if schedule_requested else "submit"
            submit_clicked = True
            # Mark the boundary before invoking the browser. A click can reach
            # the platform even when the automation bridge raises afterward.
            self.page.click(self._locator(submit_purpose))
            return self._confirmed_result(
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

    def _apply_metadata(self, metadata: Mapping[str, object]) -> None:
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
            self._select_if_text("visibility", metadata.get("visibility"))
        elif self.platform == "tiktok":
            self._fill_if_text("filename_slug", metadata.get("filename_slug"))
            self.page.fill(self._locator("caption"), caption)
            for key in ("allow_comments", "allow_duet", "allow_stitch"):
                if isinstance(metadata.get(key), bool):
                    self.page.check(self._locator(key), cast(bool, metadata[key]))
        else:
            self.page.fill(self._locator("caption"), caption)
        if metadata.get("scheduled_at"):
            schedule_date, schedule_time = self._schedule_fields(
                metadata["scheduled_at"]
            )
            self.page.click(self._locator("schedule_mode"))
            self.page.fill(self._locator("schedule_date"), schedule_date)
            self.page.fill(self._locator("schedule_time"), schedule_time)

    def _require_schedule_controls(self) -> None:
        required = (
            "schedule_mode",
            "schedule_date",
            "schedule_time",
            "schedule_submit",
        )
        missing = [
            purpose
            for purpose in required
            if purpose not in self._locators
            or not self.page.is_visible(self._locator(purpose))
        ]
        if missing:
            raise BrowserUIError(
                "scheduling controls are unavailable: " + ", ".join(missing)
            )

    @staticmethod
    def _schedule_fields(value: object) -> tuple[str, str]:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("scheduled_at is not text")
        normalized = value.strip().replace("Z", "+00:00")
        scheduled = datetime.fromisoformat(normalized)
        return scheduled.date().isoformat(), scheduled.strftime("%H:%M")

    def _fill_if_text(self, purpose: str, value: object) -> None:
        if purpose in self._locators and isinstance(value, str) and value.strip():
            self.page.fill(self._locator(purpose), value.strip())

    def _select_if_text(self, purpose: str, value: object) -> None:
        if purpose in self._locators and isinstance(value, str) and value.strip():
            self.page.select_option(self._locator(purpose), value.strip())

    def _confirmed_result(
        self,
        observed_identity: str,
        previous: Mapping[str, str],
        *,
        schedule_requested: bool,
    ) -> PublishResult:
        current = self._raw_evidence()
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
        post_pattern = cast(str, self._config["post_link_pattern"])
        permalink = ""
        for key in ("permalink", "current_url"):
            candidate = fresh.get(key, "")
            if re.fullmatch(post_pattern, candidate, re.IGNORECASE):
                permalink = candidate
                break
        raw_platform_id = fresh.get("platform_id", "")
        platform_id = (
            raw_platform_id
            if re.fullmatch(
                cast(str, self._config["platform_id_pattern"]),
                raw_platform_id,
                re.IGNORECASE,
            )
            else ""
        )
        # A created URL or object ID does not prove the requested time was
        # selected. Scheduling succeeds only on fresh schedule-specific text.
        if schedule_requested and not confirmation:
            return self._ambiguous_result(observed_identity)
        if not (confirmation or permalink or platform_id):
            return self._ambiguous_result(observed_identity)

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

    def _raw_evidence(self) -> dict[str, str]:
        confirmation = ""
        confirmation_locator = self._locator("confirmation")
        if self.page.is_visible(confirmation_locator):
            confirmation = self.page.text(confirmation_locator).strip()
        permalink = ""
        permalink_locator = self._locator("permalink")
        if self.page.is_visible(permalink_locator):
            permalink = self.page.attribute(permalink_locator, "href").strip()
        platform_id = ""
        platform_id_locator = self._locator("platform_id")
        if self.page.is_visible(platform_id_locator):
            platform_id = self.page.text(platform_id_locator).strip()
        return {
            "confirmation": confirmation,
            "permalink": permalink,
            "platform_id": platform_id,
            "current_url": self.page.current_url().strip(),
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

    selector_data = (
        _validate_selector_data(dict(selectors))
        if selectors is not None
        else load_browser_selectors()
    )
    for platform in SUPPORTED_PLATFORMS:
        registry.register(
            BrowserPublisher.from_managed_chrome(
                platform,
                chrome,
                connector,
                selector_data,
            )
        )
