from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_media_ads.models import AccountConfig, PublishRequest


class FakeResponse:
    def __init__(
        self,
        payload: object = None,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        text: str = "",
    ) -> None:
        self._payload = {} if payload is None else payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text

    def json(self) -> object:
        return self._payload


class EmptyResponse(FakeResponse):
    def json(self) -> object:
        raise ValueError("no response body")


class TruncatedSuccessResponse(FakeResponse):
    def json(self) -> object:
        raise ValueError("truncated JSON response body")


class ResponseStreamLostError(Exception):
    phase = "response_body"


class QueueTransport:
    def __init__(self, responses: list[FakeResponse | BaseException]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _secret_file(tmp_path: Path, **overrides: object) -> Path:
    data: dict[str, object] = {
        "access_token": "top-secret-token",
        "access_token_secret": "top-secret-access-secret",
        "consumer_key": "test-consumer-key",
        "consumer_secret": "top-secret-consumer-secret",
        "api_version": "v23.0",
        "destination_ids": {
            "instagram": "ig-destination-1",
            "facebook": "fb-destination-1",
        },
        "scopes": ["https://www.googleapis.com/auth/youtube.upload"],
    }
    data.update(overrides)
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)
    return path


def _request(
    tmp_path: Path,
    *,
    platform: str,
    secret_file: Path | None = None,
    metadata: dict[str, object] | None = None,
    dry_run: bool = False,
) -> PublishRequest:
    media = tmp_path / "video.mp4"
    media.write_bytes(b"abcdefghij")
    account = AccountConfig(
        account_id="local-account",
        expected_identity="expected-account",
        mode="api",
        secret_file=secret_file or _secret_file(tmp_path),
    )
    return PublishRequest(
        content_id="content-1",
        revision=1,
        platform=platform,
        account=account,
        media_path=media,
        metadata=metadata or {"caption": "A caption", "title": "A title"},
        idempotency_key="attempt-1",
        dry_run=dry_run,
    )


def test_meta_returns_published_only_after_permalink(tmp_path: Path) -> None:
    from codex_media_ads.publishing.api_adapters import MetaPublisher

    transport = QueueTransport(
        [
            FakeResponse({"id": "ig-destination-1", "username": "expected-account"}),
            FakeResponse({"id": "creation-1"}),
            FakeResponse({"success": True}),
            FakeResponse({"status_code": "FINISHED"}),
            FakeResponse({"id": "media-1"}),
            FakeResponse({"permalink": "https://social.example/reel/media-1"}),
        ]
    )
    adapter = MetaPublisher("instagram", transport, sleep=lambda _seconds: None)

    result = adapter.publish(_request(tmp_path, platform="instagram"))

    assert result.status == "published"
    assert result.platform_id == "media-1"
    assert result.post_url.endswith("media-1")
    assert result.evidence["creation_id"] == "creation-1"


def test_meta_keeps_confirmed_publish_when_permalink_lookup_fails(
    tmp_path: Path,
) -> None:
    from codex_media_ads.publishing.api_adapters import MetaPublisher

    transport = QueueTransport(
        [
            FakeResponse({"id": "ig-destination-1", "username": "expected-account"}),
            FakeResponse({"id": "creation-1"}),
            FakeResponse({"success": True}),
            FakeResponse({"status_code": "FINISHED"}),
            FakeResponse({"id": "media-1"}),
            FakeResponse(status_code=503, text="temporarily unavailable"),
        ]
    )

    result = MetaPublisher("instagram", transport, sleep=lambda _seconds: None).publish(
        _request(tmp_path, platform="instagram")
    )

    assert result.status == "published"
    assert result.platform_id == "media-1"
    assert result.post_url == ""
    assert result.evidence["permalink_lookup"] == "unavailable"


def test_meta_uses_separate_facebook_destination_and_finish_contract(
    tmp_path: Path,
) -> None:
    from codex_media_ads.publishing.api_adapters import MetaPublisher

    transport = QueueTransport(
        [
            FakeResponse({"id": "fb-destination-1", "name": "expected-account"}),
            FakeResponse(
                {
                    "video_id": "video-1",
                    "upload_url": "https://rupload.facebook.com/video-upload/v23.0/video-1",
                }
            ),
            FakeResponse({"success": True}),
            FakeResponse({"status": {"video_status": "ready"}}),
            FakeResponse({"success": True, "id": "video-1"}),
            FakeResponse({"permalink_url": "https://facebook.example/reel/video-1"}),
        ]
    )
    adapter = MetaPublisher("facebook", transport, sleep=lambda _seconds: None)

    result = adapter.publish(_request(tmp_path, platform="facebook"))

    assert result.status == "published"
    assert result.platform_id == "video-1"
    start = transport.calls[1]
    finish = transport.calls[4]
    assert "/fb-destination-1/video_reels" in start[1]
    assert start[2]["data"] == {"upload_phase": "start"}
    assert finish[2]["data"]["video_state"] == "PUBLISHED"


