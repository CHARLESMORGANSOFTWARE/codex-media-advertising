import json
from pathlib import Path

import pytest

from codex_media_ads import optimization
from codex_media_ads.models import CampaignManifest
from codex_media_ads.optimization import optimize_for_platform


PLUGIN = Path(__file__).resolve().parents[1]


@pytest.fixture
def campaign() -> CampaignManifest:
    data = json.loads((PLUGIN / "examples" / "campaign.example.json").read_text())
    return CampaignManifest.model_validate(data)


def test_tiktok_rejects_filename_slug_as_caption(campaign):
    campaign.platform_overrides = {"tiktok": {"caption": "my-video-final-v2.mp4"}}
    with pytest.raises(ValueError, match="filename slug"):
        optimize_for_platform(campaign, "tiktok")


@pytest.mark.parametrize("extension", ["mpeg", "heic", "bmp", "tiff"])
def test_tiktok_rejects_filename_slug_regardless_of_extension(campaign, extension):
    campaign.platform_overrides = {
        "tiktok": {"caption": f"my-video-final-v2.{extension}"}
    }

    with pytest.raises(ValueError, match="filename slug"):
        optimize_for_platform(campaign, "tiktok")


def test_url_caption_is_not_rejected_as_filename_slug(campaign):
    campaign.platform_overrides = {
        "tiktok": {"caption": "https://example.com/my-video-final-v2.mpeg"}
    }

    pack = optimize_for_platform(campaign, "tiktok")

    assert pack.caption == (
        "https://example.com/my-video-final-v2.mpeg Read the guide"
    )


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


def test_call_to_action_substring_does_not_count_as_present(campaign):
    campaign.calls_to_action = ["Go"]
    campaign.platform_overrides = {
        "instagram": {"caption": "Our logo launches today"}
    }

    pack = optimize_for_platform(campaign, "instagram")

    assert pack.caption == "Our logo launches today Go"


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


def test_policy_rejects_extra_dictionary_section(
    campaign, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    policies = json.loads(
        (PLUGIN / "src/codex_media_ads/policies/platforms.v1.json").read_text()
    )
    policies["defaults"] = {"caption_max": 100}
    policy_path = tmp_path / "platforms.v1.json"
    policy_path.write_text(json.dumps(policies))
    monkeypatch.setattr(optimization, "POLICY_PATH", policy_path)

    with pytest.raises(ValueError, match="platform keys"):
        optimize_for_platform(campaign, "instagram")
