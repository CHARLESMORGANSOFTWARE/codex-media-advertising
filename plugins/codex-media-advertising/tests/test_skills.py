from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PLUGIN = ROOT / "plugins" / "codex-media-advertising"
SKILL_NAMES = (
    "media-onboarding",
    "media-campaign",
    "media-automation",
    "media-publishing",
    "media-operations",
)
COMMON_RULE = (
    "Do not switch accounts to make a publish succeed. Treat an identity mismatch "
    "as blocked work. Do not infer publication from a queued state or process exit; "
    "inspect the destination receipt and report its exact status, ID, URL, and path."
)


def _frontmatter(text: str) -> dict[str, str]:
    body = text.split("---", 2)[1]
    values: dict[str, str] = {}
    for line in body.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip('"')
    return values


def _yaml_scalar(text: str, key: str) -> str:
    match = re.search(rf"^\s+{re.escape(key)}:\s*[\"']?([^\"'\n]+)", text, re.MULTILINE)
    assert match, f"missing {key} in generated metadata"
    return match.group(1).strip()


def test_all_skills_route_live_work_through_cli() -> None:
    for name in SKILL_NAMES:
        text = (PLUGIN / "skills" / name / "SKILL.md").read_text()
        assert "codex-media-ads" in text
        assert COMMON_RULE in text


def test_skills_have_valid_frontmatter_and_expected_names() -> None:
    for name in SKILL_NAMES:
        text = (PLUGIN / "skills" / name / "SKILL.md").read_text()
        assert re.match(r"^---\nname: [a-z0-9-]+\ndescription: .+\n---\n", text)
        frontmatter = _frontmatter(text)
        assert frontmatter["name"] == name
        assert frontmatter["description"].startswith("Use when")


def test_each_skill_has_generated_ui_metadata() -> None:
    for name in SKILL_NAMES:
        metadata = (PLUGIN / "skills" / name / "agents" / "openai.yaml").read_text()
        short_description = _yaml_scalar(metadata, "short_description")
        default_prompt = _yaml_scalar(metadata, "default_prompt")
        assert 25 <= len(short_description) <= 64
        assert f"${name}" in default_prompt


def test_skills_name_decisive_artifacts_and_stop_rules() -> None:
    expectations = {
        "media-onboarding": ("setup", "background automation"),
        "media-campaign": ("campaign validate", "rights"),
        "media-automation": ("automation install", "daily cap"),
        "media-publishing": ("receipts show", "ambiguous"),
        "media-operations": ("pause", "queue claim"),
    }
    for name, required_phrases in expectations.items():
        text = (PLUGIN / "skills" / name / "SKILL.md").read_text().casefold()
        for phrase in required_phrases:
            assert phrase.casefold() in text


def test_user_documentation_covers_install_auth_automation_and_platforms() -> None:
    docs = {
        name: (PLUGIN / "docs" / name).read_text().casefold()
        for name in (
            "installation.md",
            "authentication.md",
            "automations.md",
            "platform-notes.md",
        )
    }
    assert "marketplace" in docs["installation.md"]
    assert "new codex task" in docs["installation.md"]
    assert "meta" in docs["authentication.md"]
    assert "google" in docs["authentication.md"]
    assert "x" in docs["authentication.md"]
    assert "launchagent" in docs["automations.md"]
    for platform in ("instagram", "tiktok", "youtube", "x", "facebook", "threads"):
        assert platform in docs["platform-notes.md"]
    assert "private-only" in docs["platform-notes.md"]
    assert "unknown" in docs["platform-notes.md"]


def test_installation_docs_use_public_marketplace_commands_and_accurate_extras() -> None:
    text = (PLUGIN / "docs" / "installation.md").read_text()

    assert "https://github.com/CHARLESMORGANSOFTWARE/codex-media-advertising.git" in text
    assert 'codex plugin marketplace add "$PWD"' in text
    assert "codex plugin add codex-media-advertising@personal" in text
    assert "does not install the optional `browser` or `youtube` Python extras" in text
    assert "optional local dependencies available on the machine" not in text
