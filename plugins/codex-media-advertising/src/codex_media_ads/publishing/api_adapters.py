from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Callable, Mapping, Protocol
from urllib.parse import parse_qsl, quote, urlsplit

import requests

from ..models import AccountConfig, PublishRequest, PublishResult, PublishStatus
from .base import (
    ErrorCategory,
    ProbeResult,
    ValidationResult,
    probe_identity,
    redact_diagnostic,
)


YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


class HttpTransport(Protocol):
    def request(self, method: str, url: str, **kwargs: object) -> object: ...


class _ApiFailure(RuntimeError):
    def __init__(self, detail: str, *, category: ErrorCategory = ErrorCategory.NETWORK):
        super().__init__(redact_diagnostic(detail))
        self.category = category


class _AmbiguousSubmit(RuntimeError):
    pass


def _is_connection_loss(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ),
    ):
        return True
    phase = str(getattr(exc, "phase", "")).strip().casefold().replace("-", "_")
    if phase in {"response", "response_body", "response_stream", "body_read"}:
        return True
    # Transport implementations do not need to depend on requests merely to
    # communicate that the request was sent and the response body was lost.
    name = exc.__class__.__name__.casefold()
    return any(
        marker in name
        for marker in ("chunkedencoding", "incompleteread", "responsebodylost")
    )


def _is_response_phase_loss(exc: BaseException) -> bool:
    phase = str(getattr(exc, "phase", "")).strip().casefold().replace("-", "_")
    name = exc.__class__.__name__.casefold()
    return phase in {
        "response",
        "response_body",
        "response_stream",
        "body_read",
    } or any(
        marker in name
        for marker in ("chunkedencoding", "incompleteread", "responsebodylost")
    )


