from __future__ import annotations

import json
import mimetypes
import os
import stat
import time
from pathlib import Path
from typing import Callable, Mapping, Protocol

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


def _read_secret(account: AccountConfig) -> dict[str, object]:
    path = account.secret_file
    if path is None:
        raise _ApiFailure(
            "an API secret file is not configured",
            category=ErrorCategory.CONFIGURATION,
        )
    path = Path(path).expanduser()
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


def _response_json(response: object, operation: str) -> dict[str, object]:
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
        return {}
    try:
        value = response.json()  # type: ignore[attr-defined]
    except Exception as exc:
        if not str(getattr(response, "text", "")):
            return {}
        raise _ApiFailure(f"{operation} returned invalid JSON") from exc
    if not isinstance(value, dict):
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
        media = media_path.read_bytes()
        return _request_json(
            self.transport,
            "POST",
            upload_url,
            operation,
            headers={
                "Authorization": f"OAuth {token}",
                "offset": "0",
                "file_size": str(len(media)),
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
        published = _request_json(
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
        finish = _request_json(
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
        published_id = str(finish.get("id") or video_id) if finish.get("success") else ""
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
            media = request.media_path.read_bytes()
            content_type = mimetypes.guess_type(request.media_path.name)[0] or "video/mp4"
            upload_started = True
            try:
                initiation = self.transport.request(
                    "POST",
                    "https://www.googleapis.com/upload/youtube/v3/videos",
                    timeout=30,
                    headers={
                        "Authorization": f"Bearer {secret['access_token']}",
                        "X-Upload-Content-Length": str(len(media)),
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
            uploaded = _request_json(
                self.transport,
                "PUT",
                location,
                "YouTube media upload",
                headers={"Content-Type": content_type, "Content-Length": str(len(media))},
                data=media,
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


class XPublisher(_PublisherBase):
    platform = "x"

    def __init__(
        self,
        transport: HttpTransport | None = None,
        *,
        chunk_size: int = 4 * 1024 * 1024,
        sleep: Callable[[float], None] = time.sleep,
        max_poll_attempts: int = 20,
    ) -> None:
        super().__init__(transport)
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        self.chunk_size = chunk_size
        self.sleep = sleep
        self.max_poll_attempts = max(1, max_poll_attempts)

    def _headers(self, secret: Mapping[str, object]) -> dict[str, str]:
        return {"Authorization": f"Bearer {secret['access_token']}"}

    def _probe(self, account: AccountConfig, secret: Mapping[str, object]) -> ProbeResult:
        payload = _request_json(
            self.transport,
            "GET",
            "https://api.x.com/2/users/me",
            "X identity probe",
            headers=self._headers(secret),
            params={"user.fields": "username"},
        )
        data = payload.get("data", {})
        observed = str(data.get("username", "")) if isinstance(data, dict) else ""
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
            media = request.media_path.read_bytes()
            headers = self._headers(secret)
            upload_url = "https://upload.twitter.com/1.1/media/upload.json"
            upload_started = True
            initialized = _request_json(
                self.transport,
                "POST",
                upload_url,
                "X media upload initialization",
                headers=headers,
                data={
                    "command": "INIT",
                    "total_bytes": len(media),
                    "media_type": mimetypes.guess_type(request.media_path.name)[0]
                    or "video/mp4",
                    "media_category": "tweet_video",
                },
            )
            media_id = self._media_id(initialized)
            if not media_id:
                raise _ApiFailure("X did not return a media ID")
            for index, offset in enumerate(range(0, len(media), self.chunk_size)):
                _request_json(
                    self.transport,
                    "POST",
                    upload_url,
                    "X media chunk upload",
                    headers=headers,
                    data={
                        "command": "APPEND",
                        "media_id": media_id,
                        "segment_index": index,
                    },
                    files={"media": (request.media_path.name, media[offset : offset + self.chunk_size])},
                )
            finalized = _request_json(
                self.transport,
                "POST",
                upload_url,
                "X media upload finalize",
                headers=headers,
                data={"command": "FINALIZE", "media_id": media_id},
            )
            processing = self._processing(finalized)
            if processing:
                self._poll_processing(upload_url, headers, media_id, processing)
            body: dict[str, object] = {
                "text": str(request.metadata.get("caption", "")),
                "media": {"media_ids": [media_id]},
            }
            for disclosure in ("made_with_ai", "paid_partnership"):
                if disclosure in request.metadata:
                    body[disclosure] = bool(request.metadata[disclosure])
            created = _request_json(
                self.transport,
                "POST",
                "https://api.x.com/2/tweets",
                "X post creation",
                headers={**headers, "Content-Type": "application/json"},
                json=body,
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
        upload_url: str,
        headers: Mapping[str, str],
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
            payload = _request_json(
                self.transport,
                "GET",
                upload_url,
                "X media processing status",
                headers=dict(headers),
                params={"command": "STATUS", "media_id": media_id},
            )
            current = self._processing(payload)
        if str(current.get("state", "")).casefold() == "succeeded":
            return
        raise _ApiFailure("X media processing did not finish before the poll limit")
