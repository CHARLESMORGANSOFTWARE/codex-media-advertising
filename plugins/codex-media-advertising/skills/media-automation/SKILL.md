---
name: media-automation
description: Use when creating, installing, inspecting, pausing, or changing recurring background media publishing jobs
---

# Media Automation

Use this skill when the user asks Codex to post on a cadence, run unattended,
install a LaunchAgent, or change channel caps. Read `../../docs/automations.md`
and `../../docs/authentication.md` first.

## Define the automation

Confirm the campaign path, timezone, cadence, start/end window, daily cap,
retry limit, failure pause threshold, enabled destinations, account identities,
and the desired schedule. Use the smallest enabled-channel set that meets the
brief. Each destination remains independently pausable.

## Create and verify

1. Validate the campaign: `codex-media-ads campaign validate PATH --format json`.
2. Run a build dry run and inspect its manifest before enabling automation.
3. Complete onboarding gates for every destination with
   `codex-media-ads setup --enable PLATFORM --config CONFIG --format json`.
4. Install the deterministic user LaunchAgent:
   `codex-media-ads automation install daily-short --hour H --minute M` (or
   `--interval SECONDS`).
5. Verify with `codex-media-ads automation list --format json` and
   `codex-media-ads queue status --format json`.
6. For a destination failure, pause only that platform/account using
   `codex-media-ads platform pause --platform PLATFORM --account ACCOUNT`.
   Resume only after a successful probe or explicit user instruction.

## Operational boundaries

The scheduler calls plugin-owned commands with a private state-root environment;
it must never contain credentials or machine-specific source paths. Preserve
caps, retry-once behavior, duplicate suppression, and the two-failure pause
threshold. A removed automation preserves private state and receipts.

Do not switch accounts to make a publish succeed. Treat an identity mismatch as blocked work. Do not infer publication from a queued state or process exit; inspect the destination receipt and report its exact status, ID, URL, and path.

Stop on a failed setup gate, malformed schedule, unsafe LaunchAgent ownership,
or a queue/receipt ambiguity. Do not hand-edit plists or launch a second copy
to work around a lease; use `media-operations` to diagnose it.
