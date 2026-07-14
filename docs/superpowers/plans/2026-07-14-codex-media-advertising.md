# Codex Media and Advertising Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a public Codex marketplace repository whose installable plugin creates images, narration, optimized social videos, durable publishing queues, and unattended background posts for Instagram, TikTok, YouTube, X, Facebook, and Threads.

**Architecture:** The GitHub repository is a Codex team marketplace containing one nested plugin. The plugin combines five workflow skills with a Python CLI. The CLI owns schema validation, creative-provider orchestration, FFmpeg rendering, platform metadata, atomic queue claims, per-account pause state, adapters, receipts, setup, and macOS background jobs. All user state and credentials live outside the repository under `~/.codex-media-ads/`.

**Tech Stack:** Codex plugin and marketplace manifests, Python 3.11+, Pydantic 2, pytest, Requests, Playwright, Google API Python client, FFmpeg/ffprobe, Chrome CDP, macOS LaunchAgents, GitHub Actions.

## Global Constraints

- Repository name and plugin name: `codex-media-advertising`.
- License: Apache License 2.0, preserving attribution for adapted Telecodex code.
- Initial operating-system target: macOS 13 or newer; core schemas, queueing, receipts, rendering, and optimization remain portable.
- Supported destinations: `instagram`, `tiktok`, `youtube`, `x`, `facebook`, and `threads`.
- Default private state root: `~/.codex-media-ads/`; no runtime state may be written beneath the Git checkout.
- Never include account identifiers, credentials, browser profiles, customer media, generated media, queues, receipts, screenshots, or machine-specific source paths in Git.
- Account identity mismatch is a hard stop; never switch accounts automatically.
- Existing verified success receipts suppress duplicate live posts.
- Retry at most once, only for a classified transient failure with no ambiguous submission evidence.
- Pause a destination/account pair after two consecutive live failures until a successful probe or explicit resume.
- A process exit, queue state, or top-level `queued` value is not proof of publication; receipt evidence is authoritative.
- TikTok browser publishing remains the default unattended route. The official Content Posting API is an optional route because direct-post clients require app review and user-facing consent behavior.
- YouTube API uploads from unverified API projects may be private-only; setup must report this restriction and never silently claim a public upload.
- X API publishing is optional and credential-dependent; isolated Chrome/CDP remains a supported fallback.

