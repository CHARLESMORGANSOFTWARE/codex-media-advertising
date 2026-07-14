---
name: media-operations
description: Use when diagnosing blocked setup, authentication, rendering, queue, pause, scheduler, or ambiguous-submit behavior without bypassing safety gates
---

# Media Operations

Use this skill for failures, stalled jobs, missing receipts, paused channels,
dependency drift, or questions about whether an unattended run is healthy.
Read `../../docs/automations.md` and `../../docs/platform-notes.md`; inspect
private logs and receipts only in the configured state root.

## Diagnostic order

1. Re-run `codex-media-ads setup --format json` and classify each check as
   `ok`, `missing`, or `blocked`.
2. Probe the exact destination with `codex-media-ads publish probe
   --platform PLATFORM --format json`; compare observed and expected identity.
3. Inspect `codex-media-ads queue status --format json` for leases, caps, and
   pending/failed counts; this is the queue claim diagnostic. Do not delete or
   manually move queue files.
4. Inspect `codex-media-ads receipts show --format json` and the destination
   receipt path. Distinguish `published`, `submitted`, `scheduled`, `failed`,
   `unknown`, and `blocked`.
5. For a paused pair, identify the two consecutive live failures and either run
   a successful probe or use the explicit command
   `codex-media-ads platform resume --platform PLATFORM --account ACCOUNT`.
6. For scheduler issues, run `codex-media-ads automation list --format json`;
   remove only a plugin-owned job with
   `codex-media-ads automation remove daily-short --format json`.

## Stop rules

Do not switch accounts to make a publish succeed. Treat an identity mismatch as blocked work. Do not infer publication from a queued state or process exit; inspect the destination receipt and report its exact status, ID, URL, and path.

Never bypass an auth or rights gate with manual browser actions, edit a receipt,
rewind a lease, delete a failed queue item, or retry an ambiguous submission.
If evidence is incomplete, report the exact missing artifact and stop. Keep
other destinations running when one destination is paused.
