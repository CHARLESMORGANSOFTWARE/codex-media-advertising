from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from ..models import (
    AccountConfig,
    Destination,
    PublishRequest,
    PublishResult,
    PublishStatus,
)


SUPPORTED_PLATFORMS: tuple[Destination, ...] = (
    "instagram",
    "tiktok",
    "youtube",
    "x",
    "facebook",
    "threads",
)


class ErrorCategory(StrEnum):
    CONFIGURATION = "configuration"
    DEPENDENCY = "dependency"
    AUTHENTICATION = "authentication"
    IDENTITY_MISMATCH = "identity_mismatch"
    VALIDATION = "validation"
    RIGHTS = "rights"
    RENDER = "render"
    NETWORK = "network"
    PLATFORM_UI = "platform_ui"
    RATE_LIMIT = "rate_limit"
    AMBIGUOUS_SUBMIT = "ambiguous_submit"
    INTERNAL = "internal"


class ProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authenticated: bool
    observed_identity: str = ""
    error_category: ErrorCategory | None = None
    detail: str = ""
    next_action: str = ""


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    error_category: ErrorCategory | None = None
    detail: str = ""
    next_action: str = ""
    retryable: bool = False


class AdapterError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: ErrorCategory
    detail: str
    next_action: str
    retryable: bool = False


@runtime_checkable
class PublisherAdapter(Protocol):
    platform: str

    def probe_auth(self, account: AccountConfig) -> ProbeResult: ...

    def validate(self, request: PublishRequest) -> ValidationResult: ...

    def publish(self, request: PublishRequest) -> PublishResult: ...


class AdapterRegistry:
    """Destination adapter registry with an explicit completeness gate."""

    def __init__(self) -> None:
        self._adapters: dict[str, PublisherAdapter] = {}

    def register(self, adapter: PublisherAdapter) -> None:
        name = str(adapter.platform).strip().lower()
        if name not in SUPPORTED_PLATFORMS:
            raise ValueError(f"unsupported platform: {name or '<empty>'}")
        if name in self._adapters:
            raise ValueError(f"adapter already registered: {name}")
        self._adapters[name] = adapter

    def get(self, platform: str) -> PublisherAdapter:
        name = platform.strip().lower()
        try:
            return self._adapters[name]
        except KeyError as exc:
            raise KeyError(f"unregistered platform: {name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def publish(self, request: PublishRequest) -> PublishResult:
        adapter = self.get(request.platform)
        try:
            validation = adapter.validate(request)
        except Exception as exc:
            error = normalize_adapter_error(exc)
            return PublishResult(
                status=PublishStatus.BLOCKED,
                error_category=error.category.value,
                detail=error.detail,
                evidence={"next_action": error.next_action},
            )
        if not validation.ok:
            return PublishResult(
                status=PublishStatus.BLOCKED,
                error_category=(
                    validation.error_category.value
                    if validation.error_category is not None
                    else ErrorCategory.VALIDATION.value
                ),
                detail=validation.detail,
                evidence={"next_action": validation.next_action},
            )
        try:
            return adapter.publish(request)
        except Exception as exc:
            error = normalize_adapter_error(exc)
            return PublishResult(
                status=PublishStatus.FAILED,
                error_category=error.category.value,
                detail=error.detail,
                evidence={"next_action": error.next_action},
            )

    def require_complete(self) -> None:
        missing = sorted(set(SUPPORTED_PLATFORMS) - self._adapters.keys())
        extra = sorted(self._adapters.keys() - set(SUPPORTED_PLATFORMS))
        if missing or extra:
            parts = []
            if missing:
                parts.append(f"missing adapters: {', '.join(missing)}")
            if extra:
                parts.append(f"unsupported adapters: {', '.join(extra)}")
            raise ValueError("; ".join(parts))


def _normalized_identity(value: str) -> str:
    return value.strip().casefold()


def probe_identity(expected_identity: str, observed_identity: str) -> ValidationResult:
    """Require an exact normalized account identity; never infer or switch accounts."""

    if not expected_identity.strip():
        return ValidationResult(
            ok=False,
            error_category=ErrorCategory.CONFIGURATION,
            detail="expected account identity is not configured",
            next_action="Configure the expected identity before publishing.",
        )
    if not observed_identity.strip():
        return ValidationResult(
            ok=False,
            error_category=ErrorCategory.AUTHENTICATION,
            detail="the platform did not expose an authenticated account identity",
            next_action="Sign in to the configured account and run the identity probe again.",
        )
    if _normalized_identity(expected_identity) != _normalized_identity(observed_identity):
        return ValidationResult(
            ok=False,
            error_category=ErrorCategory.IDENTITY_MISMATCH,
            detail="authenticated account identity does not match the configured identity",
            next_action="Sign in to the configured account; the plugin will not switch accounts.",
        )
    return ValidationResult(ok=True)


_AUTHORIZATION_HEADER = re.compile(
    r"(?im)(\bauthorization\b\s*[:=]\s*)[^\r\n]+"
)
_AUTHORIZATION_SPACE = re.compile(
    r"(?im)(\bauthorization\b\s+)(?![:=])[^\r\n]+"
)
_COOKIE_HEADER = re.compile(
    r"(?im)(\b(?:set-cookie|cookie)\b\s*[:=]\s*)[^\r\n]+"
)
_COOKIE_JSON_CONTAINER = re.compile(
    r"(?is)([\"']?cookies[\"']?\s*[:=]\s*)(?:\[[^\]]*\]|\{[^}]*\})"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)([\"']?(?:access[_-]?token|refresh[_-]?token|id[_-]?token|token|secret|"
    r"password|cookie|api[_-]?key|oauth[_-]?code)[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s&,;}]+)"
)
_BEARER_VALUE = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")


def redact_diagnostic(value: str) -> str:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    else:
        value = json.dumps(_redact_json_value(parsed), ensure_ascii=False)
    redacted = _AUTHORIZATION_HEADER.sub(
        lambda match: f"{match.group(1)}[REDACTED]", value
    )
    redacted = _AUTHORIZATION_SPACE.sub(
        lambda match: f"{match.group(1)}[REDACTED]", redacted
    )
    redacted = _COOKIE_HEADER.sub(
        lambda match: f"{match.group(1)}[REDACTED]", redacted
    )
    redacted = _COOKIE_JSON_CONTAINER.sub(
        lambda match: f"{match.group(1)}[REDACTED]", redacted
    )
    redacted = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}[REDACTED]", redacted
    )
    redacted = _BEARER_VALUE.sub(
        lambda match: f"{match.group(1)}[REDACTED]", redacted
    )
    return redacted[:1000]


