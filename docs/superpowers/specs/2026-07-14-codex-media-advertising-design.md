# Codex Media and Advertising Plugin Design

## Outcome

Build a public, standalone Codex plugin that lets a new user install one GitHub repository, connect their own accounts, describe a brand and campaign, and create unattended background automations that generate and publish optimized media to Instagram, TikTok, YouTube, X, Facebook, and Threads.

The plugin must preserve the operational knowledge proven in the existing Codimage, Codovox/Hype Audio, Hype Video, launch-campaign, multi-platform campaign, and social-publisher workflows. It must not copy user-specific accounts, browser profiles, campaign copy, generated media, receipts, or secrets.

## Product Boundary

The repository is both a Codex plugin and a local automation runtime:

- Codex skills teach campaign design, creative production, platform optimization, onboarding, automation creation, publishing, and operations.
- Deterministic Python runners own manifests, rendering, queues, leases, limits, retries, publishing, receipts, and health checks.
- Codimage supplies campaign imagery through its public CLI or MCP contract.
- Narration uses a provider interface. The initial local providers are Codovox/Speaches and an arbitrary command template, allowing a user to route synthesis through their own Vocodex-compatible service without embedding Vocodex account or relay state.
- Hype Video behavior is extracted into the repository as a reusable FFmpeg-based renderer rather than requiring Telecodex to be installed.
- Platform adapters share one typed request/result contract but remain independently configurable and pausable.
- Codex automation templates call stable plugin-owned commands. They never contain personal absolute paths or credentials.

The initial release supports macOS because the proven unattended browser flows depend on Chrome, CDP, Playwright, launchd-style background execution, and macOS profile locations. The core manifest, render, queue, receipt, and formatting modules must remain portable so Windows and Linux schedulers can be added later.

## Source and Licensing Boundary

The plugin repository will use Apache License 2.0. The reusable Telecodex source is already Apache-2.0, so retaining that license avoids ambiguous relicensing and preserves required notices.

Source may be adapted from these reusable or generalizable implementations:

- `tool-lab/tools/hype-video`
- `tool-lab/tools/hype-audio`
- `tool-lab/tools/imovie-reel-stager`
- `tool-lab/tools/social-queue-publisher`
- generic X and TikTok publishers
- general queue, receipt, duplicate-protection, cap, retry, and pause logic from the existing campaign runners
- general YouTube and Facebook upload behavior after removing campaign-specific constants

The public repository must not include:

- Chrome profiles, cookies, tokens, OAuth client secrets, `.env` files, or account identifiers
- personal phone numbers, email addresses, usernames, channel IDs, profile URLs, or campaign names
- existing generated images, audio, videos, screenshots, queue state, logs, or receipts
- music, fonts, headshots, customer media, or other assets without explicit redistribution rights
- hard-coded machine-specific absolute source paths

## Repository Shape

```text
codex-media-advertising/
├── .codex-plugin/plugin.json
├── .github/workflows/ci.yml
├── assets/
├── docs/
│   ├── installation.md
│   ├── authentication.md
│   ├── automations.md
│   ├── platform-notes.md
│   └── superpowers/
├── examples/
│   ├── brand.example.json
│   ├── campaign.example.json
│   └── schedule.example.json
├── skills/
│   ├── media-campaign/
│   ├── media-onboarding/
│   ├── media-automation/
│   ├── media-publishing/
│   └── media-operations/
├── src/codex_media_ads/
│   ├── cli.py
│   ├── config.py
│   ├── manifests.py
│   ├── optimization.py
│   ├── creative/
│   ├── queueing/
│   ├── publishing/
│   └── automation/
├── tests/
├── scripts/install.sh
├── scripts/uninstall.sh
├── pyproject.toml
├── README.md
├── LICENSE
└── NOTICE
```

The plugin manifest discovers the five skills. The Python package installs the `codex-media-ads` command used by both humans and Codex automations.

## Configuration and State

All shareable configuration is JSON and schema-versioned. Runtime state defaults to `~/.codex-media-ads/` and is excluded from Git.

```text
~/.codex-media-ads/
├── config/
│   ├── brand.json
│   ├── channels.json
│   └── schedule.json
├── secrets/
├── browser-profiles/
├── campaigns/
├── generated/
├── queue/
│   ├── pending/
│   ├── claims/
│   ├── completed/
│   └── failed/
├── receipts/
│   ├── receipts.jsonl
│   └── <content-id>/<platform>.json
├── health/
└── logs/
```

