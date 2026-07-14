from __future__ import annotations

from collections.abc import Mapping

from ..models import AccountConfig
from .base import PublisherAdapter


class RouteUnavailableError(RuntimeError):
    pass


class PublisherRouter:
    """Choose one route before publishing; never fail over after selection."""

    def __init__(
        self,
        *,
        api_adapters: Mapping[str, PublisherAdapter],
        browser_adapters: Mapping[str, PublisherAdapter],
    ) -> None:
        self.api_adapters = dict(api_adapters)
        self.browser_adapters = dict(browser_adapters)

    @staticmethod
    def _require(
        adapters: Mapping[str, PublisherAdapter],
        platform: str,
        route: str,
        account: AccountConfig,
    ) -> PublisherAdapter:
        adapter = adapters.get(platform)
        if adapter is None:
            raise RouteUnavailableError(
                f"{route} route is not configured for {platform}"
            )
        try:
            probe = adapter.probe_auth(account)
        except Exception as exc:
            raise RouteUnavailableError(
                f"{route} authentication probe failed for {platform}"
            ) from exc
        if not probe.authenticated:
            detail = f": {probe.detail}" if probe.detail else ""
            raise RouteUnavailableError(
                f"{route} authentication probe failed for {platform}{detail}"
            )
        return adapter

    def select(self, account: AccountConfig, platform: str) -> PublisherAdapter:
        mode = account.mode.strip().casefold()
        if mode == "api":
            return self._require(
                self.api_adapters, platform, "api", account
            )
        if mode == "browser":
            return self._require(
                self.browser_adapters, platform, "browser", account
            )
        if mode != "auto":
            raise ValueError(f"invalid account mode: {account.mode}")

        api = self.api_adapters.get(platform)
        if api is not None:
            try:
                if api.probe_auth(account).authenticated:
                    return api
            except Exception:
                # Selection has not begun a publish, so an unavailable API probe
                # is equivalent to a failed probe in auto mode.
                pass
        return self._require(
            self.browser_adapters, platform, "browser", account
        )


def select_adapter(
    account: AccountConfig,
    platform: str,
    *,
    api_adapters: Mapping[str, PublisherAdapter],
    browser_adapters: Mapping[str, PublisherAdapter],
) -> PublisherAdapter:
    return PublisherRouter(
        api_adapters=api_adapters,
        browser_adapters=browser_adapters,
    ).select(account, platform)