Before executing commands, set portable repository and skill roots:

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
PLUGIN_CREATOR_ROOT="${CODEX_HOME:-$HOME/.codex}/skills/.system/plugin-creator"
SKILL_CREATOR_ROOT="${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator"
```

---

## File Map

The implementation creates these responsibility boundaries:

- `.agents/plugins/marketplace.json`: shareable Codex marketplace catalog.
- `plugins/codex-media-advertising/.codex-plugin/plugin.json`: plugin identity and UI metadata.
- `plugins/codex-media-advertising/src/codex_media_ads/models.py`: typed contracts and status enums.
- `config.py`: private state layout, JSON loading, permissions, and redaction.
- `manifests.py`: campaign validation and stable IDs.
- `optimization.py`: destination-specific metadata packs and policy versions.
- `creative/providers.py`: Codimage and narration provider interfaces.
- `creative/render.py`: FFmpeg master and destination variant rendering.
- `queueing/store.py`: atomic queue claims, leases, idempotency, and caps.
- `queueing/receipts.py`: append-only and latest receipt persistence.
- `publishing/base.py`: adapter protocol, error classification, and adapter registry.
- `publishing/chrome.py`: isolated Chrome profile clone lifecycle and CDP connection.
- `publishing/browser_adapters.py`: Instagram, Facebook, YouTube Studio, TikTok, X, and Threads browser flows.
- `publishing/api_adapters.py`: Meta, YouTube, and X API routes.
- `orchestrator.py`: build, enqueue, publish-next, retry, pause, and resume behavior.
- `setup.py`: dependency/auth checks and private configuration.
- `automation/launchd.py`: deterministic LaunchAgent generation.
- `cli.py`: stable command surface used by people, skills, and schedulers.
- `skills/*/SKILL.md`: Codex workflow routing.
- `tests/`: unit, contract, integration, installer, and packaging tests.

---

### Task 1: Marketplace and Python Package Scaffold

**Files:**
- Create: `.agents/plugins/marketplace.json`
- Create: `plugins/codex-media-advertising/.codex-plugin/plugin.json`
- Create: `plugins/codex-media-advertising/pyproject.toml`
- Create: `plugins/codex-media-advertising/src/codex_media_ads/__init__.py`
- Create: `plugins/codex-media-advertising/src/codex_media_ads/cli.py`
- Create: `plugins/codex-media-advertising/tests/test_scaffold.py`
- Create: `.gitignore`
- Create: `LICENSE`
- Create: `NOTICE`

**Interfaces:**
- Consumes: approved marketplace repository layout.
- Produces: importable `codex_media_ads` package and executable `codex-media-ads` command.

- [ ] **Step 1: Generate the validated plugin and marketplace skeleton**

Run from the plugin-creator skill directory:

```bash
python3 scripts/create_basic_plugin.py codex-media-advertising \
  --path "$REPO_ROOT/plugins" \
  --marketplace-path "$REPO_ROOT/.agents/plugins/marketplace.json" \
  --with-skills --with-scripts --with-assets --with-marketplace
```

Expected: the nested plugin manifest and repo marketplace entry are created with matching normalized names.

- [ ] **Step 2: Write the failing scaffold tests**

```python
import json
from pathlib import Path

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


def test_checkout_contains_no_private_state_directories():
    forbidden = {"secrets", "browser-profiles", "generated", "receipts", "queue", "logs"}
    assert not forbidden.intersection(path.name for path in PLUGIN.iterdir())
```

- [ ] **Step 3: Run the tests and observe the incomplete package failure**

Run:

```bash
python3 -m pytest plugins/codex-media-advertising/tests/test_scaffold.py -q
```

Expected: failure until manifest metadata, marketplace policy, package metadata, and exclusions match the contract.

- [ ] **Step 4: Complete package and manifest metadata**

Use this package contract in `pyproject.toml`:

```toml
[build-system]
requires = ["hatchling>=1.26"]
build-backend = "hatchling.build"

[project]
name = "codex-media-advertising"
version = "0.1.0"
description = "Local media creation and social publishing automation for Codex"
requires-python = ">=3.11"
license = {text = "Apache-2.0"}
dependencies = [
  "pydantic>=2.10,<3",
  "requests>=2.32,<3",
  "platformdirs>=4.3,<5",
]

[project.optional-dependencies]
browser = ["playwright>=1.54,<2"]
youtube = [
  "google-api-python-client>=2.170,<3",
  "google-auth-oauthlib>=1.2,<2",
]
test = ["pytest>=8.3,<9", "pytest-cov>=6,<7"]

[project.scripts]
codex-media-ads = "codex_media_ads.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["src/codex_media_ads"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

Create an initial CLI that is testable without side effects:

```python
from __future__ import annotations

import argparse
import json

__version__ = "0.1.0"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codex-media-ads")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        print(json.dumps({"name": "codex-media-advertising", "version": __version__}))
    return 0
```

Set the plugin display name to `Codex Media & Advertising`, category to `Productivity`, capabilities to `Interactive`, `Write`, and `Automation`, and provide no website/privacy URLs until real public URLs exist.

- [ ] **Step 5: Validate and commit the scaffold**

Run:

```bash
python3 -m pytest plugins/codex-media-advertising/tests/test_scaffold.py -q
python3 "$PLUGIN_CREATOR_ROOT/scripts/validate_plugin.py" plugins/codex-media-advertising
git diff --check
```

Expected: all tests pass and plugin validation reports success.

Commit:

```bash
git add .agents .gitignore LICENSE NOTICE plugins/codex-media-advertising
git commit -m "feat: scaffold media advertising plugin"
```

---

### Task 2: Typed Models, Private State, and Campaign Validation

**Files:**
- Create: `plugins/codex-media-advertising/src/codex_media_ads/models.py`
- Create: `plugins/codex-media-advertising/src/codex_media_ads/config.py`
- Create: `plugins/codex-media-advertising/src/codex_media_ads/manifests.py`
- Create: `plugins/codex-media-advertising/tests/test_manifests.py`
- Create: `plugins/codex-media-advertising/examples/brand.example.json`
- Create: `plugins/codex-media-advertising/examples/campaign.example.json`
- Create: `plugins/codex-media-advertising/examples/schedule.example.json`

**Interfaces:**
- Produces: `CampaignManifest`, `AccountConfig`, `PublishRequest`, `PublishResult`, `load_campaign(path)`, `state_layout(root)`.
- Consumed by: optimization, creative pipeline, queue, adapters, setup, and CLI.

- [ ] **Step 1: Write failing manifest and privacy tests**

```python
from pathlib import Path
import pytest

from codex_media_ads.config import state_layout
from codex_media_ads.manifests import load_campaign


def test_manifest_rejects_embedded_secret(tmp_path: Path):
    path = tmp_path / "campaign.json"
    path.write_text('{"schema_version":"1","campaign_id":"launch","rights_confirmed":true,"secrets":{"token":"abc"}}')
    with pytest.raises(ValueError, match="secret-bearing key"):
        load_campaign(path)


def test_state_layout_never_uses_checkout(tmp_path: Path):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    with pytest.raises(ValueError, match="outside the Git checkout"):
        state_layout(checkout / "runtime", checkout=checkout)


def test_valid_manifest_has_stable_content_id(example_campaign: Path):
    first = load_campaign(example_campaign)
    second = load_campaign(example_campaign)
    assert first.content_id == second.content_id
    assert set(first.destinations) == {"instagram", "tiktok", "youtube", "x", "facebook", "threads"}
```

- [ ] **Step 2: Run the focused tests and confirm missing modules**

Run:

```bash
python3 -m pytest plugins/codex-media-advertising/tests/test_manifests.py -q
```

Expected: import failures for the new modules.

- [ ] **Step 3: Implement exact shared status and publishing contracts**

```python
from enum import StrEnum
from pathlib import Path
from pydantic import BaseModel, Field


class PublishStatus(StrEnum):
    PUBLISHED = "published"
    SUBMITTED = "submitted"
    SCHEDULED = "scheduled"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    FAILED = "failed"
    UNKNOWN = "unknown"


class AccountConfig(BaseModel):
    account_id: str = Field(min_length=1)
    expected_identity: str = Field(min_length=1)
    mode: str = "auto"
    secret_file: Path | None = None
    chrome_profile: str | None = None
    cdp_url: str | None = None


class PublishRequest(BaseModel):
    content_id: str
    revision: int = Field(ge=1)
    platform: str
    account: AccountConfig
    media_path: Path
    metadata: dict[str, object]
    idempotency_key: str
    dry_run: bool = False


class PublishResult(BaseModel):
    status: PublishStatus
    platform_id: str = ""
    post_url: str = ""
    evidence: dict[str, object] = Field(default_factory=dict)
    error_category: str = ""
    detail: str = ""
```

Implement `CampaignManifest` with strict fields for brand, campaign ID, rights confirmation, audience, offer, proof points, calls to action, visual prompts, narration, duration, destinations, timezone, schedule, daily cap, retry limit, and failure-pause threshold. Reject unknown destinations and any recursive key matching `token`, `secret`, `password`, `cookie`, `authorization`, or `api_key`.

Generate `content_id` as the first 24 hex characters of SHA-256 over canonical JSON excluding schedule timestamps and output paths.

- [ ] **Step 4: Add private state creation and redaction**

Implement:

```python
PRIVATE_MODES = 0o700
SECRET_FILE_MODE = 0o600
SENSITIVE_KEYS = {"token", "secret", "password", "cookie", "authorization", "api_key"}


def redact(value: object, key: str = "") -> object:
    if any(part in key.lower() for part in SENSITIVE_KEYS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
```

`state_layout()` must create `config`, `secrets`, `browser-profiles`, `campaigns`, `generated`, `queue/pending`, `queue/claims`, `queue/completed`, `queue/failed`, `receipts`, `health`, and `logs` with private permissions.

- [ ] **Step 5: Run, validate examples, and commit**

Run:

```bash
python3 -m pytest plugins/codex-media-advertising/tests/test_manifests.py -q
python3 -m compileall -q plugins/codex-media-advertising/src
git diff --check
```

Expected: tests pass and examples load without warnings.

Commit:

```bash
git add plugins/codex-media-advertising/src plugins/codex-media-advertising/tests plugins/codex-media-advertising/examples
git commit -m "feat: add campaign and private state contracts"
```

---

### Task 3: Platform Optimization Policies

**Files:**
- Create: `plugins/codex-media-advertising/src/codex_media_ads/optimization.py`
- Create: `plugins/codex-media-advertising/src/codex_media_ads/policies/platforms.v1.json`
- Create: `plugins/codex-media-advertising/tests/test_optimization.py`

**Interfaces:**
- Consumes: `CampaignManifest`.
- Produces: `MetadataPack` and `optimize_for_platform(campaign, platform)`.

- [ ] **Step 1: Write failing policy tests**

```python
import pytest
from codex_media_ads.optimization import optimize_for_platform


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
```

- [ ] **Step 2: Run and verify failure**

Run:

```bash
python3 -m pytest plugins/codex-media-advertising/tests/test_optimization.py -q
```

Expected: module or function not found.

- [ ] **Step 3: Implement a versioned metadata pack**

```python
from pydantic import BaseModel, Field


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
```

The JSON policy file must contain explicit, testable limits and output fields. Treat them as configurable validation defaults rather than timeless platform facts:

```json
{
  "version": "platforms.v1",
  "instagram": {"caption_max": 2200, "hashtags_max": 30, "aspect_ratio": "9:16"},
  "tiktok": {"caption_max": 2200, "hashtags_max": 8, "aspect_ratio": "9:16"},
  "youtube": {"title_max": 100, "description_max": 5000, "tags_chars_max": 500, "aspect_ratio": "9:16"},
  "x": {"caption_max": 280, "hashtags_max": 4, "aspect_ratio": "9:16"},
  "facebook": {"caption_max": 2200, "hashtags_max": 15, "aspect_ratio": "9:16"},
  "threads": {"caption_max": 500, "hashtags_max": 5, "aspect_ratio": "9:16"}
}
```

`optimize_for_platform()` must apply platform overrides, normalize whitespace, retain the required call to action, deduplicate hashtags, validate all configured limits, and record `policy_version`.

- [ ] **Step 4: Run tests and commit**

Run:

```bash
python3 -m pytest plugins/codex-media-advertising/tests/test_optimization.py -q
git diff --check
```

Commit:

```bash
git add plugins/codex-media-advertising/src/codex_media_ads/optimization.py plugins/codex-media-advertising/src/codex_media_ads/policies plugins/codex-media-advertising/tests/test_optimization.py
git commit -m "feat: add platform optimization policies"
```

---

### Task 4: Atomic Queue, Idempotency, Caps, and Receipts

**Files:**
- Create: `plugins/codex-media-advertising/src/codex_media_ads/queueing/__init__.py`
- Create: `plugins/codex-media-advertising/src/codex_media_ads/queueing/store.py`
- Create: `plugins/codex-media-advertising/src/codex_media_ads/queueing/receipts.py`
- Create: `plugins/codex-media-advertising/tests/test_queueing.py`

**Interfaces:**
- Produces: `QueueStore.enqueue`, `claim_next`, `complete`, `fail`, `recover_expired`; `ReceiptStore.write_attempt`, `latest_success`, `count_successes_on_date`.
- Consumed by: orchestrator and publishing CLI.

- [ ] **Step 1: Write failing queue truth tests**

```python
from datetime import datetime, timedelta, timezone


def test_success_receipt_suppresses_duplicate(queue_store, publish_request):
    queue_store.enqueue(publish_request)
    queue_store.receipts.write_attempt(publish_request, status="published", post_url="https://example.test/post/1")
    assert queue_store.enqueue(publish_request).status == "duplicate_success"


def test_active_claim_cannot_be_stolen(queue_store, publish_request):
    queue_store.enqueue(publish_request)
    first = queue_store.claim_next(worker_id="worker-a", lease_seconds=300)
    second = queue_store.claim_next(worker_id="worker-b", lease_seconds=300)
    assert first is not None
    assert second is None


def test_ambiguous_submission_is_not_retryable(classifier):
    decision = classifier(category="ambiguous_submit", attempt=1, max_retries=1)
    assert decision.retry is False


def test_daily_caps_use_configured_timezone(receipt_store, publish_request):
    receipt_store.write_attempt(publish_request, status="published", occurred_at="2026-07-14T06:30:00Z")
    assert receipt_store.count_successes_on_date("2026-07-13", "America/Los_Angeles") == 1
```

- [ ] **Step 2: Run and confirm missing queue implementation**

Run:

```bash
python3 -m pytest plugins/codex-media-advertising/tests/test_queueing.py -q
```

Expected: import failure.

- [ ] **Step 3: Implement idempotency and atomic claims**

Use this exact key:

```python
def idempotency_key(content_id: str, platform: str, account_id: str, revision: int) -> str:
    raw = f"{content_id}\0{platform}\0{account_id}\0{revision}".encode()
    return hashlib.sha256(raw).hexdigest()
```

Persist each pending record as `<idempotency-key>.json`. Claim with `os.replace(pending_path, claim_path)` so competing workers cannot both own it. A claim record includes `worker_id`, `claimed_at`, and `lease_expires_at`. Recover only when the lease is expired and no success receipt exists.

- [ ] **Step 4: Implement receipt state transitions and pause counters**

Receipt statuses count as success only when they are `published`, `submitted`, or `scheduled` and the adapter supplies positive evidence. A result named `unknown`, `blocked`, or `failed` never counts toward caps or duplicate suppression.

Append compact JSON to `receipts.jsonl` with `os.open(..., os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)`, then atomically replace the latest receipt file. Protect an existing success receipt from being overwritten by a later failure.

Implement:

```python
def retry_decision(category: str, attempt: int, max_retries: int) -> RetryDecision:
    transient = {"network", "rate_limit", "platform_ui"}
    retry = category in transient and category != "ambiguous_submit" and attempt <= max_retries
    return RetryDecision(retry=retry, delay_seconds=min(60, 5 * (2 ** max(0, attempt - 1))))
```

- [ ] **Step 5: Run concurrency and receipt tests, then commit**

Run:

```bash
python3 -m pytest plugins/codex-media-advertising/tests/test_queueing.py -q
python3 -m pytest plugins/codex-media-advertising/tests/test_queueing.py -q -x --count=5
git diff --check
```

If `pytest --count` is unavailable, repeat the first command five times from the shell without adding a runtime dependency.

Commit:

```bash
git add plugins/codex-media-advertising/src/codex_media_ads/queueing plugins/codex-media-advertising/tests/test_queueing.py
git commit -m "feat: add receipt-backed publishing queue"
```

---

### Task 5: Codimage, Narration, FFmpeg, and Resumable Creative Builds

**Files:**
- Create: `plugins/codex-media-advertising/src/codex_media_ads/creative/__init__.py`
- Create: `plugins/codex-media-advertising/src/codex_media_ads/creative/providers.py`
- Create: `plugins/codex-media-advertising/src/codex_media_ads/creative/render.py`
- Create: `plugins/codex-media-advertising/src/codex_media_ads/creative/pipeline.py`
- Create: `plugins/codex-media-advertising/tests/test_creative.py`
- Create: `plugins/codex-media-advertising/tests/fixtures/fake_codimage.py`
- Create: `plugins/codex-media-advertising/tests/fixtures/fake_tts.py`

**Interfaces:**
- Produces: `CodimageProvider.generate`, `CommandNarrationProvider.synthesize`, `render_master`, `render_variant`, `build_campaign`.
- Consumed by: orchestrator and CLI.

- [ ] **Step 1: Write failing provider and render-command tests**

```python
def test_codimage_job_uses_absolute_output(codimage_provider, tmp_path):
    job = codimage_provider.make_job("A clean product scene. No text.", tmp_path / "scene.png")
    assert Path(job["out"]).is_absolute()


def test_narration_command_does_not_use_shell(command_provider, tmp_path):
    invocation = command_provider.invocation("Hello", tmp_path / "voice.wav")
    assert isinstance(invocation, list)
    assert invocation[0].endswith("fake_tts.py")


def test_master_render_is_reused_when_hash_matches(pipeline, campaign):
    first = pipeline.build(campaign)
    second = pipeline.build(campaign)
    assert first.master_path == second.master_path
    assert second.stages["render"].status == "reused"
```

- [ ] **Step 2: Run and verify provider failures**

Run:

```bash
python3 -m pytest plugins/codex-media-advertising/tests/test_creative.py -q
```

Expected: missing modules.

- [ ] **Step 3: Implement provider contracts**

```python
class ImageProvider(Protocol):
    def generate(self, jobs: list[ImageJob]) -> list[Path]: ...


class NarrationProvider(Protocol):
    def synthesize(self, text: str, output_path: Path, voice: str) -> Path: ...
```

`CodimageProvider` calls:

```python
["uv", "run", "codimage", "batch", "--input", str(job_file), "--project-root", str(project_root), "--overwrite"]
```

Allow the executable prefix to be configured as a JSON argument array. Never accept a shell string. When Codimage is unavailable, write the JSONL job file and return a structured `dependency` block with the exact reconnect command.

Narration providers:

- `codovox`: Python executable plus local Codovox `run.py` argument array.
- `speaches`: HTTP request to a user-configured local OpenAI-compatible audio endpoint.
- `command`: argument-array template supporting only `{text_file}`, `{output_path}`, and `{voice}` placeholders.

- [ ] **Step 4: Implement deterministic rendering and stage hashes**

Build a 1080x1920 master using FFmpeg concat input and the narration duration. The command builder returns an argument list equivalent to:

```python
[
    "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
    "-i", str(narration_path), "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,format=yuv420p",
    "-c:v", "libx264", "-profile:v", "high", "-level", "4.1", "-preset", "medium", "-crf", "20",
    "-c:a", "aac", "-b:a", "192k", "-af", "loudnorm=I=-16:LRA=11:TP=-1.5", "-shortest", str(output_path),
]
```

Probe output with ffprobe JSON and require video codec `h264`, audio codec `aac`, positive duration, 1080x1920 dimensions, and a nonzero file size.

Persist `build-manifest.json` with SHA-256 hashes for the campaign, each input, audio, render command, and output. Reuse a stage only when its declared input and command hashes still match.

- [ ] **Step 5: Run synthetic FFmpeg integration and commit**

Run:

```bash
python3 -m pytest plugins/codex-media-advertising/tests/test_creative.py -q
python3 -m pytest plugins/codex-media-advertising/tests/test_creative.py -q -m integration
git diff --check
```

Expected: fake providers and a short synthetic render pass without network access.

Commit:

```bash
git add plugins/codex-media-advertising/src/codex_media_ads/creative plugins/codex-media-advertising/tests/test_creative.py plugins/codex-media-advertising/tests/fixtures
git commit -m "feat: add resumable media creation pipeline"
```

---

### Task 6: Adapter Protocol and Isolated Chrome Runtime

**Files:**
- Create: `plugins/codex-media-advertising/src/codex_media_ads/publishing/__init__.py`
- Create: `plugins/codex-media-advertising/src/codex_media_ads/publishing/base.py`
- Create: `plugins/codex-media-advertising/src/codex_media_ads/publishing/chrome.py`
- Create: `plugins/codex-media-advertising/tests/test_publishing_base.py`
- Create: `plugins/codex-media-advertising/tests/test_chrome_runtime.py`

**Interfaces:**
- Produces: `PublisherAdapter`, `AdapterRegistry`, `ManagedChrome`, `clone_profile`, `probe_identity`.
- Consumed by: browser and API adapters, setup, orchestrator.

- [ ] **Step 1: Write failing adapter registry and Chrome safety tests**

```python
def test_registry_requires_all_six_platforms(registry):
    assert set(registry.names()) == {"instagram", "tiktok", "youtube", "x", "facebook", "threads"}


def test_profile_clone_excludes_sensitive_cache_files(profile_source, tmp_path):
    clone = clone_profile(profile_source, tmp_path / "clone", profile_name="Profile 1")
    assert (clone / "Local State").exists()
    assert not any(path.name == "SingletonLock" for path in clone.rglob("*"))
    assert not any("Cache" in path.parts for path in clone.rglob("*"))


def test_identity_mismatch_is_hard_block(fake_adapter, publish_request):
    fake_adapter.observed_identity = "wrong-account"
    result = fake_adapter.validate(publish_request)
    assert result.ok is False
    assert result.error_category == "identity_mismatch"
```

- [ ] **Step 2: Implement the adapter protocol and stable error model**

```python
class PublisherAdapter(Protocol):
    platform: str

    def probe_auth(self, account: AccountConfig) -> ProbeResult: ...
    def validate(self, request: PublishRequest) -> ValidationResult: ...
    def publish(self, request: PublishRequest) -> PublishResult: ...
```

The registry rejects duplicate names and refuses to publish through an unregistered platform. Normalize exceptions to the categories in the design specification while retaining a redacted diagnostic detail.

- [ ] **Step 3: Implement isolated Chrome lifecycle**

Adapt the proven profile-clone behavior with configuration rather than fixed emails, profile names, ports, URLs, or application paths. Copy only `Local State` and the selected profile. Exclude cache, service-worker cache, singleton, lock, crashpad, and shader data. Launch Chrome with a state-root-owned `--user-data-dir`, a dynamically reserved loopback port, and a per-run log.

`ManagedChrome.close()` sends TERM to processes matching the unique clone root, waits five seconds, then sends KILL only to that same process family. It never uses a broad Chrome process kill.

- [ ] **Step 4: Run tests and commit**

Run:

```bash
python3 -m pytest plugins/codex-media-advertising/tests/test_publishing_base.py plugins/codex-media-advertising/tests/test_chrome_runtime.py -q
git diff --check
```

Commit:

```bash
git add plugins/codex-media-advertising/src/codex_media_ads/publishing plugins/codex-media-advertising/tests/test_publishing_base.py plugins/codex-media-advertising/tests/test_chrome_runtime.py
git commit -m "feat: add publisher and isolated browser contracts"
```

---

### Task 7: Browser Publishing Adapters for Six Platforms

**Files:**
- Create: `plugins/codex-media-advertising/src/codex_media_ads/publishing/browser_adapters.py`
- Create: `plugins/codex-media-advertising/src/codex_media_ads/publishing/browser_selectors.v1.json`
- Create: `plugins/codex-media-advertising/tests/test_browser_adapters.py`

**Interfaces:**
- Consumes: `PublisherAdapter`, `ManagedChrome`, `PublishRequest`.
- Produces: six registered `BrowserPublisher` implementations.

- [ ] **Step 1: Write parameterized contract tests**

```python
import pytest


@pytest.mark.parametrize("platform", ["instagram", "tiktok", "youtube", "x", "facebook", "threads"])
def test_probe_detects_logged_out_surface(platform, browser_adapter_factory, fake_page):
    fake_page.body_text = "Log in Password"
    result = browser_adapter_factory(platform, fake_page).probe_auth(account_for(platform))
    assert result.authenticated is False
    assert result.error_category == "authentication"


@pytest.mark.parametrize("platform", ["instagram", "tiktok", "youtube", "x", "facebook", "threads"])
def test_publish_requires_positive_submit_evidence(platform, browser_adapter_factory, fake_page, request_for):
    fake_page.submit_clicked = True
    fake_page.confirmation = ""
    result = browser_adapter_factory(platform, fake_page).publish(request_for(platform))
    assert result.status == "unknown"
    assert result.error_category == "ambiguous_submit"
```

- [ ] **Step 2: Add versioned selectors and platform-specific page contracts**

Store URLs, login markers, upload selectors, metadata fields, submit controls, confirmation markers, and post-link patterns in `browser_selectors.v1.json`. Code must prefer semantic Playwright roles and text patterns, using CSS selectors only for file inputs and stable application controls.

Implement these proven flows after removing all campaign-specific constants:

- Instagram: create/select, attach image or video, advance, fill caption, share, verify permalink or explicit shared confirmation.
- TikTok: Studio upload, attach video, replace filename slug, fill caption, apply configured interaction settings, post, verify success surface.
- YouTube: Studio upload, fill title/description, set audience and synthetic-media fields when available, select visibility, publish or schedule, capture video ID or URL.
- X: compose, attach media, wait for processing, fill post, submit, capture post link.
- Facebook: Reels create, attach video, fill caption, optionally schedule, publish, capture explicit confirmation or URL.
- Threads: compose, attach media, fill text, submit through the modal action, verify profile/post evidence.

- [ ] **Step 3: Enforce account identity and dry-run semantics**

Every probe returns `observed_identity`. Publishing stops before upload when it does not equal `expected_identity`. A dry run may navigate, probe, validate media, and inspect controls but must stop before clicking the final publish action and return `skipped` with `evidence.dry_run=true`.

- [ ] **Step 4: Run mocked browser contracts and commit**

Run:

```bash
python3 -m pytest plugins/codex-media-advertising/tests/test_browser_adapters.py -q
git diff --check
```

Expected: all six adapters pass logged-in, logged-out, identity mismatch, media missing, success, and ambiguous-submit cases.

Commit:

```bash
git add plugins/codex-media-advertising/src/codex_media_ads/publishing/browser_adapters.py plugins/codex-media-advertising/src/codex_media_ads/publishing/browser_selectors.v1.json plugins/codex-media-advertising/tests/test_browser_adapters.py
git commit -m "feat: add six browser publishing adapters"
```

---

### Task 8: Official API Adapters and Route Selection

**Files:**
- Create: `plugins/codex-media-advertising/src/codex_media_ads/publishing/api_adapters.py`
- Create: `plugins/codex-media-advertising/src/codex_media_ads/publishing/router.py`
- Create: `plugins/codex-media-advertising/tests/test_api_adapters.py`
- Create: `plugins/codex-media-advertising/tests/test_publisher_router.py`

**Interfaces:**
- Produces: `MetaPublisher`, `YouTubePublisher`, `XPublisher`, `select_adapter(account, platform)`.
- Consumed by: orchestrator and setup.

- [ ] **Step 1: Write failing API receipt tests**

```python
def test_meta_returns_published_only_after_permalink(meta_adapter, request, graph_api):
    graph_api.creation_id = "creation-1"
    graph_api.publish_id = "media-1"
    graph_api.permalink = "https://social.example/reel/media-1"
    result = meta_adapter.publish(request)
    assert result.status == "published"
    assert result.platform_id == "media-1"
    assert result.post_url.endswith("media-1")


def test_youtube_reports_private_only_restriction(youtube_adapter, request, youtube_api):
    youtube_api.upload_response = {"id": "video-1", "status": {"privacyStatus": "private"}}
    request.metadata["visibility"] = "public"
    result = youtube_adapter.publish(request)
    assert result.status == "submitted"
    assert result.evidence["actual_visibility"] == "private"


def test_router_prefers_api_when_probe_passes(router, api_adapter, browser_adapter, account):
    account.mode = "auto"
    api_adapter.probe_result.authenticated = True
    assert router.select(account, "youtube") is api_adapter
```

- [ ] **Step 2: Implement Meta resumable publishing**

Adapt the existing Meta credential discovery and local resumable upload logic into a generic adapter. Support Instagram Reels and Facebook Reels with separate destination IDs. Store tokens only in the configured owner-only secret file. Require a successful account probe before upload. Poll container processing with a bounded timeout and return a positive success only after publish returns a media ID; retrieve a permalink when available.

- [ ] **Step 3: Implement YouTube and X API publishing**

YouTube uses OAuth scope `https://www.googleapis.com/auth/youtube.upload`, resumable `videos.insert`, `status.selfDeclaredMadeForKids`, `status.containsSyntheticMedia`, privacy status, and optional `publishAt`. Return `submitted` while processing, retaining the video ID and actual privacy status as evidence.

X uses user OAuth, chunked media upload for video, processing-status polling, then `POST /2/tweets` with the media ID. Map `made_with_ai` and `paid_partnership` from metadata when configured. Return success only after the create-post response contains a post ID.

- [ ] **Step 4: Implement explicit route selection**

Rules:

```python
if account.mode == "api":
    return require_working_api_adapter()
if account.mode == "browser":
    return require_working_browser_adapter()
if api_adapter is not None and api_adapter.probe_auth(account).authenticated:
    return api_adapter
return require_working_browser_adapter()
```

Never change mode after upload has started. An API failure after upload begins returns its result to the orchestrator; it does not fall through to browser publishing in the same attempt.

- [ ] **Step 5: Run mocked HTTP contracts and commit**

Run:

```bash
python3 -m pytest plugins/codex-media-advertising/tests/test_api_adapters.py plugins/codex-media-advertising/tests/test_publisher_router.py -q
git diff --check
```

Commit:

```bash
git add plugins/codex-media-advertising/src/codex_media_ads/publishing/api_adapters.py plugins/codex-media-advertising/src/codex_media_ads/publishing/router.py plugins/codex-media-advertising/tests/test_api_adapters.py plugins/codex-media-advertising/tests/test_publisher_router.py
git commit -m "feat: add API-first publishing routes"
```

---

### Task 9: Orchestrator and Complete CLI

**Files:**
- Create: `plugins/codex-media-advertising/src/codex_media_ads/orchestrator.py`
- Modify: `plugins/codex-media-advertising/src/codex_media_ads/cli.py`
- Create: `plugins/codex-media-advertising/tests/test_orchestrator.py`
- Create: `plugins/codex-media-advertising/tests/test_cli.py`

**Interfaces:**
- Produces: stable commands `campaign validate`, `campaign build`, `queue add`, `queue status`, `publish next`, `publish probe`, `platform pause`, `platform resume`, and `receipts show`.
- Consumed by: skills, LaunchAgents, installer smoke tests.

- [ ] **Step 1: Write failing end-to-end state tests**

```python
def test_one_platform_failure_does_not_block_others(orchestrator, six_platform_campaign):
    orchestrator.adapters["tiktok"].next_result = failed("platform_ui")
    result = orchestrator.run_campaign(six_platform_campaign, live=True)
    assert result.platforms["tiktok"].status == "failed"
    assert result.platforms["youtube"].status in {"published", "submitted", "scheduled"}


def test_two_live_failures_pause_only_platform_account(orchestrator, x_request):
    orchestrator.publish(x_request)
    orchestrator.publish(x_request.model_copy(update={"revision": 2}))
    assert orchestrator.pause_store.is_paused("x", x_request.account.account_id)
    assert not orchestrator.pause_store.is_paused("threads", x_request.account.account_id)


def test_dry_run_never_creates_success_receipt(cli_runner, campaign_path):
    result = cli_runner(["campaign", "build", str(campaign_path), "--dry-run"])
    assert result.exit_code == 0
    assert result.json["live_success_count"] == 0
```

- [ ] **Step 2: Implement orchestration order**

`run_campaign()` validates, builds, optimizes, enqueues six independent records, and processes due destinations. It catches and records per-destination failures, then continues. It checks daily caps and pause state before claiming a queue record.

Publishing order for one record:

1. Reject a prior success receipt.
2. Reject paused destination/account state.
3. Claim atomically.
4. Select adapter once.
5. Probe identity and validate media/metadata.
6. Publish or dry-run.
7. Write attempt receipt before moving the claim.
8. Complete on verified success, fail on terminal result, or requeue one classified transient failure.
9. Update consecutive-failure state.

- [ ] **Step 3: Build a JSON-first CLI**

Every subcommand accepts `--format json|text`, defaulting to JSON for automation-safe parsing. Structured failures use:

```json
{
  "ok": false,
  "status": "blocked",
  "error_category": "authentication",
  "detail": "YouTube account probe failed.",
  "next_action": "Run codex-media-ads publish probe --platform youtube.",
  "receipt_file": "/private/state/path/receipts/content/youtube.json"
}
```

The CLI returns `0` for success and intentional no-op, `2` for validation/configuration, `3` for blocked work, and `4` for failed work.

- [ ] **Step 4: Run orchestrator and CLI tests, then commit**

Run:

```bash
python3 -m pytest plugins/codex-media-advertising/tests/test_orchestrator.py plugins/codex-media-advertising/tests/test_cli.py -q
python3 -m codex_media_ads.cli --help
git diff --check
```

Commit:

```bash
git add plugins/codex-media-advertising/src/codex_media_ads/orchestrator.py plugins/codex-media-advertising/src/codex_media_ads/cli.py plugins/codex-media-advertising/tests/test_orchestrator.py plugins/codex-media-advertising/tests/test_cli.py
git commit -m "feat: orchestrate media creation and publishing"
```

---

### Task 10: Setup, Authentication Probes, and Background Jobs

**Files:**
- Create: `plugins/codex-media-advertising/src/codex_media_ads/setup.py`
- Create: `plugins/codex-media-advertising/src/codex_media_ads/automation/__init__.py`
- Create: `plugins/codex-media-advertising/src/codex_media_ads/automation/launchd.py`
- Create: `plugins/codex-media-advertising/scripts/install.sh`
- Create: `plugins/codex-media-advertising/scripts/uninstall.sh`
- Create: `plugins/codex-media-advertising/tests/test_setup.py`
- Create: `plugins/codex-media-advertising/tests/test_launchd.py`

**Interfaces:**
- Produces: `codex-media-ads setup`, `automation install`, `automation list`, `automation remove`, and user LaunchAgents.

- [ ] **Step 1: Write failing setup and LaunchAgent tests**

```python
def test_setup_does_not_enable_unprobed_channel(setup_service):
    setup_service.probes["instagram"] = unauthenticated_probe()
    result = setup_service.configure(enabled=["instagram"])
    assert result.channels["instagram"].background_enabled is False


def test_launchagent_uses_absolute_cli_and_state_paths(launchd_builder, tmp_path):
    plist = launchd_builder.build("daily-short", state_root=tmp_path / "state")
    args = plist["ProgramArguments"]
    assert Path(args[0]).is_absolute()
    assert Path(plist["WorkingDirectory"]).is_absolute()
    assert "StartCalendarInterval" in plist


def test_uninstall_preserves_user_state(uninstaller, state_root):
    uninstaller.run(preserve_state=True)
    assert state_root.exists()
```

- [ ] **Step 2: Implement rerunnable setup checks**

Checks report `ok`, `blocked`, or `missing` for Python, FFmpeg, ffprobe, Chrome, Playwright browsers, Codimage, the configured narration provider, writable state, and each enabled adapter. Setup writes only nonsecret channel configuration; secret import copies to the private secrets directory with mode `0600`.

Before enabling a live job, setup must pass a synthetic FFmpeg render, adapter authentication probe, expected-identity comparison, and final-action-skipping dry run.

- [ ] **Step 3: Implement deterministic LaunchAgents**

Generate reverse-DNS labels under the default namespace `com.codex-media-ads`. For the `daily-short` automation, `ProgramArguments` call `codex-media-ads publish next --schedule daily-short --format json`. Use `StartCalendarInterval` or `StartInterval`, `RunAtLoad=false`, and log paths beneath the private state root. Write the plist atomically to `~/Library/LaunchAgents/com.codex-media-ads.daily-short.plist` and load it with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.codex-media-ads.daily-short.plist`.

Removal uses `launchctl bootout gui/<uid> <plist>` and deletes only plugin-owned plist files. It preserves campaigns, media, receipts, and secrets unless the user separately requests state deletion.

- [ ] **Step 4: Run setup tests and a temporary-home installer smoke**

Run:

```bash
python3 -m pytest plugins/codex-media-advertising/tests/test_setup.py plugins/codex-media-advertising/tests/test_launchd.py -q
HOME="$(mktemp -d)" plugins/codex-media-advertising/scripts/install.sh --dry-run
git diff --check
```

Expected: no real LaunchAgent is loaded during tests or dry run.

Commit:

```bash
git add plugins/codex-media-advertising/src/codex_media_ads/setup.py plugins/codex-media-advertising/src/codex_media_ads/automation plugins/codex-media-advertising/scripts plugins/codex-media-advertising/tests/test_setup.py plugins/codex-media-advertising/tests/test_launchd.py
git commit -m "feat: add onboarding and background automation"
```

---

### Task 11: Five Codex Skills and User Documentation

**Files:**
- Create: `plugins/codex-media-advertising/skills/media-onboarding/SKILL.md`
- Create: `plugins/codex-media-advertising/skills/media-onboarding/agents/openai.yaml`
- Create: `plugins/codex-media-advertising/skills/media-campaign/SKILL.md`
- Create: `plugins/codex-media-advertising/skills/media-campaign/agents/openai.yaml`
- Create: `plugins/codex-media-advertising/skills/media-automation/SKILL.md`
- Create: `plugins/codex-media-advertising/skills/media-automation/agents/openai.yaml`
- Create: `plugins/codex-media-advertising/skills/media-publishing/SKILL.md`
- Create: `plugins/codex-media-advertising/skills/media-publishing/agents/openai.yaml`
- Create: `plugins/codex-media-advertising/skills/media-operations/SKILL.md`
- Create: `plugins/codex-media-advertising/skills/media-operations/agents/openai.yaml`
- Create: `plugins/codex-media-advertising/docs/installation.md`
- Create: `plugins/codex-media-advertising/docs/authentication.md`
- Create: `plugins/codex-media-advertising/docs/automations.md`
- Create: `plugins/codex-media-advertising/docs/platform-notes.md`
- Create: `plugins/codex-media-advertising/tests/test_skills.py`

**Interfaces:**
- Consumes: stable CLI from Tasks 1–10.
- Produces: discoverable Codex workflows and GitHub-ready onboarding.

- [ ] **Step 1: Write failing skill-content tests**

```python
import re
from pathlib import Path


def test_all_skills_route_live_work_through_cli(plugin_root: Path):
    for skill in (plugin_root / "skills").glob("*/SKILL.md"):
        text = skill.read_text()
        assert "codex-media-ads" in text
        assert "Do not switch accounts" in text