def test_facebook_finish_must_return_a_published_id(tmp_path: Path) -> None:
    from codex_media_ads.publishing.api_adapters import MetaPublisher

    transport = QueueTransport(
        [
            FakeResponse({"id": "fb-destination-1", "name": "expected-account"}),
            FakeResponse({"video_id": "upload-session-1", "upload_url": "https://upload.example"}),
            FakeResponse({"success": True}),
            FakeResponse({"status": {"video_status": "ready"}}),
            FakeResponse({"success": True}),
        ]
    )

    result = MetaPublisher("facebook", transport, sleep=lambda _seconds: None).publish(
        _request(tmp_path, platform="facebook")
    )

    assert result.status == "failed"
    assert result.platform_id == ""


def test_meta_final_publish_timeout_is_ambiguous_unknown(tmp_path: Path) -> None:
    from codex_media_ads.publishing.api_adapters import MetaPublisher

    transport = QueueTransport(
        [
            FakeResponse({"id": "ig-destination-1", "username": "expected-account"}),
            FakeResponse({"id": "creation-1"}),
            FakeResponse({"success": True}),
            FakeResponse({"status_code": "FINISHED"}),
            TimeoutError("access_token=must-not-leak"),
        ]
    )

    result = MetaPublisher("instagram", transport, sleep=lambda _seconds: None).publish(
        _request(tmp_path, platform="instagram")
    )

    assert result.status == "unknown"
    assert result.error_category == "ambiguous_submit"
    assert result.evidence["retry_safe"] is False
    assert "must-not-leak" not in result.model_dump_json()


@pytest.mark.parametrize(
    "final_response",
    [
        TruncatedSuccessResponse(status_code=200, text="{"),
        ResponseStreamLostError("response stream ended early"),
    ],
)
def test_meta_success_or_response_stream_loss_at_final_publish_is_unknown(
    tmp_path: Path, final_response: FakeResponse | BaseException
) -> None:
    from codex_media_ads.publishing.api_adapters import MetaPublisher

    transport = QueueTransport(
        [
            FakeResponse({"id": "ig-destination-1", "username": "expected-account"}),
            FakeResponse({"id": "creation-1"}),
            FakeResponse({"success": True}),
            FakeResponse({"status_code": "FINISHED"}),
            final_response,
        ]
    )

    result = MetaPublisher("instagram", transport, sleep=lambda _seconds: None).publish(
        _request(tmp_path, platform="instagram")
    )

    assert result.status == "unknown"
    assert result.error_category == "ambiguous_submit"
    assert result.evidence["retry_safe"] is False


def test_youtube_reports_requested_and_actual_upload_evidence(tmp_path: Path) -> None:
    from codex_media_ads.publishing.api_adapters import YouTubePublisher

    transport = QueueTransport(
        [
            FakeResponse(
                {
                    "items": [
                        {
                            "id": "channel-1",
                            "snippet": {"title": "expected-account"},
                        }
                    ]
                }
            ),
            FakeResponse(headers={"Location": "https://upload.example/session-1"}),
            FakeResponse(
                {
                    "id": "video-1",
                    "status": {
                        "privacyStatus": "private",
                        "selfDeclaredMadeForKids": False,
                        "containsSyntheticMedia": True,
                        "publishAt": "2030-01-02T03:04:05Z",
                    },
                }
            ),
        ]
    )
    request = _request(
        tmp_path,
        platform="youtube",
        metadata={
            "title": "A title",
            "description": "A description",
            "visibility": "public",
            "made_for_kids": False,
            "contains_synthetic_media": True,
            "publish_at": "2030-01-02T03:04:05Z",
        },
    )

    result = YouTubePublisher(transport).publish(request)

    assert result.status == "submitted"
    assert result.platform_id == "video-1"
    assert result.evidence == {
        "requested_visibility": "public",
        "actual_visibility": "private",
        "requested_made_for_kids": False,
        "actual_made_for_kids": False,
        "requested_contains_synthetic_media": True,
        "actual_contains_synthetic_media": True,
        "requested_publish_at": "2030-01-02T03:04:05Z",
        "actual_publish_at": "2030-01-02T03:04:05Z",
        "upload_scope": "https://www.googleapis.com/auth/youtube.upload",
    }
    initiation = transport.calls[1]
    body = initiation[2]["json"]
    assert initiation[2]["params"] == {
        "uploadType": "resumable",
        "part": "snippet,status",
    }
    assert body["status"] == {
        "privacyStatus": "private",
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": True,
        "publishAt": "2030-01-02T03:04:05Z",
    }


