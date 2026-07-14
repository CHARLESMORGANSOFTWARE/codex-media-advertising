from __future__ import annotations

from dataclasses import dataclass

import pytest

from codex_media_ads.models import AccountConfig, PublishResult, PublishStatus
from codex_media_ads.publishing.base import ProbeResult


@dataclass
class FakeAdapter:
    platform: str
    probe_result: ProbeResult
    publish_count: int = 0

    def probe_auth(self, _account: AccountConfig) -> ProbeResult:
        return self.probe_result

    def publish(self, _request: object) -> PublishResult:
        self.publish_count += 1
        return PublishResult(status=PublishStatus.FAILED)


class ExplodingProbeAdapter(FakeAdapter):
    def probe_auth(self, _account: AccountConfig) -> ProbeResult:
        raise ConnectionError("probe unavailable")


def _account(mode: str) -> AccountConfig:
    return AccountConfig(
        account_id="account-1",
        expected_identity="expected",
        mode=mode,
    )


def _adapter(platform: str, authenticated: bool) -> FakeAdapter:
    return FakeAdapter(
        platform,
        ProbeResult(authenticated=authenticated, observed_identity="expected"),
    )


def test_router_prefers_api_when_probe_passes() -> None:
    from codex_media_ads.publishing.router import PublisherRouter

    api_adapter = _adapter("youtube", True)
    browser_adapter = _adapter("youtube", True)
    router = PublisherRouter(
        api_adapters={"youtube": api_adapter},
        browser_adapters={"youtube": browser_adapter},
    )

    assert router.select(_account("auto"), "youtube") is api_adapter


def test_auto_falls_back_to_browser_only_when_api_probe_fails() -> None:
    from codex_media_ads.publishing.router import PublisherRouter

    api_adapter = _adapter("youtube", False)
    browser_adapter = _adapter("youtube", True)
    router = PublisherRouter(
        api_adapters={"youtube": api_adapter},
        browser_adapters={"youtube": browser_adapter},
    )

    assert router.select(_account("auto"), "youtube") is browser_adapter


def test_auto_falls_back_when_api_probe_raises_before_selection() -> None:
    from codex_media_ads.publishing.router import PublisherRouter

    api_adapter = ExplodingProbeAdapter(
        "youtube", ProbeResult(authenticated=False)
    )
    browser_adapter = _adapter("youtube", True)
    router = PublisherRouter(
        api_adapters={"youtube": api_adapter},
        browser_adapters={"youtube": browser_adapter},
    )

    assert router.select(_account("auto"), "youtube") is browser_adapter


@pytest.mark.parametrize(
    ("mode", "missing_route"),
    [("api", "api"), ("browser", "browser")],
)
def test_explicit_mode_requires_its_route(mode: str, missing_route: str) -> None:
    from codex_media_ads.publishing.router import PublisherRouter, RouteUnavailableError

    api = {} if missing_route == "api" else {"youtube": _adapter("youtube", True)}
    browser = (
        {} if missing_route == "browser" else {"youtube": _adapter("youtube", True)}
    )
    router = PublisherRouter(api_adapters=api, browser_adapters=browser)

    with pytest.raises(RouteUnavailableError, match=missing_route):
        router.select(_account(mode), "youtube")


def test_explicit_route_must_pass_probe() -> None:
    from codex_media_ads.publishing.router import PublisherRouter, RouteUnavailableError

    router = PublisherRouter(
        api_adapters={"x": _adapter("x", False)}, browser_adapters={}
    )

    with pytest.raises(RouteUnavailableError, match="authentication probe"):
        router.select(_account("api"), "x")


def test_explicit_route_wraps_probe_exception_as_unavailable() -> None:
    from codex_media_ads.publishing.router import PublisherRouter, RouteUnavailableError

    adapter = ExplodingProbeAdapter("x", ProbeResult(authenticated=False))
    router = PublisherRouter(api_adapters={"x": adapter}, browser_adapters={})

    with pytest.raises(RouteUnavailableError, match="authentication probe"):
        router.select(_account("api"), "x")


def test_api_failure_after_selection_never_invokes_browser() -> None:
    from codex_media_ads.publishing.router import PublisherRouter

    api_adapter = _adapter("x", True)
    browser_adapter = _adapter("x", True)
    router = PublisherRouter(
        api_adapters={"x": api_adapter}, browser_adapters={"x": browser_adapter}
    )

    selected = router.select(_account("auto"), "x")
    result = selected.publish(object())

    assert result.status == "failed"
    assert api_adapter.publish_count == 1
    assert browser_adapter.publish_count == 0


def test_select_adapter_function_exposes_explicit_selection() -> None:
    from codex_media_ads.publishing.router import select_adapter

    api_adapter = _adapter("youtube", True)
    browser_adapter = _adapter("youtube", True)

    selected = select_adapter(
        _account("browser"),
        "youtube",
        api_adapters={"youtube": api_adapter},
        browser_adapters={"youtube": browser_adapter},
    )

    assert selected is browser_adapter


def test_invalid_mode_is_rejected() -> None:
    from codex_media_ads.publishing.router import PublisherRouter

    router = PublisherRouter(api_adapters={}, browser_adapters={})

    with pytest.raises(ValueError, match="mode"):
        router.select(_account("magic"), "youtube")