def test_skills_have_valid_frontmatter(plugin_root: Path):
    for skill in (plugin_root / "skills").glob("*/SKILL.md"):
        text = skill.read_text()
        assert re.match(r"^---\nname: [a-z0-9-]+\ndescription: .+\n---\n", text)
```

- [ ] **Step 2: Author concise workflow skills**

Each skill must state when it triggers, which references to read, the exact CLI sequence, decisive artifacts, and stop rules. Required common rule text:

```text
Do not switch accounts to make a publish succeed. Treat an identity mismatch as blocked work. Do not infer publication from a queued state or process exit; inspect the destination receipt and report its exact status, ID, URL, and path.
```

Skill-specific responsibilities:

- onboarding: dependency setup, private state, account probes, dry runs, and automation installation.
- campaign: clarify brand/offer/audience/rights, write a manifest, validate it, and generate creative.
- automation: create background jobs with explicit cadence, caps, enabled channels, and health monitoring.
- publishing: build, queue, publish, and report receipt truth.
- operations: diagnose dependency, auth, identity, rendering, queue claim, pause, and ambiguous-submit states without manual bypass.

- [ ] **Step 3: Generate `agents/openai.yaml` deterministically**

Use the skill-creator generator for each skill with a display name, 25–64 character short description, and one-sentence prompt explicitly naming the skill, for example:

```bash
python3 "$SKILL_CREATOR_ROOT/scripts/generate_openai_yaml.py" \
  plugins/codex-media-advertising/skills/media-onboarding \
  --interface 'display_name=Media Onboarding' \
  --interface 'short_description=Connect media tools and publishing accounts' \
  --interface 'default_prompt=Use $media-onboarding to configure my media automation safely.'