def test_youtube_upload_put_is_authorized_and_resumes_from_308_range(
    tmp_path: Path,
) -> None:
    from codex_media_ads.publishing.api_adapters import YouTubePublisher

    transport = QueueTransport(
        [
            FakeResponse({"items": [{"snippet": {"title": "expected-account"}}]}),
            EmptyResponse(headers={"Location": "https://upload.example/session-1"}),
            EmptyResponse(status_code=308, headers={"Range": "bytes=0-3"}),
            FakeResponse({"id": "video-1", "status": {"privacyStatus": "private"}}, status_code=201),
        ]
    )

    result = YouTubePublisher(transport).publish(
        _request(tmp_path, platform="youtube", metadata={"title": "A title"})
    )

    assert result.status == "submitted"
    puts = [call for call in transport.calls if call[0] == "PUT"]
    assert len(puts) == 2
    assert all(call[2]["headers"]["Authorization"] == "Bearer top-secret-token" for call in puts)
    assert puts[0][2]["headers"]["Content-Range"] == "bytes 0-9/10"
    assert puts[1][2]["headers"]["Content-Range"] == "bytes 4-9/10"
    assert puts[1][2]["data"] == b"efghij"


def test_youtube_final_chunk_timeout_without_reconciliation_is_unknown(
    tmp_path: Path,
) -> None:
    from codex_media_ads.publishing.api_adapters import YouTubePublisher

    transport = QueueTransport(
        [
            FakeResponse({"items": [{"snippet": {"title": "expected-account"}}]}),
            EmptyResponse(headers={"Location": "https://upload.example/session-1"}),
            TimeoutError("final upload timed out"),
            TimeoutError("status unavailable"),
        ]
    )

    result = YouTubePublisher(transport, max_resume_attempts=1).publish(
        _request(tmp_path, platform="youtube", metadata={"title": "A title"})
    )

    assert result.status == "unknown"
    assert result.error_category == "ambiguous_submit"
    assert result.evidence["retry_safe"] is False
    assert len([call for call in transport.calls if call[0] == "PUT"]) == 2


def test_youtube_repeated_zero_progress_reconciliation_is_bounded(
    tmp_path: Path,
) -> None:
    from codex_media_ads.publishing.api_adapters import YouTubePublisher

    transport = QueueTransport(
        [
            FakeResponse({"items": [{"snippet": {"title": "expected-account"}}]}),
            EmptyResponse(headers={"Location": "https://upload.example/session-1"}),
            TimeoutError("upload response lost"),
            EmptyResponse(status_code=308),
            TimeoutError("upload response lost again"),
            EmptyResponse(status_code=308),
        ]
    )

    result = YouTubePublisher(
        transport,
        max_resume_attempts=2,
        sleep=lambda _seconds: None,
    ).publish(_request(tmp_path, platform="youtube", metadata={"title": "title"}))

    assert result.status == "failed"
    assert result.evidence["retry_safe"] is False
    assert len([call for call in transport.calls if call[0] == "PUT"]) == 4
    assert transport.responses == []


def test_youtube_repeated_5xx_zero_progress_reconciliation_is_bounded(
    tmp_path: Path,
) -> None:
    from codex_media_ads.publishing.api_adapters import YouTubePublisher

    transport = QueueTransport(
        [
            FakeResponse({"items": [{"snippet": {"title": "expected-account"}}]}),
            EmptyResponse(headers={"Location": "https://upload.example/session-1"}),
            FakeResponse(status_code=503, text="unavailable"),
            EmptyResponse(status_code=308),
            FakeResponse(status_code=503, text="unavailable"),
            EmptyResponse(status_code=308),
        ]
    )

    result = YouTubePublisher(
        transport,
        max_resume_attempts=2,
        sleep=lambda _seconds: None,
    ).publish(_request(tmp_path, platform="youtube", metadata={"title": "title"}))

    assert result.status == "failed"
    assert result.evidence["retry_safe"] is False
    assert len([call for call in transport.calls if call[0] == "PUT"]) == 4
    assert transport.responses == []


