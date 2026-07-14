from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, get_args

from pydantic import BaseModel, Field

from .models import CampaignManifest, Destination


POLICY_PATH = Path(__file__).with_name("policies") / "platforms.v1.json"
DESTINATIONS: frozenset[str] = frozenset(get_args(Destination))
FILENAME_SLUG = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*\.[A-Za-z0-9]+$",
    re.IGNORECASE,
)


class MetadataPack(BaseModel):
    policy_version: str = "platforms.v1"
    platform: str
    title: str = ""
    caption: str = ""
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    alt_text: str = ""
    visibility: str = "public"
    made_for_kids: bool = False
    contains_synthetic_media: bool = False
    paid_partnership: bool = False


def _normalize(value: object) -> str:
    return " ".join(str(value).split())


def _normalize_list(values: object) -> list[str]:
    if not isinstance(values, list):
        raise ValueError("metadata list fields must be lists")
    return [normalized for item in values if (normalized := _normalize(item))]


def _deduplicate(values: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(value)
    return unique


def _load_policies() -> dict[str, Any]:
    policies = json.loads(POLICY_PATH.read_text())
    if not isinstance(policies, dict) or not isinstance(policies.get("version"), str):
        raise ValueError("platform policy file is invalid")
    platform_keys = {
        key for key, value in policies.items() if isinstance(value, dict)
    }
    if platform_keys != DESTINATIONS:
        raise ValueError(
            "platform keys must be exactly: "
            + ", ".join(sorted(DESTINATIONS))
        )
    return policies


def _campaign_value(campaign: CampaignManifest, field: str, default: object) -> object:
    return getattr(campaign, field, default)


def _contains_phrase(copy: str, phrase: str) -> bool:
    return re.search(
        rf"(?<!\w){re.escape(phrase)}(?!\w)", copy, re.IGNORECASE
    ) is not None


def _append_required_calls_to_action(
    campaign: CampaignManifest, platform: str, metadata: dict[str, object]
) -> None:
    target = "description" if platform == "youtube" else "caption"
    copy = str(metadata[target])
    for call_to_action in _normalize_list(campaign.calls_to_action):
        if not _contains_phrase(copy, call_to_action):
            copy = f"{copy} {call_to_action}".strip()
    metadata[target] = copy


def _validate_limits(pack: MetadataPack, limits: dict[str, object]) -> None:
    field_limits = {
        "caption_max": len(pack.caption),
        "hashtags_max": len(pack.hashtags),
        "title_max": len(pack.title),
        "description_max": len(pack.description),
        "tags_chars_max": len(",".join(pack.tags)),
    }
    for limit_name, actual in field_limits.items():
        configured = limits.get(limit_name)
        if isinstance(configured, int) and actual > configured:
            raise ValueError(f"{limit_name} exceeded: {actual} > {configured}")


def optimize_for_platform(
    campaign: CampaignManifest, platform: str
) -> MetadataPack:
    policies = _load_policies()
    limits = policies.get(platform)
    if not isinstance(limits, dict):
        raise ValueError(f"unsupported platform: {platform}")

    metadata: dict[str, object] = {
        "policy_version": policies["version"],
        "platform": platform,
        "title": _campaign_value(campaign, "title", campaign.offer),
        "caption": _campaign_value(campaign, "caption", campaign.narration),
        "description": _campaign_value(campaign, "description", campaign.narration),
        "hashtags": _campaign_value(campaign, "hashtags", []),
        "tags": _campaign_value(campaign, "tags", []),
        "alt_text": _campaign_value(campaign, "alt_text", ""),
        "visibility": _campaign_value(campaign, "visibility", "public"),
        "made_for_kids": _campaign_value(campaign, "made_for_kids", False),
        "contains_synthetic_media": _campaign_value(
            campaign, "synthetic_media", False
        ),
        "paid_partnership": _campaign_value(campaign, "paid_partnership", False),
    }
    overrides = _campaign_value(campaign, "platform_overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("platform_overrides must be a mapping")
    platform_overrides = overrides.get(platform, {})
    if not isinstance(platform_overrides, dict):
        raise ValueError(f"platform override for {platform} must be a mapping")
    metadata.update(platform_overrides)
    metadata["policy_version"] = policies["version"]
    metadata["platform"] = platform

    for field in ("title", "caption", "description", "alt_text", "visibility"):
        metadata[field] = _normalize(metadata[field])
    metadata["hashtags"] = _deduplicate(_normalize_list(metadata["hashtags"]))
    metadata["tags"] = _deduplicate(_normalize_list(metadata["tags"]))

    if FILENAME_SLUG.fullmatch(str(metadata["caption"])):
        raise ValueError("caption cannot be a filename slug")

    _append_required_calls_to_action(campaign, platform, metadata)
    pack = MetadataPack.model_validate(metadata)
    _validate_limits(pack, limits)
    return pack