```

- [ ] **Step 4: Document GitHub installation and platform constraints**

`installation.md` must include cloning, marketplace addition, plugin install, new-task pickup, setup, validation, upgrade cachebuster, and uninstall commands. `authentication.md` must explain user-owned Meta, Google, X, and browser sessions without sample secrets. `platform-notes.md` must explain browser fragility, API app-review constraints, YouTube private-only behavior for unverified projects, TikTok consent/app review, and the difference between `published`, `submitted`, and `unknown`.

- [ ] **Step 5: Validate every skill and commit**

Run:

```bash
for skill in plugins/codex-media-advertising/skills/*; do
  python3 "$SKILL_CREATOR_ROOT/scripts/quick_validate.py" "$skill"
done
python3 -m pytest plugins/codex-media-advertising/tests/test_skills.py -q
python3 "$PLUGIN_CREATOR_ROOT/scripts/validate_plugin.py" plugins/codex-media-advertising
git diff --check
```

Commit:

```bash
git add plugins/codex-media-advertising/skills plugins/codex-media-advertising/docs plugins/codex-media-advertising/tests/test_skills.py
git commit -m "feat: add media automation skills and guides"
```

---

### Task 12: End-to-End Test, CI, Packaging, and Public-Repo Hygiene

**Files:**
- Create: `plugins/codex-media-advertising/tests/test_end_to_end.py`
- Create: `plugins/codex-media-advertising/scripts/scan_release.py`
- Create: `plugins/codex-media-advertising/scripts/build_release.py`
- Create: `.github/workflows/ci.yml`
- Create: `README.md`
- Create: `CONTRIBUTING.md`
- Create: `SECURITY.md`

**Interfaces:**
- Produces: verified source checkout, clean release archive, CI receipts, and final public GitHub instructions.

- [ ] **Step 1: Write a fully mocked six-platform end-to-end test**

```python
def test_campaign_builds_and_publishes_six_independent_records(e2e_runtime, campaign_path):
    result = e2e_runtime.run(campaign_path, live=True)
    assert result.build.master_path.exists()
    assert set(result.platforms) == {"instagram", "tiktok", "youtube", "x", "facebook", "threads"}
    assert result.platforms["tiktok"].status == "failed"
    assert all(
        result.platforms[name].status in {"published", "submitted", "scheduled"}
        for name in {"instagram", "youtube", "x", "facebook", "threads"}
    )
    assert len(list(result.receipt_root.glob("*/latest.json"))) == 6