Secret files receive owner-only permissions when supported. Configuration stores references to secrets, never secret values in campaign manifests or automation prompts.

## Campaign Contract

A campaign manifest contains:

- schema version and stable campaign/content IDs
- brand name, voice, audience, offer, proof points, required claims, prohibited claims, calls to action, and destination URL
- creative format, duration, orientation, shot count, image prompts, narration, music policy, captions, and accessibility text
- enabled destinations and per-platform overrides
- timezone, start/end windows, cadence, daily cap, retry limit, and failure pause threshold
- provenance for source assets and confirmation that the user has publishing rights

The command `codex-media-ads campaign validate` rejects incomplete manifests, unknown platforms, unsafe relative paths, conflicting schedule windows, absent rights confirmation, and secrets embedded in the document.

## Creative Pipeline

The command `codex-media-ads campaign build` performs these resumable stages:

1. Validate the campaign and create a stable build ID.
2. Generate Codimage JSONL jobs with absolute output paths and no-text image instructions unless on-image text is explicitly requested.
3. Invoke Codimage through its CLI, or emit the job file with an actionable blocked state when Codimage is not yet connected.
4. Synthesize narration through the selected provider and retain the transcript.
5. Probe image and audio inputs, then render a master H.264/AAC MP4 through FFmpeg.
6. Create destination variants for aspect ratio, duration, bitrate, loudness, and file-size limits.
7. Generate deterministic metadata packs for each destination.
8. Validate every output and enqueue one publish record per destination.

Existing valid stage artifacts are reused by content hash. `--force-stage` can rebuild one named stage without discarding unrelated work.

## Platform Optimization

Optimization is a deterministic policy layer whose inputs are campaign copy and whose outputs are platform metadata packs. Codex may author the initial copy, but the runtime validates the result before publishing.

Each pack includes the supported subset of title, caption, description, hashtags, tags, alt text, thumbnail, visibility, made-for-kids choice, link placement, and scheduling fields. Policies enforce current configured limits, required disclosures, duplicate hashtag cleanup, filename-slug rejection, and destination-specific calls to action.

Platform limit values live in versioned configuration and can be updated without rewriting campaign logic. The runtime records the policy version used in each receipt.

## Publishing Adapters

Every adapter implements:

```python
probe_auth(account: AccountConfig) -> ProbeResult
validate(request: PublishRequest) -> ValidationResult
publish(request: PublishRequest) -> PublishResult
```

`PublishResult` distinguishes `published`, `submitted`, `scheduled`, `skipped`, `blocked`, `failed`, and `unknown`. Only a verified success result may close a queue record as successful.

Initial adapters:

- Instagram: Meta Graph/resumable upload when configured; isolated authenticated Chrome fallback.
- Facebook: Meta API when configured; Facebook Reels Chrome fallback.
- YouTube: YouTube Data API with user-owned OAuth credentials; authenticated YouTube Studio fallback.
- TikTok: isolated authenticated Chrome/CDP upload.
- X: isolated authenticated Chrome/CDP compose and media upload.
- Threads: isolated authenticated Chrome/CDP compose and media upload.

Each adapter verifies the configured account identity when the platform exposes it. An identity mismatch is a hard stop. It must never switch accounts automatically.

Browser automation is best-effort because platform UIs change. API paths are preferred where a suitable official upload API and user credentials are available. Platform documentation will state these operational constraints and require users to comply with applicable platform rules.

## Queue, Duplicate Protection, and Receipts

The queue is file-backed and safe for one or more scheduler invocations:

- A worker atomically claims a destination record with a lease.
- The idempotency key is the content ID, destination, account ID, and revision.
- Existing success receipts suppress duplicate live publication.
- Expired claims can be recovered; active claims cannot be stolen.
- Dry runs never write a live-success status.
- A missing URL may still be a valid `submitted` result only when the adapter returns positive platform evidence; the receipt must preserve that distinction.
- Failed attempts write receipts before the command exits nonzero.
- One bounded retry is allowed only for errors classified as transient and only when no ambiguous submit signal exists.
- Two consecutive live failures pause that destination/account pair until an authentication probe succeeds or the user explicitly resumes it.
- Per-platform and global daily caps use the configured local timezone and verified receipt timestamps.

Every attempt appends to `receipts.jsonl` and writes a latest structured receipt containing IDs, account, platform, media hashes and paths, policy version, attempt count, timestamps, adapter, status, returned URL/ID, evidence, error category, and diagnostic artifact paths.

## Onboarding and Background Automation

`codex-media-ads setup` is rerunnable and performs:

1. Dependency checks for Python, FFmpeg/ffprobe, Chrome, Playwright, Codimage, and the selected narration provider.
2. Creation of private state directories.
3. Brand, timezone, channel, cadence, and limit configuration.
4. Per-channel authentication setup and identity probes.
5. A synthetic render smoke test.
6. A non-publishing adapter dry run.
7. Installation of user-level background jobs only after the relevant checks pass.

The automation skill supplies templates for:

- daily short creation and publishing
- scheduled campaign queue processing
- cross-platform repurposing of an existing video
- authentication and queue health monitoring
- paused-platform recovery checks

On macOS, the installer generates user-specific LaunchAgent files that call the installed CLI with absolute paths. The plugin also includes ready-to-paste Codex automation prompts for users who want Codex Automations to own the schedule. Setup never enables a live publishing job for a channel that has not passed its authentication probe and dry run.

## Codex Skills

- `media-onboarding`: install dependencies, configure local state, connect accounts, verify identities, and create automations.
- `media-campaign`: turn a product, service, or idea into a rights-aware campaign manifest and creative plan.
- `media-automation`: create, inspect, pause, resume, and safely modify recurring background jobs.
- `media-publishing`: build, queue, dry-run, and publish through the plugin-owned commands while reporting receipt truth.
- `media-operations`: diagnose failures from probes, claims, logs, and receipts without bypassing account or retry boundaries.

The skill instructions are concise and route operational work through the runtime. They do not tell Codex to improvise live clicks outside the adapter contract.

## Error Handling

Errors use stable categories: `configuration`, `dependency`, `authentication`, `identity_mismatch`, `validation`, `rights`, `render`, `network`, `platform_ui`, `rate_limit`, `ambiguous_submit`, and `internal`.

Commands return structured JSON with a nonzero exit status for failed or blocked work. Errors include the next safe action and point to the decisive log, claim, or receipt. Ambiguous submission never triggers an automatic retry. No top-level queued or process-exit signal is reported as a published post without a success receipt.

## Security and Privacy

- The installer and CI run a secret-pattern and forbidden-path scan.
- `.gitignore` excludes state, secrets, profiles, media, queues, receipts, logs, and common credential filenames.
- Browser-profile data is stored outside the repository and is never zipped or copied into distributable artifacts.
- Commands redact tokens, cookies, OAuth codes, and secret-bearing environment variables from logs and JSON output.
- The runtime invokes commands as argument arrays, not shell-expanded strings, except for an explicitly configured narration command template whose placeholders are validated.
- Generated content is local by default. Public media hosting is optional and explicit.

## Testing and Release Gates

Unit tests cover schemas, path safety, secret redaction, metadata policies, rendering commands, claim leases, idempotency, caps, retry classification, pause recovery, and receipt state transitions.

Contract tests run each adapter against mocked browser/API responses and verify authentication, identity mismatch, success, hard failure, ambiguous submission, and missing-URL behavior.

Integration tests use temporary state to run setup, synthetic media generation, FFmpeg rendering, queueing, dry-run publishing, and receipt inspection without network publication.

Release checks require:

- plugin and skill validation
- Python tests and compile checks
- installer smoke test in a clean temporary home
- FFmpeg render/probe smoke test
- secret and personal-identifier scan
- archive inspection proving excluded data is absent
- GitHub Actions success on supported macOS and Python versions

No live social post is part of CI. A maintainer release checklist performs opt-in canary posts on separately configured test accounts and records receipt IDs outside the public repository.

## Installation and Upgrade Experience

The README provides a short path:

1. Clone the public GitHub repository.
2. Run `./scripts/install.sh`.
3. Install the repository marketplace/plugin in Codex using the documented plugin command.
4. Start a new Codex task and invoke `$media-onboarding`.
5. Complete account authentication, dry runs, and automation creation.

Upgrades preserve `~/.codex-media-ads/`, validate configuration migrations, update the plugin cachebuster through supported tooling, and require a new Codex task before testing updated skills.

## Acceptance Criteria

A clean macOS user account with Codex, Chrome, and Git can clone the repository and complete setup without Telecodex or the Marketing repository.

After authenticating their own services, the user can ask Codex to create a recurring campaign automation. The automation can generate images, narration, and a validated video; create six optimized destination records; publish enabled destinations in the background; avoid duplicates; pause a failing destination without stopping others; and provide decisive receipts for every attempt.

The repository and its release archive contain no personal identifiers, credentials, browser state, private media, existing campaign output, or local absolute paths.
