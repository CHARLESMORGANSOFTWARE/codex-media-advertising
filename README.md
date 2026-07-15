# Codex Media & Advertising

Create a narrated image-to-video campaign and operate receipt-backed publishing
automations for Instagram, TikTok, YouTube, X, Facebook, and Threads from
Codex. Every destination is isolated: a paused, failed, or ambiguous channel
does not silently mark the other channels as published.

## Demo

```bash
codex-media-ads campaign validate plugins/codex-media-advertising/examples/campaign.example.json --format json
codex-media-ads campaign build plugins/codex-media-advertising/examples/campaign.example.json --dry-run --format json
```

## Requirements

macOS 13+, Codex, Git, Python 3.11+, FFmpeg/ffprobe, Chrome or Chromium, and
configured Codimage plus narration (Codovox-compatible, command, or Speaches)
providers. API and Playwright/YouTube dependencies are optional per route.

## Install

```bash
git clone https://github.com/CHARLESMORGANSOFTWARE/codex-media-advertising.git
cd codex-media-advertising
codex plugin marketplace add "$PWD"
codex plugin add codex-media-advertising@personal
./plugins/codex-media-advertising/scripts/install.sh
```

The installer adds the pinned local Speaches/Whisper dependency under
`$HOME/.local/share/codex-media-ads/speech/speaches`; it does not replace the
existing media pipeline or provider interface. Start that local service and
select the existing `speaches` narration provider by following
[`docs/installation.md`](plugins/codex-media-advertising/docs/installation.md).

Start a new Codex task and use `$media-onboarding` to connect providers and
social accounts. Install `[browser,youtube]` extras when those routes are
needed; the installer never creates credentials or publishes content.

## First dry run

Run `codex-media-ads setup --format json`, resolve every blocked dependency or
identity check, then use `$media-campaign` and `campaign build --dry-run`.
Setup proves synthetic FFmpeg output, exact account identity, and actionable
upload/submit controls before a background job can be enabled.

## First automation

After a successful dry run, install a user LaunchAgent:

```bash
codex-media-ads automation install daily-short --hour 9 --minute 0 --format json
codex-media-ads automation list --format json
```

Use `$media-automation` to inspect, pause, resume, or remove jobs. The worker
uses private state outside the checkout and deterministic arguments.

## Receipt inspection

Publication is proven only by a destination receipt. Inspect it with:

```bash
codex-media-ads queue status --format json
codex-media-ads receipts show --format json
```

Report the exact status, platform ID, URL, and receipt path. Queued work or a
process exit is not publication proof.

## Safety boundary

Secrets, browser profiles, media, queues, logs, and receipts stay in the
private state root (`~/.codex-media-ads/` by default). Credentials are imported
with owner-only permissions; account identity is checked before upload; final
submit evidence is required; duplicate suppression, caps, one transient retry,
and per-platform pause rules are enforced. Never hand-edit a receipt or plist,
switch accounts to bypass an identity mismatch, or retry an ambiguous submit.

## Supported platforms

Instagram, TikTok, YouTube, X, Facebook, and Threads each have an API-first
route where supported and an isolated Chrome/Playwright route where configured.
Platform-specific metadata, schedule controls, and final-action evidence are
validated independently.

## Troubleshooting

Use `$media-operations`, `codex-media-ads setup --format json`, and
`codex-media-ads publish probe --platform PLATFORM --format json`. Resolve the
reported `next_action`, then rerun the same command. For `unknown` or
`ambiguous_submit`, reconcile the destination and receipt before retrying.
See [`docs/installation.md`](plugins/codex-media-advertising/docs/installation.md),
[`docs/authentication.md`](plugins/codex-media-advertising/docs/authentication.md),
and [`docs/platform-notes.md`](plugins/codex-media-advertising/docs/platform-notes.md).

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