def _redact_json_value(value: object) -> object:
    if isinstance(value, dict):
        cookie_record = _looks_like_cookie_record(value)
        output: dict[str, object] = {}
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if (cookie_record and normalized in {"value", "encrypted_value"}) or any(
                part in normalized
                for part in (
                    "cookie",
                    "token",
                    "secret",
                    "password",
                    "authorization",
                    "api_key",
                    "oauth_code",
                )
            ):
                output[str(key)] = "[REDACTED]"
            else:
                output[str(key)] = _redact_json_value(item)
        return output
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    return value


def _looks_like_cookie_record(value: dict[object, object]) -> bool:
    normalized = {
        str(key).casefold().replace("-", "_"): item for key, item in value.items()
    }
    if "name" not in normalized or "value" not in normalized:
        return False
    cookie_attributes = {
        "expires",
        "expiry",
        "http_only",
        "httponly",
        "secure",
        "same_site",
        "samesite",
        "host_only",
        "hostonly",
        "session",
    }
    if cookie_attributes.intersection(normalized):
        return True
    domain = normalized.get("domain")
    if "path" in normalized and isinstance(domain, str):
        return True
    raw_name = str(normalized["name"])
    delimiter_name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", raw_name)
    name = delimiter_name.casefold()
    segments = set(re.findall(r"[a-z0-9]+", name))
    return (
        name
        in {
            "sid",
            "session",
            "sessionid",
            "phpsessid",
            "jsessionid",
            "csrftoken",
            "xsrftoken",
            "authtoken",
            "authorization",
            "authentication",
        }
        or name.startswith(("__host-", "__secure-", "_ga", "_gid"))
        or bool(
            segments.intersection(
                {"sid", "session", "sessionid", "csrf", "xsrf", "auth", "token", "cookie"}
            )
        )
    )


def normalize_adapter_error(exc: BaseException) -> AdapterError:
    """Translate implementation exceptions to the public, redacted error contract."""

    if isinstance(exc, (TimeoutError, ConnectionError)):
        category = ErrorCategory.NETWORK
        action = "Check connectivity and retry once if no submit signal was observed."
        retryable = True
    elif isinstance(exc, PermissionError):
        category = ErrorCategory.AUTHENTICATION
        action = "Reconnect the configured account and rerun its authentication probe."
        retryable = False
    elif isinstance(exc, (ValueError, TypeError, FileNotFoundError)):
        category = ErrorCategory.VALIDATION
        action = "Correct the request and validate it again before publishing."
        retryable = False
    else:
        category = ErrorCategory.INTERNAL
        action = "Inspect the adapter diagnostic log before attempting another live run."
        retryable = False
    detail = redact_diagnostic(str(exc) or exc.__class__.__name__)
    return AdapterError(
        category=category,
        detail=detail,
        next_action=action,
        retryable=retryable,
    )
