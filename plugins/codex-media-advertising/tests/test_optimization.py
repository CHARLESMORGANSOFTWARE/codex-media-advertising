import json
from pathlib import Path

import pytest
from pydantic import Field

from codex_media_ads.models import CampaignManifest
from codex_media_ads.optimization import optimize_for_platform


PLUGIN = Path(__file__).resolve().parents[1]


class OptimizationCampaign(CampaignManifest):
    platform_overrides: dict[str, dict[str, object]] = Field(default_factory=dict)
    hashtags: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    synthetic_media: bool = False


@pytest.fixture
def campaign() -> OptimizationCampaign:
    data = json.loads((PLUGIN / "examples" / "campaign.example.json").read_text())
    return OptimizationCampaign.model_validate(data)


def test_tiktok_rejects_filename_slug_as_caption(campaign):
    campaign.platform_overrides = {"tiktok": {"caption": "my-video-final-v2.mp4"}}
    with pytest.raises(ValueError, match="filename slug"):
        optimize_for_platform(campaign, "tiktok")


def test_duplicate_hashtags_are_removed_case_insensitively(campaign):
    campaign.hashtags = ["#SmallBusiness", "#smallbusiness", "#Launch"]
    pack = optimize_for_platform(campaign, "instagram")
    assert pack.hashtags == ["#SmallBusiness", "#Launch"]


def test_youtube_pack_preserves_disclosure_and_audience(campaign):
    campaign.synthetic_media = True
    pack = optimize_for_platform(campaign, "youtube")
    assert pack.contains_synthetic_media is True
    assert pack.made_for_kids is False


def test_platform_override_is_normalized_and_retains_required_call_to_action(campaign):
    campaign.platform_overrides = {
        "instagram": {"caption": "  A focused   launch\nmessage  "}
    }

    pack = optimize_for_platform(campaign, "instagram")

    assert pack.caption == "A focused launch message Read the guide"
    assert pack.policy_version == "platforms.v1"


def test_platform_override_cannot_replace_policy_identity(campaign):
    campaign.platform_overrides = {
        "instagram": {"platform": "tiktok", "policy_version": "forged"}
    }

    pack = optimize_for_platform(campaign, "instagram")

    assert pack.platform == "instagram"
    assert pack.policy_version == "platforms.v1"


@pytest.mark.parametrize(
    ("platform", "field", "value", "message"),
    [
        ("instagram", "caption", "a" * 2201, "caption_max"),
        ("tiktok", "hashtags", [f"#tag{i}" for i in range(9)], "hashtags_max"),
        ("youtube", "title", "a" * 101, "title_max"),
        ("youtube", "description", "a" * 5001, "description_max"),
        ("youtube", "tags", ["a" * 501], "tags_chars_max"),
        ("x", "caption", "a" * 281, "caption_max"),
        ("facebook", "hashtags", [f"#tag{i}" for i in range(16)], "hashtags_max"),
        ("threads", "caption", "a" * 501, "caption_max"),
    ],
)
def test_configured_limits_are_validated(campaign, platform, field, value, message):
    campaign.platform_overrides = {platform: {field: value}}

    with pytest.raises(ValueError, match=message):
        optimize_for_platform(campaign, platform)


def test_unknown_platform_is_rejected(campaign):
    with pytest.raises(ValueError, match="unsupported platform"):
        optimize_for_platform(campaign, "myspace")