@pytest.mark.parametrize(
    "final_response",
    [
        TruncatedSuccessResponse(status_code=201, text="{"),
        ResponseStreamLostError("response stream ended early"),
    ],
)
def test_youtube_final_response_body_loss_is_ambiguous_unknown(
    tmp_path: Path, final_response: FakeResponse | BaseException
) -> None:
    from codex_media_ads.publishing.api_adapters import YouTubePublisher

    transport = QueueTransport(
        [
            FakeResponse({"items": [{"snippet": {"title": "expected-account"}}]}),
            EmptyResponse(headers={"Location": "https://upload.example/session-1"}),
            final_response,
        ]
    )

    result = YouTubePublisher(transport, max_resume_attempts=1).publish(
        _request(tmp_path, platform="youtube", metadata={"title": "title"})
    )

    assert result.status == "unknown"
    assert result.error_category == "ambiguous_submit"
    assert result.evidence["retry_safe"] is False


@pytest.mark.parametrize("chunk_size", [0, 1, 256 * 1024 + 1])
def test_youtube_rejects_invalid_nonfinal_chunk_size(chunk_size: int) -> None:
    from codex_media_ads.publishing.api_adapters import YouTubePublisher

    with pytest.raises(ValueError, match="256 KiB"):
        YouTubePublisher(QueueTransport([]), chunk_size=chunk_size)


def test_youtube_accepts_256_kib_chunk_size() -> None:
    from codex_media_ads.publishing.api_adapters import YouTubePublisher

    assert YouTubePublisher(QueueTransport([]), chunk_size=256 * 1024).chunk_size == 256 * 1024


def test_x_uploads_chunks_polls_and_creates_post_with_disclosures(
    tmp_path: Path,
) -> None:
    from codex_media_ads.publishing.api_adapters import XPublisher

    transport = QueueTransport(
        [
            FakeResponse(
                {"data": {"id": "user-1", "username": "expected-account"}}
            ),
            FakeResponse({"data": {"id": "media-1"}}),
            FakeResponse({}),
            FakeResponse({}),
            FakeResponse({}),
            FakeResponse(
                {
                    "data": {
                        "id": "media-1",
                        "processing_info": {"state": "pending", "check_after_secs": 0},
                    }
                }
            ),
            FakeResponse(
                {
                    "data": {
                        "id": "media-1",
                        "processing_info": {"state": "succeeded"},
                    }
                }
            ),
            FakeResponse({"data": {"id": "post-1", "text": "A caption"}}),
        ]
    )
    request = _request(
        tmp_path,
        platform="x",
        metadata={
            "caption": "A caption",
            "made_with_ai": True,
            "paid_partnership": True,
        },
    )
    adapter = XPublisher(
        transport,
        chunk_size=4,
        sleep=lambda _seconds: None,
    )

    result = adapter.publish(request)

    assert result.status == "published"
    assert result.platform_id == "post-1"
    append_calls = [
        call for call in transport.calls if call[2].get("data", {}).get("command") == "APPEND"
    ]
    assert [call[2]["data"]["segment_index"] for call in append_calls] == [0, 1, 2]
    post_body = transport.calls[-1][2]["json"]
    assert post_body == {
        "text": "A caption",
        "media": {"media_ids": ["media-1"]},
        "made_with_ai": True,
        "paid_partnership": True,
    }
    assert all(
        str(call[2]["headers"]["Authorization"]).startswith("OAuth ")
        for call in transport.calls
    )
    assert all(
        "Bearer " not in str(call[2]["headers"]["Authorization"])
        for call in transport.calls
    )


def test_x_oauth1_header_contains_user_context_contract(tmp_path: Path) -> None:
    from codex_media_ads.publishing.api_adapters import XPublisher

    transport = QueueTransport(
        [FakeResponse({"screen_name": "expected-account", "id_str": "user-1"})]
    )
    adapter = XPublisher(
        transport,
        nonce_factory=lambda: "fixed-nonce",
        timestamp_factory=lambda: 1_700_000_000,
    )

    probe = adapter.probe_auth(_request(tmp_path, platform="x").account)

    assert probe.authenticated is True
    method, url, kwargs = transport.calls[0]
    authorization = str(kwargs["headers"]["Authorization"])
    assert method == "GET"
    assert url.endswith("/1.1/account/verify_credentials.json")
    assert authorization.startswith("OAuth ")
    assert 'oauth_consumer_key="test-consumer-key"' in authorization
    assert 'oauth_token="top-secret-token"' in authorization
    assert 'oauth_nonce="fixed-nonce"' in authorization
    assert 'oauth_signature_method="HMAC-SHA1"' in authorization
    assert "top-secret-access-secret" not in authorization
    assert "top-secret-consumer-secret" not in authorization