```

- [ ] **Step 2: Implement release scanning**

`scan_release.py` walks tracked files and archive members, failing on:

- filenames associated with tokens, cookies, browser profiles, `.env`, receipts, queues, logs, or generated media
- absolute home, Applications, or mounted-volume paths
- email addresses, phone-number patterns, OAuth tokens, JWTs, private keys, Meta/Google client-secret keys, and known personal campaign identifiers
- media files outside explicitly allowlisted plugin icons and synthetic test fixtures

Allowlist only documented examples such as `author@example.com`, `https://example.com`, synthetic fixture IDs, and the Apache license text.

- [ ] **Step 3: Build reproducible release archives**

`build_release.py` reads only `git ls-files`, excludes design/plan internals from the plugin archive, writes files in sorted order with normalized timestamps, and produces `dist/codex-media-advertising-0.1.0.zip` plus a SHA-256 file. It runs the scanner on both the checkout and archive before returning success.

- [ ] **Step 4: Add macOS CI and complete README**

CI jobs:

```yaml
jobs:
  test:
    runs-on: macos-15
    strategy:
      matrix:
        python-version: ["3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: brew install ffmpeg
      - run: python -m pip install -e 'plugins/codex-media-advertising[test,browser,youtube]'
      - run: python -m playwright install chromium
      - run: python -m pytest plugins/codex-media-advertising/tests -q
      - run: python plugins/codex-media-advertising/scripts/scan_release.py .
      - run: python plugins/codex-media-advertising/scripts/build_release.py
```

