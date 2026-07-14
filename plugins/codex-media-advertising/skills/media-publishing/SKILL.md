---
name: media-publishing
description: Use when building, queueing, publishing, probing, or reporting receipt-backed posts across supported social destinations
---

# Media Publishing

Use this skill for an explicit publish request, a queued campaign, a scheduled
post, or a receipt/status report. Read `docs/platform-notes.md` before choosing
an API or isolated-browser route.

## Publish sequence

1. Validate and dry-run the campaign first. Build only from a rights-confirmed
   manifest and inspect the destination variant metadata.
2. Queue a typed request with `codex-media-ads queue add REQUEST.json
   --format json`. Save the returned queue path and idempotency key.
3. Probe the exact configured account before a live attempt:
   `codex-media-ads publish probe --platform PLATFORM --format json`.
4. Process one claim with `codex-media-ads publish next --format json` (use
   `--dry-run` for a non-mutating proof). The queue enforces caps, leases,
   duplicate suppression, retries, and per-destination pause state.
5. Inspect `codex-media-ads receipts show --format json`. A receipt is the only
   decisive publication evidence; report status, platform ID, URL, and receipt
   path for every destination.

## Status semantics

`published` means the platform returned final success evidence; `submitted` means
the platform accepted the request but final visibility may still be pending;
`scheduled` includes a platform-confirmed future time; `failed` is a classified
terminal failure; `unknown` or `ambiguous_submit` means the mutation outcome is
uncertain and must not be retried automatically. `blocked` means setup, policy,
identity, pause, or cap gates stopped the attempt.

Do not switch accounts to make a publish succeed. Treat an identity mismatch as blocked work. Do not infer publication from a queued state or process exit; inspect the destination receipt and report its exact status, ID, URL, and path.

Never retry after ambiguous submission, never reuse a successful idempotency key
for a different asset, and never use a generic success banner as platform proof.