def test_x_oauth1_signer_matches_reference_hmac_sha1_vector() -> None:
    from codex_media_ads.publishing.api_adapters import _oauth1_authorization

    authorization = _oauth1_authorization(
        method="GET",
        url="http://photos.example.net/photos",
        consumer_key="dpf43f3p2l4k3l03",
        consumer_secret="kd94hf93k423kf44",
        access_token="nnch734d00sl2jdk",
        access_token_secret="pfkkdhi9sl3r4s00",
        nonce="kllo9940pd9333jh",
        timestamp=1_191_242_096,
        query={"file": "vacation.jpg", "size": "original"},
    )

    assert 'oauth_signature="tR3%2BTy81lMeYAr%2FFid0kMTYa%2FWM%3D"' in authorization


def test_x_tweet_timeout_is_ambiguous_unknown(tmp_path: Path) -> None:
    from codex_media_ads.publishing.api_adapters import XPublisher

    transport = QueueTransport(
        [
            FakeResponse({"screen_name": "expected-account", "id_str": "user-1"}),
            FakeResponse({"media_id_string": "media-1"}),
            EmptyResponse(status_code=204),
            FakeResponse({"media_id_string": "media-1"}),
            ConnectionError("oauth_token_secret=must-not-leak"),
        ]
    )

    result = XPublisher(transport, chunk_size=20).publish(
        _request(tmp_path, platform="x")
    )

    assert result.status == "unknown"
    assert result.error_category == "ambiguous_submit"
    assert result.evidence["retry_safe"] is False
    assert "must-not-leak" not in result.model_dump_json()


@pytest.mark.parametrize(
    "final_response",
    [
        TruncatedSuccessResponse(status_code=201, text="{"),
        ResponseStreamLostError("response stream ended early"),
    ],
)
def test_x_final_response_body_loss_is_ambiguous_unknown(
    tmp_path: Path, final_response: FakeResponse | BaseException
) -> None:
    from codex_media_ads.publishing.api_adapters import XPublisher

    transport = QueueTransport(
        [
            FakeResponse({"screen_name": "expected-account"}),
            FakeResponse({"media_id_string": "media-1"}),
            EmptyResponse(status_code=204),
            FakeResponse({"media_id_string": "media-1"}),
            final_response,
        ]
    )

    result = XPublisher(transport, chunk_size=20).publish(
        _request(tmp_path, platform="x")
    )

    assert result.status == "unknown"
    assert result.error_category == "ambiguous_submit"
    assert result.evidence["retry_safe"] is False


@pytest.mark.parametrize("publisher_name", ["youtube", "x"])
def test_large_video_publishers_do_not_use_path_read_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, publisher_name: str
) -> None:
    from codex_media_ads.publishing.api_adapters import YouTubePublisher, XPublisher

    def forbidden_read_bytes(_path: Path) -> bytes:
        raise AssertionError("whole-file read is forbidden")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)
    if publisher_name == "youtube":
        transport = QueueTransport(
            [
                FakeResponse({"items": [{"snippet": {"title": "expected-account"}}]}),
                EmptyResponse(headers={"Location": "https://upload.example/session"}),
                FakeResponse({"id": "video-1", "status": {"privacyStatus": "private"}}, status_code=201),
            ]
        )
        result = YouTubePublisher(transport).publish(
            _request(tmp_path, platform="youtube", metadata={"title": "title"})
        )
    else:
        transport = QueueTransport(
            [
                FakeResponse({"screen_name": "expected-account"}),
                FakeResponse({"media_id_string": "media-1"}),
                EmptyResponse(status_code=204),
                FakeResponse({"media_id_string": "media-1"}),
                FakeResponse({"data": {"id": "post-1"}}),
            ]
        )
        result = XPublisher(transport, chunk_size=20).publish(
            _request(tmp_path, platform="x")
        )

    assert result.status in {"submitted", "published"}