README order: outcome, demo command, requirements, install, `$media-onboarding`, first dry run, first automation, receipt inspection, safety boundary, supported platforms, troubleshooting, license.

- [ ] **Step 5: Run the complete release gate**

Run:

```bash
python3 -m pytest plugins/codex-media-advertising/tests -q
python3 -m compileall -q plugins/codex-media-advertising/src
python3 plugins/codex-media-advertising/scripts/scan_release.py .
python3 plugins/codex-media-advertising/scripts/build_release.py
python3 "$PLUGIN_CREATOR_ROOT/scripts/validate_plugin.py" plugins/codex-media-advertising
unzip -l dist/codex-media-advertising-0.1.0.zip
shasum -a 256 dist/codex-media-advertising-0.1.0.zip
git diff --check
git status --short
```

Expected: all tests and validators pass; the archive contains no private/runtime artifacts; only intentional source changes remain before commit.

- [ ] **Step 6: Commit release-ready v0.1.0**

```bash
git add .github README.md CONTRIBUTING.md SECURITY.md plugins/codex-media-advertising/tests/test_end_to_end.py plugins/codex-media-advertising/scripts/scan_release.py plugins/codex-media-advertising/scripts/build_release.py
git commit -m "release: prepare media advertising plugin v0.1.0"
```

