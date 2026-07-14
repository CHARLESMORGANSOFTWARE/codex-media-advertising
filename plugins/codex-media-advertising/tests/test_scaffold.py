import json
import os
from pathlib import Path
import subprocess
import sys

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9-3.10 test runners
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[3]
PLUGIN = ROOT / "plugins" / "codex-media-advertising"


def test_marketplace_points_to_nested_plugin():
    data = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
    entry = next(item for item in data["plugins"] if item["name"] == "codex-media-advertising")
    assert entry["source"]["path"] == "./plugins/codex-media-advertising"
    assert entry["policy"] == {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}


def test_manifest_discovers_skills():
    data = json.loads((PLUGIN / ".codex-plugin/plugin.json").read_text())
    assert data["name"] == "codex-media-advertising"
    assert data["skills"] == "./skills/"


def test_skills_discovery_path_survives_fresh_checkout():
    assert (PLUGIN / "skills" / ".gitkeep").is_file()


def test_checkout_contains_no_private_state_directories():
    forbidden = {"secrets", "browser-profiles", "generated", "receipts", "queue", "logs"}
    assert not forbidden.intersection(path.name for path in PLUGIN.iterdir())


def test_manifest_describes_public_plugin_capabilities():
    data = json.loads((PLUGIN / ".codex-plugin/plugin.json").read_text())
    interface = data["interface"]

    assert interface["displayName"] == "Codex Media & Advertising"
    assert interface["category"] == "Productivity"
    assert interface["capabilities"] == ["Interactive", "Write", "Automation"]
    assert "websiteURL" not in interface
    assert "privacyPolicyURL" not in interface


def test_python_package_metadata_matches_contract():
    data = tomllib.loads((PLUGIN / "pyproject.toml").read_text())
    project = data["project"]

    assert project["name"] == "codex-media-advertising"
    assert project["version"] == "0.1.0"
    assert project["requires-python"] == ">=3.11"
    assert project["scripts"]["codex-media-ads"] == "codex_media_ads.cli:main"
    assert data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/codex_media_ads"
    ]


def test_cli_prints_machine_readable_version():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PLUGIN / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from codex_media_ads.cli import main; raise SystemExit(main(['--version']))",
        ],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "name": "codex-media-advertising",
        "version": "0.1.0",
    }


def test_private_runtime_state_is_ignored():
    ignored = set((ROOT / ".gitignore").read_text().splitlines())

    assert {
        "secrets/",
        "browser-profiles/",
        "generated/",
        "receipts/",
        "queue/",
        "logs/",
    } <= ignored