def _read_secret(account: AccountConfig) -> dict[str, object]:
    path = account.secret_file
    if path is None:
        raise _ApiFailure(
            "an API secret file is not configured",
            category=ErrorCategory.CONFIGURATION,
        )
    path = Path(
        os.path.abspath(
            os.path.normpath(os.fspath(Path(path).expanduser()))
        )
    )
    for parent in reversed(path.parents):
        try:
            parent_info = parent.lstat()
        except OSError as exc:
            raise _ApiFailure(
                f"cannot inspect the configured secret path: {exc}",
                category=ErrorCategory.CONFIGURATION,
            ) from exc
        if stat.S_ISLNK(parent_info.st_mode):
            raise _ApiFailure(
                "the API secret path must not contain symlink directories",
                category=ErrorCategory.CONFIGURATION,
            )
        if not stat.S_ISDIR(parent_info.st_mode):
            raise _ApiFailure(
                "the API secret path must contain only regular directories",
                category=ErrorCategory.CONFIGURATION,
            )
    try:
        info = path.lstat()
    except OSError as exc:
        raise _ApiFailure(
            f"cannot read the configured secret file: {exc}",
            category=ErrorCategory.CONFIGURATION,
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _ApiFailure(
            "the API secret file must be a regular file, not a symlink",
            category=ErrorCategory.CONFIGURATION,
        )
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise _ApiFailure(
            "the API secret file must be owner-only (mode 0600)",
            category=ErrorCategory.CONFIGURATION,
        )
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise _ApiFailure(
            "the API secret file must be owned by the current user",
            category=ErrorCategory.CONFIGURATION,
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _ApiFailure(
            f"the API secret file is not valid JSON: {exc}",
            category=ErrorCategory.CONFIGURATION,
        ) from exc
    if not isinstance(value, dict):
        raise _ApiFailure(
            "the API secret file must contain a JSON object",
            category=ErrorCategory.CONFIGURATION,
        )
    token = value.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise _ApiFailure(
            "the API secret file does not contain an access token",
            category=ErrorCategory.CONFIGURATION,
        )
    return value


def _response_json(
    response: object,
    operation: str,
    *,
    ambiguous_success: bool = False,
) -> dict[str, object]:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if not 200 <= status_code < 300:
        # Response bodies are useful during development, but must pass through the
        # shared diagnostic redactor before becoming a public result.
        detail = str(getattr(response, "text", ""))
        if status_code in {401, 403}:
            category = ErrorCategory.AUTHENTICATION
        elif status_code == 429:
            category = ErrorCategory.RATE_LIMIT
        elif 400 <= status_code < 500:
            category = ErrorCategory.VALIDATION
        else:
            category = ErrorCategory.NETWORK
        raise _ApiFailure(
            f"{operation} failed with HTTP {status_code}: {detail}",
            category=category,
        )
    if status_code == 204:
        if ambiguous_success:
            raise _AmbiguousSubmit(
                f"{operation} returned success without publication evidence; reconcile before retrying"
            )
        return {}
    try:
        value = response.json()  # type: ignore[attr-defined]
    except Exception as exc:
        if ambiguous_success:
            raise _AmbiguousSubmit(
                f"{operation} returned success but its response body was unreadable; reconcile before retrying"
            ) from exc
        if not str(getattr(response, "text", "")):
            return {}
        raise _ApiFailure(f"{operation} returned invalid JSON") from exc
    if not isinstance(value, dict):
        if ambiguous_success:
            raise _AmbiguousSubmit(
                f"{operation} returned success with malformed publication evidence; reconcile before retrying"
            )
        raise _ApiFailure(f"{operation} returned an invalid response object")
    return value


def _request_json(
    transport: HttpTransport,
    method: str,
    url: str,
    operation: str,
    **kwargs: object,
) -> dict[str, object]:
    try:
        response = transport.request(method, url, timeout=30, **kwargs)
    except _ApiFailure:
        raise
    except Exception as exc:
        raise _ApiFailure(f"{operation} request failed: {exc}") from exc
    return _response_json(response, operation)


def _blocked(probe: ProbeResult) -> PublishResult:
    return PublishResult(
        status=PublishStatus.BLOCKED,
        error_category=(
            probe.error_category.value
            if probe.error_category is not None
            else ErrorCategory.AUTHENTICATION.value
        ),
        detail=probe.detail,
        evidence={
            "observed_identity": probe.observed_identity,
            "upload_started": False,
            "retry_safe": True,
        },
    )


def _failed(exc: BaseException, *, upload_started: bool) -> PublishResult:
    category = exc.category if isinstance(exc, _ApiFailure) else ErrorCategory.INTERNAL
    return PublishResult(
        status=PublishStatus.FAILED,
        error_category=category.value,
        detail=redact_diagnostic(str(exc) or exc.__class__.__name__),
        evidence={
            "upload_started": upload_started,
            # Once a remote container/session exists, an automatic browser retry
            # could duplicate publication. The orchestrator must reconcile first.
            "retry_safe": not upload_started,
        },
    )


def _unknown(exc: BaseException) -> PublishResult:
    return PublishResult(
        status=PublishStatus.UNKNOWN,
        error_category=ErrorCategory.AMBIGUOUS_SUBMIT.value,
        detail=redact_diagnostic(str(exc) or "final submit response was not received"),
        evidence={"upload_started": True, "retry_safe": False},
    )


def _final_request_json(
    transport: HttpTransport,
    method: str,
    url: str,
    operation: str,
    **kwargs: object,
) -> dict[str, object]:
    try:
        response = transport.request(method, url, timeout=30, **kwargs)
    except Exception as exc:
        if _is_connection_loss(exc):
            raise _AmbiguousSubmit(
                f"{operation} may have reached the platform; reconcile before retrying: {exc}"
            ) from exc
        raise _ApiFailure(f"{operation} request failed: {exc}") from exc
    try:
        return _response_json(response, operation, ambiguous_success=True)
    except (_ApiFailure, _AmbiguousSubmit):
        raise
    except Exception as exc:
        raise _AmbiguousSubmit(
            f"{operation} response could not be read; reconcile before retrying"
        ) from exc


def _oauth_encode(value: object) -> str:
    return quote(str(value), safe="~-._")


def _oauth1_authorization(
    *,
    method: str,
    url: str,
    consumer_key: str,
    consumer_secret: str,
    access_token: str,
    access_token_secret: str,
    nonce: str,
    timestamp: int,
    query: Mapping[str, object] | None = None,
    form: Mapping[str, object] | None = None,
) -> str:
    """Build an RFC 5849 HMAC-SHA1 user-context Authorization header."""

    parts = urlsplit(url)
    base_url = f"{parts.scheme}://{parts.netloc}{parts.path}"
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(timestamp),
        "oauth_token": access_token,
        "oauth_version": "1.0",
    }
    parameters: list[tuple[str, str]] = [
        (str(key), str(value)) for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    for source in (query or {}, form or {}, oauth):
        for key, value in source.items():
            if isinstance(value, (list, tuple)):
                parameters.extend((str(key), str(item)) for item in value)
            else:
                parameters.append((str(key), str(value)))
    encoded_parameters = sorted(
        (_oauth_encode(key), _oauth_encode(value)) for key, value in parameters
    )
    normalized = "&".join(
        f"{key}={value}" for key, value in encoded_parameters
    )
    signature_base = "&".join(
        (_oauth_encode(method.upper()), _oauth_encode(base_url), _oauth_encode(normalized))
    )
    signing_key = f"{_oauth_encode(consumer_secret)}&{_oauth_encode(access_token_secret)}"
    signature = base64.b64encode(
        hmac.new(
            signing_key.encode("ascii"),
            signature_base.encode("ascii"),
            hashlib.sha1,
        ).digest()
    ).decode("ascii")
    oauth["oauth_signature"] = signature
    return "OAuth " + ", ".join(
        f'{_oauth_encode(key)}="{_oauth_encode(value)}"'
        for key, value in sorted(oauth.items())
    )


class _PublisherBase:
    platform: str

    def __init__(self, transport: HttpTransport | None = None) -> None:
        self.transport = transport or requests.Session()

    def validate(self, request: PublishRequest) -> ValidationResult:
        if request.platform != self.platform:
            return ValidationResult(
                ok=False,
                error_category=ErrorCategory.VALIDATION,
                detail=f"request platform does not match {self.platform} adapter",
                next_action="Select the adapter matching the request destination.",
            )
        if not request.media_path.is_file():
            return ValidationResult(
                ok=False,
                error_category=ErrorCategory.VALIDATION,
                detail="media file does not exist",
                next_action="Render the media file before publishing.",
            )
        return ValidationResult(ok=True)

    def _probe(self, account: AccountConfig, secret: Mapping[str, object]) -> ProbeResult:
        raise NotImplementedError

    def probe_auth(self, account: AccountConfig) -> ProbeResult:
        try:
            return self._probe(account, _read_secret(account))
        except _ApiFailure as exc:
            return ProbeResult(
                authenticated=False,
                error_category=exc.category,
                detail=str(exc),
                next_action="Repair the API credentials and run the probe again.",
            )

    def _prepare(self, request: PublishRequest) -> tuple[dict[str, object], ProbeResult]:
        validation = self.validate(request)
        if not validation.ok:
            raise _ApiFailure(
                validation.detail,
                category=validation.error_category or ErrorCategory.VALIDATION,
            )
        secret = _read_secret(request.account)
        try:
            probe = self._probe(request.account, secret)
        except _ApiFailure as exc:
            probe = ProbeResult(
                authenticated=False,
                error_category=exc.category,
                detail=str(exc),
                next_action="Repair the API credentials and run the probe again.",
            )
        return secret, probe


class MetaPublisher(_PublisherBase):
    """Instagram/Facebook Reels publisher using Meta's resumable contracts."""

    def __init__(
        self,
        platform: str,
        transport: HttpTransport | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        max_poll_attempts: int = 20,
    ) -> None:
        if platform not in {"instagram", "facebook"}:
            raise ValueError("MetaPublisher supports instagram or facebook")
        super().__init__(transport)
        self.platform = platform
        self.sleep = sleep
        self.max_poll_attempts = max(1, max_poll_attempts)

    @staticmethod
    def _version(secret: Mapping[str, object]) -> str:
        value = str(secret.get("api_version", "v23.0")).strip()
        return value if value.startswith("v") else f"v{value}"

    def _destination(self, secret: Mapping[str, object]) -> str:
        destinations = secret.get("destination_ids")
        if not isinstance(destinations, dict):
            raise _ApiFailure(
                "Meta destination IDs are not configured",
                category=ErrorCategory.CONFIGURATION,
            )
        value = destinations.get(self.platform)
        if not isinstance(value, str) or not value.strip():
            raise _ApiFailure(
                f"the {self.platform} destination ID is not configured",
                category=ErrorCategory.CONFIGURATION,
            )
        return value.strip()

    def _probe(self, account: AccountConfig, secret: Mapping[str, object]) -> ProbeResult:
        destination = self._destination(secret)
        fields = "id,username" if self.platform == "instagram" else "id,name"
        payload = _request_json(
            self.transport,
            "GET",
            f"https://graph.facebook.com/{self._version(secret)}/{destination}",
            "Meta identity probe",
            params={"fields": fields, "access_token": secret["access_token"]},
        )
        identity_key = "username" if self.platform == "instagram" else "name"
        observed = str(payload.get(identity_key, ""))
        identity = probe_identity(account.expected_identity, observed)
        return ProbeResult(
            authenticated=identity.ok,
            observed_identity=observed,
            error_category=identity.error_category,
            detail=identity.detail,
            next_action=identity.next_action,
        )

    def publish(self, request: PublishRequest) -> PublishResult:
        upload_started = False
        try:
            secret, probe = self._prepare(request)
            if not probe.authenticated:
                return _blocked(probe)
            if request.dry_run:
                return PublishResult(
                    status=PublishStatus.SKIPPED,
                    evidence={"dry_run": True, "observed_identity": probe.observed_identity},
                )
            upload_started = True
            if self.platform == "instagram":
                return self._publish_instagram(request, secret)
            return self._publish_facebook(request, secret)
        except _AmbiguousSubmit as exc:
            return _unknown(exc)
        except Exception as exc:
            if isinstance(exc, _ApiFailure) and exc.category in {
                ErrorCategory.CONFIGURATION,
                ErrorCategory.VALIDATION,
            } and not upload_started:
                return PublishResult(
                    status=PublishStatus.BLOCKED,
                    error_category=exc.category.value,
                    detail=str(exc),
                )
            return _failed(exc, upload_started=upload_started)

    def _upload_bytes(
        self, upload_url: str, media_path: Path, token: object, operation: str
    ) -> dict[str, object]:
        size = media_path.stat().st_size
        with media_path.open("rb") as media:
            return _request_json(
                self.transport,
                "POST",
                upload_url,
                operation,
                headers={
                    "Authorization": f"OAuth {token}",
                    "offset": "0",
                    "file_size": str(size),
                    "Content-Type": "application/octet-stream",
                },
                data=media,
            )

    def _publish_instagram(
        self, request: PublishRequest, secret: Mapping[str, object]
    ) -> PublishResult:
        version = self._version(secret)
        destination = self._destination(secret)
        base = f"https://graph.facebook.com/{version}"
        create = _request_json(
            self.transport,
            "POST",
            f"{base}/{destination}/media",
            "Instagram container creation",
            data={
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": str(request.metadata.get("caption", "")),
                "access_token": secret["access_token"],
            },
        )
        creation_id = str(create.get("id", ""))
        if not creation_id:
            raise _ApiFailure("Instagram did not return a creation ID")
        upload_url = str(
            create.get("uri")
            or f"https://rupload.facebook.com/ig-api-upload/{version}/{creation_id}"
        )
        self._upload_bytes(
            upload_url, request.media_path, secret["access_token"], "Instagram upload"
        )
        self._poll_meta_instagram(base, creation_id, secret["access_token"])
        published = _final_request_json(
            self.transport,
            "POST",
            f"{base}/{destination}/media_publish",
            "Instagram publish",
            data={"creation_id": creation_id, "access_token": secret["access_token"]},
        )
        media_id = str(published.get("id", ""))
        if not media_id:
            raise _ApiFailure("Instagram publish did not return a media ID")
        evidence: dict[str, object] = {"creation_id": creation_id}
        try:
            link = _request_json(
                self.transport,
                "GET",
                f"{base}/{media_id}",
                "Instagram permalink lookup",
                params={
                    "fields": "permalink",
                    "access_token": secret["access_token"],
                },
            )
            permalink = str(link.get("permalink", ""))
        except _ApiFailure:
            # The publish response's media ID is decisive. A secondary link
            # lookup must not erase confirmed publication evidence.
            permalink = ""
            evidence["permalink_lookup"] = "unavailable"
        return PublishResult(
            status=PublishStatus.PUBLISHED,
            platform_id=media_id,
            post_url=permalink,
            evidence=evidence,
        )

    def _poll_meta_instagram(self, base: str, creation_id: str, token: object) -> None:
        for _ in range(self.max_poll_attempts):
            state = _request_json(
                self.transport,
                "GET",
                f"{base}/{creation_id}",
                "Instagram processing status",
                params={"fields": "status_code", "access_token": token},
            )
            status_code = str(state.get("status_code", "")).upper()
            if status_code == "FINISHED":
                return
            if status_code in {"ERROR", "EXPIRED"}:
                raise _ApiFailure("Instagram processing failed")
            self.sleep(2)
        raise _ApiFailure("Instagram processing did not finish before the poll limit")

    def _publish_facebook(
        self, request: PublishRequest, secret: Mapping[str, object]
    ) -> PublishResult:
        version = self._version(secret)
        destination = self._destination(secret)
        base = f"https://graph.facebook.com/{version}"
        start = _request_json(
            self.transport,
            "POST",
            f"{base}/{destination}/video_reels",
            "Facebook Reels upload start",
            params={"access_token": secret["access_token"]},
            data={"upload_phase": "start"},
        )
        video_id = str(start.get("video_id", ""))
        upload_url = str(start.get("upload_url", ""))
        if not video_id or not upload_url:
            raise _ApiFailure("Facebook did not return an upload URL and video ID")
        self._upload_bytes(
            upload_url, request.media_path, secret["access_token"], "Facebook upload"
        )
        self._poll_meta_facebook(base, video_id, secret["access_token"])
        finish = _final_request_json(
            self.transport,
            "POST",
            f"{base}/{destination}/video_reels",
            "Facebook Reels publish",
            params={"access_token": secret["access_token"]},
            data={
                "upload_phase": "finish",
                "video_id": video_id,
                "video_state": "PUBLISHED",
                "description": str(request.metadata.get("caption", "")),
            },
        )
        published_id = str(finish.get("id", "")) if finish.get("success") else ""
        if not published_id:
            raise _ApiFailure("Facebook publish did not return positive success evidence")
        evidence: dict[str, object] = {"video_id": video_id}
        try:
            link = _request_json(
                self.transport,
                "GET",
                f"{base}/{published_id}",
                "Facebook permalink lookup",
                params={
                    "fields": "permalink_url",
                    "access_token": secret["access_token"],
                },
            )
            permalink = str(link.get("permalink_url", ""))
        except _ApiFailure:
            permalink = ""
            evidence["permalink_lookup"] = "unavailable"
        return PublishResult(
            status=PublishStatus.PUBLISHED,
            platform_id=published_id,
            post_url=permalink,
            evidence=evidence,
        )

    def _poll_meta_facebook(self, base: str, video_id: str, token: object) -> None:
        for _ in range(self.max_poll_attempts):
            state = _request_json(
                self.transport,
                "GET",
                f"{base}/{video_id}",
                "Facebook processing status",
                params={"fields": "status", "access_token": token},
            )
            status_value = state.get("status", {})
            status_text = (
                str(status_value.get("video_status", ""))
                if isinstance(status_value, dict)
                else str(status_value)
            ).casefold()
            if status_text in {"ready", "complete", "completed", "published"}:
                return
            if status_text in {"error", "failed", "expired"}:
                raise _ApiFailure("Facebook processing failed")
            self.sleep(2)
        raise _ApiFailure("Facebook processing did not finish before the poll limit")


class YouTubePublisher(_PublisherBase):
    platform = "youtube"

    def __init__(
        self,
        transport: HttpTransport | None = None,
        *,
        chunk_size: int = 8 * 1024 * 1024,
        max_resume_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(transport)
        if chunk_size < 1 or chunk_size % (256 * 1024):
            raise ValueError(
                "chunk_size must be a positive multiple of 256 KiB; only the final chunk may be smaller"
            )
        if max_resume_attempts < 1:
            raise ValueError("max_resume_attempts must be positive")
        self.chunk_size = chunk_size
        self.max_resume_attempts = max_resume_attempts
        self.sleep = sleep

    def _probe(self, account: AccountConfig, secret: Mapping[str, object]) -> ProbeResult:
        scopes = secret.get("scopes", [])
        if not isinstance(scopes, list) or YOUTUBE_UPLOAD_SCOPE not in scopes:
            return ProbeResult(
                authenticated=False,
                error_category=ErrorCategory.CONFIGURATION,
                detail="the YouTube upload scope is not configured",
                next_action=f"Authorize {YOUTUBE_UPLOAD_SCOPE}.",
            )
        payload = _request_json(
            self.transport,
            "GET",
            "https://www.googleapis.com/youtube/v3/channels",
            "YouTube identity probe",
            headers={"Authorization": f"Bearer {secret['access_token']}"},
            params={"part": "snippet", "mine": "true"},
        )
        items = payload.get("items", [])
        item = items[0] if isinstance(items, list) and items else {}
        snippet = item.get("snippet", {}) if isinstance(item, dict) else {}
        observed = str(snippet.get("title", "")) if isinstance(snippet, dict) else ""
        identity = probe_identity(account.expected_identity, observed)
        return ProbeResult(
            authenticated=identity.ok,
            observed_identity=observed,
            error_category=identity.error_category,
            detail=identity.detail,
            next_action=identity.next_action,
        )

    def publish(self, request: PublishRequest) -> PublishResult:
        upload_started = False
        try:
            secret, probe = self._prepare(request)
            if not probe.authenticated:
                return _blocked(probe)
            if request.dry_run:
                return PublishResult(
                    status=PublishStatus.SKIPPED,
                    evidence={"dry_run": True, "observed_identity": probe.observed_identity},
                )
            metadata = request.metadata
            requested_visibility = str(metadata.get("visibility", "private"))
            publish_at = metadata.get("publish_at")
            upload_visibility = "private" if publish_at else requested_visibility
            made_for_kids = bool(metadata.get("made_for_kids", False))
            synthetic = bool(metadata.get("contains_synthetic_media", False))
            status: dict[str, object] = {
                "privacyStatus": upload_visibility,
                "selfDeclaredMadeForKids": made_for_kids,
                "containsSyntheticMedia": synthetic,
            }
            if publish_at:
                status["publishAt"] = publish_at
            body = {
                "snippet": {
                    "title": str(metadata.get("title", "")),
                    "description": str(metadata.get("description", metadata.get("caption", ""))),
                    "tags": list(metadata.get("tags", [])),
                    "categoryId": str(metadata.get("category_id", "22")),
                },
                "status": status,
            }
            media_size = request.media_path.stat().st_size
            if media_size < 1:
                raise _ApiFailure(
                    "YouTube media file is empty", category=ErrorCategory.VALIDATION
                )
            content_type = mimetypes.guess_type(request.media_path.name)[0] or "video/mp4"
            upload_started = True
            try:
                initiation = self.transport.request(
                    "POST",
                    "https://www.googleapis.com/upload/youtube/v3/videos",
                    timeout=30,
                    headers={
                        "Authorization": f"Bearer {secret['access_token']}",
                        "X-Upload-Content-Length": str(media_size),
                        "X-Upload-Content-Type": content_type,
                        "Content-Type": "application/json; charset=UTF-8",
                    },
                    params={"uploadType": "resumable", "part": "snippet,status"},
                    json=body,
                )
            except Exception as exc:
                raise _ApiFailure(f"YouTube upload initiation failed: {exc}") from exc
            _response_json(initiation, "YouTube upload initiation")
            location = str(getattr(initiation, "headers", {}).get("Location", ""))
            if not location:
                raise _ApiFailure("YouTube did not return a resumable upload location")
            uploaded = self._upload_resumable(
                location=location,
                media_path=request.media_path,
                media_size=media_size,
                content_type=content_type,
                access_token=str(secret["access_token"]),
            )
            video_id = str(uploaded.get("id", ""))
            actual_status = uploaded.get("status", {})
            actual_status = actual_status if isinstance(actual_status, dict) else {}
            if not video_id:
                raise _ApiFailure("YouTube upload did not return a video ID")
            return PublishResult(
                status=PublishStatus.SUBMITTED,
                platform_id=video_id,
                post_url=f"https://www.youtube.com/watch?v={video_id}",
                evidence={
                    "requested_visibility": requested_visibility,
                    "actual_visibility": str(actual_status.get("privacyStatus", "")),
                    "requested_made_for_kids": made_for_kids,
                    "actual_made_for_kids": actual_status.get("selfDeclaredMadeForKids"),
                    "requested_contains_synthetic_media": synthetic,
                    "actual_contains_synthetic_media": actual_status.get("containsSyntheticMedia"),
                    "requested_publish_at": publish_at,
                    "actual_publish_at": actual_status.get("publishAt"),
                    "upload_scope": YOUTUBE_UPLOAD_SCOPE,
                },
            )
        except _AmbiguousSubmit as exc:
            return _unknown(exc)
        except Exception as exc:
            if isinstance(exc, _ApiFailure) and exc.category in {
                ErrorCategory.CONFIGURATION,
                ErrorCategory.VALIDATION,
            } and not upload_started:
                return PublishResult(
                    status=PublishStatus.BLOCKED,
                    error_category=exc.category.value,
                    detail=str(exc),
                )
            return _failed(exc, upload_started=upload_started)

    @staticmethod
    def _resume_offset(response: object, total: int) -> int:
        range_value = str(getattr(response, "headers", {}).get("Range", ""))
        match = re.fullmatch(r"(?:bytes=)?0-(\d+)", range_value.strip())
        if not match:
            return 0
        return min(int(match.group(1)) + 1, total)

    def _advance_confirmed_high_water(
        self,
        *,
        reported_offset: int,
        confirmed_high_water: int,
        stalled_responses: int,
        final_chunk_was_sent: bool,
    ) -> tuple[int, int]:
        if reported_offset > confirmed_high_water:
            return reported_offset, 0
        stalled_responses += 1
        if stalled_responses >= self.max_resume_attempts:
            if final_chunk_was_sent:
                raise _AmbiguousSubmit(
                    "YouTube final upload could not make monotonic confirmed progress; reconcile before retrying"
                )
            raise _ApiFailure(
                "YouTube resumable reconciliation made no monotonic progress before the retry limit"
            )
        # Missing, malformed, equal, and regressing Range reports never lower
        # the next send offset and never reset the stall budget.
        return confirmed_high_water, stalled_responses

    def _query_upload_status(
        self,
        *,
        location: str,
        media_size: int,
        access_token: str,
        final_chunk_was_sent: bool,
    ) -> tuple[int, dict[str, object] | None]:
        last_error: BaseException | None = None
        for attempt in range(self.max_resume_attempts):
            if attempt:
                self.sleep(min(2**attempt, 8))
            try:
                response = self.transport.request(
                    "PUT",
                    location,
                    timeout=30,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Length": "0",
                        "Content-Range": f"bytes */{media_size}",
                    },
                    data=b"",
                )
            except Exception as exc:
                if not _is_connection_loss(exc):
                    raise _ApiFailure(
                        f"YouTube upload status request failed: {exc}"
                    ) from exc
                last_error = exc
                continue
            try:
                status_code = int(getattr(response, "status_code", 0) or 0)
            except Exception as exc:
                if final_chunk_was_sent:
                    raise _AmbiguousSubmit(
                        "YouTube final upload status response could not be read; reconcile before retrying"
                    ) from exc
                raise _ApiFailure(
                    "YouTube upload status response could not be read"
                ) from exc
            if status_code == 308:
                return self._resume_offset(response, media_size), None
            if 200 <= status_code < 300:
                return media_size, _response_json(
                    response,
                    "YouTube upload status reconciliation",
                    ambiguous_success=True,
                )
            if status_code in {500, 502, 503, 504}:
                last_error = _ApiFailure(
                    f"YouTube upload status returned HTTP {status_code}"
                )
                continue
            _response_json(response, "YouTube upload status reconciliation")
        if final_chunk_was_sent:
            raise _AmbiguousSubmit(
                f"YouTube final upload response could not be reconciled: {last_error or 'status unavailable'}"
            )
        raise _ApiFailure(
            f"YouTube upload status could not be reconciled: {last_error or 'status unavailable'}"
        )

    def _upload_resumable(
        self,
        *,
        location: str,
        media_path: Path,
        media_size: int,
        content_type: str,
        access_token: str,
    ) -> dict[str, object]:
        confirmed_high_water = 0
        stalled_responses = 0
        with media_path.open("rb") as media:
            while confirmed_high_water < media_size:
                media.seek(confirmed_high_water)
                chunk = media.read(
                    min(self.chunk_size, media_size - confirmed_high_water)
                )
                if not chunk:
                    raise _ApiFailure("YouTube media ended before its declared size")
                end = confirmed_high_water + len(chunk) - 1
                final_chunk = end + 1 == media_size
                headers = {
                    "Authorization": f"Bearer {access_token}",
                    "Content-Length": str(len(chunk)),
                    "Content-Type": content_type,
                    "Content-Range": (
                        f"bytes {confirmed_high_water}-{end}/{media_size}"
                    ),
                }
                try:
                    response = self.transport.request(
                        "PUT",
                        location,
                        timeout=30,
                        headers=headers,
                        data=chunk,
                    )
                except Exception as exc:
                    if not _is_connection_loss(exc):
                        raise _ApiFailure(f"YouTube media upload failed: {exc}") from exc
                    if final_chunk and _is_response_phase_loss(exc):
                        raise _AmbiguousSubmit(
                            "YouTube final upload response stream was lost; reconcile before retrying"
                        ) from exc
                    next_offset, completed = self._query_upload_status(
                        location=location,
                        media_size=media_size,
                        access_token=access_token,
                        final_chunk_was_sent=final_chunk,
                    )
                    if completed is not None:
                        return completed
                    confirmed_high_water, stalled_responses = (
                        self._advance_confirmed_high_water(
                            reported_offset=next_offset,
                            confirmed_high_water=confirmed_high_water,
                            stalled_responses=stalled_responses,
                            final_chunk_was_sent=final_chunk,
                        )
                    )
                    continue

                try:
                    status_code = int(getattr(response, "status_code", 0) or 0)
                except Exception as exc:
                    if final_chunk:
                        raise _AmbiguousSubmit(
                            "YouTube final upload response could not be read; reconcile before retrying"
                        ) from exc
                    raise _ApiFailure(
                        "YouTube upload response could not be read"
                    ) from exc
                if status_code == 308:
                    next_offset = self._resume_offset(response, media_size)
                    confirmed_high_water, stalled_responses = (
                        self._advance_confirmed_high_water(
                            reported_offset=next_offset,
                            confirmed_high_water=confirmed_high_water,
                            stalled_responses=stalled_responses,
                            final_chunk_was_sent=final_chunk,
                        )
                    )
                    continue
                if status_code in {500, 502, 503, 504}:
                    next_offset, completed = self._query_upload_status(
                        location=location,
                        media_size=media_size,
                        access_token=access_token,
                        final_chunk_was_sent=final_chunk,
                    )
                    if completed is not None:
                        return completed
                    confirmed_high_water, stalled_responses = (
                        self._advance_confirmed_high_water(
                            reported_offset=next_offset,
                            confirmed_high_water=confirmed_high_water,
                            stalled_responses=stalled_responses,
                            final_chunk_was_sent=final_chunk,
                        )
                    )
                    continue
                if 200 <= status_code < 300:
                    return _response_json(
                        response,
                        "YouTube media upload",
                        ambiguous_success=True,
                    )
                _response_json(response, "YouTube media upload")
        raise _AmbiguousSubmit(
            "YouTube upload bytes were confirmed without a completion response; reconcile before retrying"
        )


class XPublisher(_PublisherBase):
    platform = "x"

    def __init__(
        self,
        transport: HttpTransport | None = None,
        *,
        chunk_size: int = 4 * 1024 * 1024,
        sleep: Callable[[float], None] = time.sleep,
        max_poll_attempts: int = 20,
        nonce_factory: Callable[[], str] | None = None,
        timestamp_factory: Callable[[], int] | None = None,
    ) -> None:
        super().__init__(transport)
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        self.chunk_size = chunk_size
        self.sleep = sleep
        self.max_poll_attempts = max(1, max_poll_attempts)
        self.nonce_factory = nonce_factory or (lambda: secrets.token_hex(16))
        self.timestamp_factory = timestamp_factory or (lambda: int(time.time()))

    @staticmethod
    def _credentials(secret: Mapping[str, object]) -> dict[str, str]:
        names = (
            "consumer_key",
            "consumer_secret",
            "access_token",
            "access_token_secret",
        )
        values: dict[str, str] = {}
        for name in names:
            value = secret.get(name)
            if not isinstance(value, str) or not value.strip():
                raise _ApiFailure(
                    f"X OAuth 1.0a credential {name} is not configured",
                    category=ErrorCategory.CONFIGURATION,
                )
            values[name] = value
        return values

    def _x_request(
        self,
        secret: Mapping[str, object],
        method: str,
        url: str,
        operation: str,
        *,
        params: Mapping[str, object] | None = None,
        data: Mapping[str, object] | None = None,
        files: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
        final_mutation: bool = False,
    ) -> dict[str, object]:
        credentials = self._credentials(secret)
        # OAuth 1.0a signs URL query parameters and form-encoded bodies. Multipart
        # and JSON bodies are intentionally excluded from the signature base.
        form = data if data is not None and files is None and json_body is None else None
        authorization = _oauth1_authorization(
            method=method,
            url=url,
            **credentials,
            nonce=self.nonce_factory(),
            timestamp=self.timestamp_factory(),
            query=params,
            form=form,
        )
        headers = {"Authorization": authorization}
        kwargs: dict[str, object] = {"headers": headers}
        if params is not None:
            kwargs["params"] = dict(params)
        if data is not None:
            kwargs["data"] = dict(data)
        if files is not None:
            kwargs["files"] = dict(files)
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            kwargs["json"] = dict(json_body)
        requester = _final_request_json if final_mutation else _request_json
        return requester(self.transport, method, url, operation, **kwargs)

    def _probe(self, account: AccountConfig, secret: Mapping[str, object]) -> ProbeResult:
        payload = self._x_request(
            secret,
            "GET",
            "https://api.x.com/1.1/account/verify_credentials.json",
            "X identity probe",
            params={"skip_status": "true", "include_email": "false"},
        )
        data = payload.get("data", {})
        observed = str(payload.get("screen_name", ""))
        if not observed and isinstance(data, dict):
            observed = str(data.get("username", ""))
        identity = probe_identity(account.expected_identity, observed)
        return ProbeResult(
            authenticated=identity.ok,
            observed_identity=observed,
            error_category=identity.error_category,
            detail=identity.detail,
            next_action=identity.next_action,
        )

    @staticmethod
    def _media_id(payload: Mapping[str, object]) -> str:
        data = payload.get("data", {})
        if isinstance(data, dict):
            value = data.get("id") or data.get("media_id_string")
            if value:
                return str(value)
        value = payload.get("media_id_string") or payload.get("media_id")
        return str(value or "")

    @staticmethod
    def _processing(payload: Mapping[str, object]) -> dict[str, object]:
        data = payload.get("data", {})
        if isinstance(data, dict) and isinstance(data.get("processing_info"), dict):
            return data["processing_info"]  # type: ignore[return-value]
        value = payload.get("processing_info", {})
        return value if isinstance(value, dict) else {}

    def publish(self, request: PublishRequest) -> PublishResult:
        upload_started = False
        try:
            secret, probe = self._prepare(request)
            if not probe.authenticated:
                return _blocked(probe)
            if request.dry_run:
                return PublishResult(
                    status=PublishStatus.SKIPPED,
                    evidence={"dry_run": True, "observed_identity": probe.observed_identity},
                )
            media_size = request.media_path.stat().st_size
            if media_size < 1:
                raise _ApiFailure(
                    "X media file is empty", category=ErrorCategory.VALIDATION
                )
            upload_url = "https://upload.twitter.com/1.1/media/upload.json"
            upload_started = True
            initialized = self._x_request(
                secret,
                "POST",
                upload_url,
                "X media upload initialization",
                data={
                    "command": "INIT",
                    "total_bytes": media_size,
                    "media_type": mimetypes.guess_type(request.media_path.name)[0]
                    or "video/mp4",
                    "media_category": "tweet_video",
                },
            )
            media_id = self._media_id(initialized)
            if not media_id:
                raise _ApiFailure("X did not return a media ID")
            with request.media_path.open("rb") as media:
                index = 0
                while True:
                    chunk = media.read(self.chunk_size)
                    if not chunk:
                        break
                    self._x_request(
                        secret,
                        "POST",
                        upload_url,
                        "X media chunk upload",
                        data={
                            "command": "APPEND",
                            "media_id": media_id,
                            "segment_index": index,
                        },
                        files={"media": (request.media_path.name, chunk)},
                    )
                    index += 1
            finalized = self._x_request(
                secret,
                "POST",
                upload_url,
                "X media upload finalize",
                data={"command": "FINALIZE", "media_id": media_id},
            )
            processing = self._processing(finalized)
            if processing:
                self._poll_processing(secret, upload_url, media_id, processing)
            body: dict[str, object] = {
                "text": str(request.metadata.get("caption", "")),
                "media": {"media_ids": [media_id]},
            }
            for disclosure in ("made_with_ai", "paid_partnership"):
                if disclosure in request.metadata:
                    body[disclosure] = bool(request.metadata[disclosure])
            created = self._x_request(
                secret,
                "POST",
                "https://api.x.com/2/tweets",
                "X post creation",
                json_body=body,
                final_mutation=True,
            )
            data = created.get("data", {})
            post_id = str(data.get("id", "")) if isinstance(data, dict) else ""
            if not post_id:
                raise _ApiFailure("X post creation did not return a post ID")
            return PublishResult(
                status=PublishStatus.PUBLISHED,
                platform_id=post_id,
                post_url=f"https://x.com/i/web/status/{post_id}",
                evidence={"media_id": media_id},
            )
        except _AmbiguousSubmit as exc:
            return _unknown(exc)
        except Exception as exc:
            if isinstance(exc, _ApiFailure) and exc.category in {
                ErrorCategory.CONFIGURATION,
                ErrorCategory.VALIDATION,
            } and not upload_started:
                return PublishResult(
                    status=PublishStatus.BLOCKED,
                    error_category=exc.category.value,
                    detail=str(exc),
                )
            return _failed(exc, upload_started=upload_started)

    def _poll_processing(
        self,
        secret: Mapping[str, object],
        upload_url: str,
        media_id: str,
        processing: Mapping[str, object],
    ) -> None:
        current = dict(processing)
        for _ in range(self.max_poll_attempts):
            state = str(current.get("state", "")).casefold()
            if state == "succeeded":
                return
            if state in {"failed", "error"}:
                raise _ApiFailure("X media processing failed")
            self.sleep(float(current.get("check_after_secs", 1) or 0))
            payload = self._x_request(
                secret,
                "GET",
                upload_url,
                "X media processing status",
                params={"command": "STATUS", "media_id": media_id},
            )
            current = self._processing(payload)
        if str(current.get("state", "")).casefold() == "succeeded":
            return
        raise _ApiFailure("X media processing did not finish before the poll limit")