---

### Task 13: GitHub Publication and Install Verification

**Files:**
- Modify: `plugins/codex-media-advertising/.codex-plugin/plugin.json`
- Modify: `README.md`

**Interfaces:**
- Consumes: verified local repository and GitHub authentication.
- Produces: public GitHub repository, remote commit proof, and clean-clone installation proof.

- [ ] **Step 1: Confirm publication identity and repository availability**

Run:

```bash
gh auth status
gh repo view CHARLESMORGANSOFTWARE/codex-media-advertising --json nameWithOwner,visibility,url 2>/dev/null || true
```

Expected: authenticated GitHub identity is visible; either the repository is absent or it already points to this project. Do not overwrite an unrelated repository.

- [ ] **Step 2: Create the public repository when absent**

Run:

```bash
gh repo create CHARLESMORGANSOFTWARE/codex-media-advertising \
  --public \
  --description "Codex plugin for automated media creation and social publishing" \
  --source . \
  --remote origin \
  --push
```

Expected: GitHub reports the public repository URL and pushes `main`.

- [ ] **Step 3: Replace provisional public URLs and revalidate**

Set plugin `repository`, `homepage`, and interface website URL to the real HTTPS GitHub URLs. Keep privacy-policy and terms URLs omitted unless real documents are added at stable public HTTPS locations.