def test_x_accepts_empty_204_append_responses(tmp_path: Path) -> None:
    from codex_media_ads.publishing.api_adapters import XPublisher

    transport = QueueTransport(
        [
            FakeResponse({"data": {"id": "user-1", "username": "expected-account"}}),
            FakeResponse({"media_id_string": "media-1"}),
            EmptyResponse(status_code=204),
            FakeResponse({"media_id_string": "media-1"}),
            FakeResponse({"data": {"id": "post-1"}}),
        ]
    )

    result = XPublisher(transport, chunk_size=20).publish(
        _request(tmp_path, platform="x")
    )

    assert result.status == "published"
    assert result.platform_id == "post-1"


def test_dry_run_probes_identity_but_never_mutates_remote_state(tmp_path: Path) -> None:
    from codex_media_ads.publishing.api_adapters import XPublisher

    transport = QueueTransport(
        [
            FakeResponse(
                {"data": {"id": "user-1", "username": "expected-account"}}
            )
        ]
    )

    result = XPublisher(transport).publish(
        _request(tmp_path, platform="x", dry_run=True)
    )

    assert result.status == "skipped"
    assert result.evidence["dry_run"] is True
    assert [method for method, _url, _kwargs in transport.calls] == ["GET"]


def test_identity_mismatch_blocks_before_upload(tmp_path: Path) -> None:
    from codex_media_ads.publishing.api_adapters import MetaPublisher

    transport = QueueTransport(
        [FakeResponse({"id": "ig-destination-1", "username": "wrong-account"})]
    )

    result = MetaPublisher("instagram", transport).publish(
        _request(tmp_path, platform="instagram")
    )

    assert result.status == "blocked"
    assert result.error_category == "identity_mismatch"
    assert [method for method, _url, _kwargs in transport.calls] == ["GET"]


def test_unauthorized_identity_probe_blocks_before_upload(tmp_path: Path) -> None:
    from codex_media_ads.publishing.api_adapters import XPublisher

    transport = QueueTransport(
        [FakeResponse({"error": "unauthorized"}, status_code=401, text="unauthorized")]
    )

    result = XPublisher(transport).publish(_request(tmp_path, platform="x"))

    assert result.status == "blocked"
    assert result.error_category == "authentication"
    assert result.evidence["upload_started"] is False
    assert [method for method, _url, _kwargs in transport.calls] == ["GET"]


def test_secret_file_must_be_owner_only(tmp_path: Path) -> None:
    from codex_media_ads.publishing.api_adapters import XPublisher

    secret_file = _secret_file(tmp_path)
    secret_file.chmod(0o644)
    transport = QueueTransport([])

    result = XPublisher(transport).publish(
        _request(tmp_path, platform="x", secret_file=secret_file)
    )

    assert result.status == "blocked"
    assert result.error_category == "configuration"
    assert "owner-only" in result.detail
    assert transport.calls == []


def test_api_errors_redact_tokens_after_upload_has_started(tmp_path: Path) -> None:
    from codex_media_ads.publishing.api_adapters import XPublisher

    token = "top-secret-token"
    transport = QueueTransport(
        [
            FakeResponse(
                {"data": {"id": "user-1", "username": "expected-account"}}
            ),
            FakeResponse({"data": {"id": "media-1"}}),
            FakeResponse(
                {"error": {"access_token": token}},
                status_code=500,
                text=f'access_token="{token}"',
            ),
        ]
    )

    result = XPublisher(transport, chunk_size=20).publish(
        _request(tmp_path, platform="x")
    )

    assert result.status == "failed"
    assert result.evidence["upload_started"] is True
    assert result.evidence["retry_safe"] is False
    assert token not in result.model_dump_json()


def test_processing_polling_is_bounded(tmp_path: Path) -> None:
    from codex_media_ads.publishing.api_adapters import XPublisher

    pending = FakeResponse(
        {
            "data": {
                "id": "media-1",
                "processing_info": {"state": "pending", "check_after_secs": 0},
            }
        }
    )
    transport = QueueTransport(
        [
            FakeResponse(
                {"data": {"id": "user-1", "username": "expected-account"}}
            ),
            FakeResponse({"data": {"id": "media-1"}}),
            FakeResponse({}),
            FakeResponse({"data": {"id": "media-1", "processing_info": {"state": "pending"}}}),
            pending,
            pending,
        ]
    )

    result = XPublisher(
        transport,
        chunk_size=20,
        max_poll_attempts=2,
        sleep=lambda _seconds: None,
    ).publish(_request(tmp_path, platform="x"))

    assert result.status == "failed"
    assert result.evidence["upload_started"] is True
    status_calls = [
        call
        for call in transport.calls
        if call[0] == "GET" and call[2].get("params", {}).get("command") == "STATUS"
    ]
    assert len(status_calls) == 2
