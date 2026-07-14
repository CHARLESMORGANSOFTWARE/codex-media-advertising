import json
from pathlib import Path
import stat

import pytest
from pydantic import ValidationError

from codex_media_ads.config import PRIVATE_MODES, SECRET_FILE_MODE, redact, state_layout
from codex_media_ads.manifests import load_campaign
from codex_media_ads.models import (
    AccountConfig,
    CampaignManifest,
    PublishRequest,
    PublishResult,
    PublishStatus,
)


PLUGIN = Path(__file__).resolve().parents[1]


@pytest.fixture
def example_campaign() -> Path:
    return PLUGIN / "examples" / "campaign.example.json"


def test_manifest_rejects_embedded_secret(tmp_path: Path):
    path = tmp_path / "campaign.json"
    path.write_text(
        '{"schema_version":"1","campaign_id":"launch",'
        '"rights_confirmed":true,"secrets":{"token":"abc"}}'
    )
    with pytest.raises(ValueError, match="secret-bearing key"):
        load_campaign(path)


def test_state_layout_never_uses_checkout(tmp_path: Path):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    with pytest.raises(ValueError, match="outside the Git checkout"):
        state_layout(checkout / "runtime", checkout=checkout)


def test_state_layout_defaults_to_private_directory_under_runtime_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    layout = state_layout()

    expected_root = (home / ".codex-media-ads").resolve()
    assert layout["config"] == expected_root / "config"
    assert stat.S_IMODE(expected_root.stat().st_mode) == PRIVATE_MODES


def test_state_layout_creates_directories_with_private_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    root = tmp_path / "private-state"
    mkdir_calls: list[tuple[Path, int]] = []
    original_mkdir = Path.mkdir

    def record_mkdir(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if path == root or root in path.parents:
            mkdir_calls.append((path, mode))
        original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", record_mkdir)

    state_layout(root, checkout=checkout)

    first_mode_by_path = {path: mode for path, mode in reversed(mkdir_calls)}
    assert first_mode_by_path
    assert set(first_mode_by_path.values()) == {PRIVATE_MODES}


def test_valid_manifest_has_stable_content_id(example_campaign: Path):
    first = load_campaign(example_campaign)
    second = load_campaign(example_campaign)
    assert first.content_id == second.content_id
    assert set(first.destinations) == {
        "instagram",
        "tiktok",
        "youtube",
        "x",
        "facebook",
        "threads",
    }


def test_content_id_ignores_schedule_timestamps(example_campaign: Path, tmp_path: Path):
    data = json.loads(example_campaign.read_text())
    original = load_campaign(example_campaign)
    data["schedule"] = ["2030-02-03T04:05:06Z"]
    changed_path = tmp_path / "rescheduled.json"
    changed_path.write_text(json.dumps(data))

    assert load_campaign(changed_path).content_id == original.content_id


def test_manifest_rejects_unknown_destination(example_campaign: Path):
    data = json.loads(example_campaign.read_text())
    data["destinations"] = ["myspace"]

    with pytest.raises(ValidationError, match="destination"):
        CampaignManifest.model_validate(data)


def test_manifest_rejects_unknown_fields(example_campaign: Path):
    data = json.loads(example_campaign.read_text())
    data["surprise"] = True

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CampaignManifest.model_validate(data)


def test_load_campaign_accepts_typed_optimization_fields(
    example_campaign: Path, tmp_path: Path
):
    data = json.loads(example_campaign.read_text())
    data.update(
        {
            "platform_overrides": {
                "instagram": {"caption": "A platform-specific caption"}
            },
            "hashtags": ["#Launch"],
            "tags": ["launch"],
            "synthetic_media": True,
        }
    )
    path = tmp_path / "campaign.json"
    path.write_text(json.dumps(data))

    campaign = load_campaign(path)

    assert campaign.platform_overrides == {
        "instagram": {"caption": "A platform-specific caption"}
    }
    assert campaign.hashtags == ["#Launch"]
    assert campaign.tags == ["launch"]
    assert campaign.synthetic_media is True


def test_manifest_requires_confirmed_rights(example_campaign: Path):
    data = json.loads(example_campaign.read_text())
    data["rights_confirmed"] = False

    with pytest.raises(ValidationError, match="rights_confirmed"):
        CampaignManifest.model_validate(data)


def test_content_id_is_24_lowercase_hex_characters(example_campaign: Path):
    content_id = load_campaign(example_campaign).content_id

    assert len(content_id) == 24
    assert content_id == content_id.lower()
    int(content_id, 16)


def test_redact_hides_recursive_sensitive_values():
    value = {
        "safe": "visible",
        "nested": [{"access_token": "abc"}, {"authorization": "Bearer abc"}],
    }

    assert redact(value) == {
        "safe": "visible",
        "nested": [
            {"access_token": "[REDACTED]"},
            {"authorization": "[REDACTED]"},
        ],
    }


def test_state_layout_creates_only_private_directories(tmp_path: Path):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    root = tmp_path / "private-state"

    layout = state_layout(root, checkout=checkout)

    expected = {
        "config",
        "secrets",
        "browser-profiles",
        "campaigns",
        "generated",
        "queue/pending",
        "queue/claims",
        "queue/completed",
        "queue/failed",
        "receipts",
        "health",
        "logs",
    }
    assert set(layout) == expected
    assert all(path.is_dir() for path in layout.values())
    assert stat.S_IMODE(root.stat().st_mode) == PRIVATE_MODES
    assert all(stat.S_IMODE(path.stat().st_mode) == PRIVATE_MODES for path in layout.values())
    assert stat.S_IMODE((root / "queue").stat().st_mode) == PRIVATE_MODES
    assert SECRET_FILE_MODE == 0o600


def test_publish_status_values_are_the_shared_contract():
    assert {status.value for status in PublishStatus} == {
        "published",
        "submitted",
        "scheduled",
        "skipped",
        "blocked",
        "failed",
        "unknown",
    }


def test_shared_publish_contracts_validate_and_preserve_values(tmp_path: Path):
    account = AccountConfig(
        account_id="launch-account",
        expected_identity="brand@example.com",
        secret_file=tmp_path / "account.env",
    )
    request = PublishRequest(
        content_id="abc123",
        revision=1,
        platform="instagram",
        account=account,
        media_path=tmp_path / "creative.mp4",
        metadata={"caption": "Launch day"},
        idempotency_key="abc123:1:instagram",
    )
    result = PublishResult(status=PublishStatus.SUBMITTED)

    assert request.account.mode == "auto"
    assert request.dry_run is False
    assert result.model_dump() == {
        "status": PublishStatus.SUBMITTED,
        "platform_id": "",
        "post_url": "",
        "evidence": {},
        "error_category": "",
        "detail": "",
    }


def test_publish_request_requires_positive_revision(tmp_path: Path):
    account = AccountConfig(account_id="account", expected_identity="identity")

    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        PublishRequest(
            content_id="abc123",
            revision=0,
            platform="instagram",
            account=account,
            media_path=tmp_path / "creative.mp4",
            metadata={},
            idempotency_key="abc123:0:instagram",
        )


@pytest.mark.parametrize(
    "name",
    ["brand.example.json", "campaign.example.json", "schedule.example.json"],
)
def test_example_json_is_valid(name: str):
    data = json.loads((PLUGIN / "examples" / name).read_text())
    assert isinstance(data, dict)
