---
name: media-onboarding
description: Use when connecting Codimage, narration, FFmpeg, Chrome, and social accounts or enabling the first safe background media automation
---

# Media Onboarding

Use this skill when a new user is installing Codex Media & Advertising, connecting
their own accounts, or asking whether a channel is ready for unattended work.

## Before changing anything

Read `../../docs/installation.md`, `../../docs/authentication.md`, and
`../../docs/platform-notes.md`. Confirm the user owns the accounts and
publishing rights. Keep all credentials and runtime state under
`~/.codex-media-ads/`;
never put secrets in a campaign file, prompt, checkout, or receipt summary.

## Safe sequence

1. If using the managed local Speaches add-on, follow the exact start command
   in `../../docs/installation.md`, keep it bound to `127.0.0.1:8000`, and
   configure the existing `speaches` provider endpoint as
   `http://127.0.0.1:8000/v1/audio/speech`. Kokoro creates narration and Whisper
   provides transcription; first use downloads both models to private cache.
2. Check the installation and private-state prerequisites. Use
   `codex-media-ads setup --format json` and report every check, including
   missing optional browser/API dependencies. Before video creation,
   `narration_provider` must be `ok`; `missing` or `blocked` is a stop.
3. Import a user-provided credential only with
   `codex-media-ads setup --import-secret NAME=PATH --format json`. Never print
   the file contents. The command must reject symlinks and non-owner-only paths.
4. Write only nonsecret channel configuration (expected identity and route) to a
   user-owned JSON file, then run `codex-media-ads setup --config PATH
   --format json`.
5. Probe each enabled destination with
   `codex-media-ads publish probe --platform PLATFORM --format json`. An exact
   identity match is required; record the observed identity privately.
6. Enable a destination only after setup reports dependency checks, synthetic
   render/narration checks, identity probe, and a final-action-skipping dry run
   as `ok`. Use `codex-media-ads setup --enable PLATFORM --config PATH`.
7. Install a scheduler only after setup has persisted background-enabled channels
   for the requested background automation:
   `codex-media-ads automation install daily-short --hour 9 --minute 0`.

## Evidence and stop rules

Report the redacted setup JSON, configured channels, LaunchAgent path, and the
next action. A `blocked` check is an actionable stop, not permission to guess.
Do not switch accounts to make a publish succeed. Treat an identity mismatch as blocked work. Do not infer publication from a queued state or process exit; inspect the destination receipt and report its exact status, ID, URL, and path.

Never bypass setup with a manually edited plist, browser click, or secret copied
into a manifest. If a probe, dry run, or identity check is ambiguous, leave the
channel disabled and direct the user to `media-operations`.