Run:

```bash
python3 "$PLUGIN_CREATOR_ROOT/scripts/validate_plugin.py" plugins/codex-media-advertising
python3 plugins/codex-media-advertising/scripts/scan_release.py .
git add README.md plugins/codex-media-advertising/.codex-plugin/plugin.json
git commit -m "docs: link public plugin repository"
git push origin main
```

- [ ] **Step 4: Verify a clean clone and Codex marketplace install**

Run in a temporary directory:

```bash
git clone https://github.com/CHARLESMORGANSOFTWARE/codex-media-advertising.git clean-clone
cd clean-clone
codex plugin marketplace add "$PWD"
codex plugin add codex-media-advertising@codex-media-advertising
codex plugin list
```

Expected: the marketplace is installed, the plugin appears under its exact name, and a new Codex task can discover `$media-onboarding`.

- [ ] **Step 5: Record final publication evidence**

Run:

```bash
git rev-parse HEAD
git status --short
gh repo view CHARLESMORGANSOFTWARE/codex-media-advertising --json nameWithOwner,visibility,url,defaultBranchRef
shasum -a 256 dist/codex-media-advertising-0.1.0.zip
```

Expected: clean status, public visibility, `main` points at the local HEAD, and the release hash is recorded in the final handoff.

---

## Plan Self-Review Checklist

- Every design requirement maps to a task: marketplace and plugin identity (Task 1), schemas and privacy (Task 2), optimization (Task 3), receipt truth and safety (Task 4), creation pipeline (Task 5), adapter foundation (Task 6), six browser destinations (Task 7), API routes (Task 8), orchestration (Task 9), background setup (Task 10), skills/docs (Task 11), clean release (Task 12), and public GitHub proof (Task 13).
- Shared types are defined once in Task 2 and consumed consistently afterward.
- Live platform calls are absent from tests and CI; opt-in canary posts remain a maintainer action after users connect test accounts.
- Public publishing is an authorized final step because the user explicitly requested a public GitHub plugin; repository ownership is still checked before creation.
- The plan contains no credential values, browser profiles, customer media, personal account mappings, or local source paths inside the future distributable plugin.
