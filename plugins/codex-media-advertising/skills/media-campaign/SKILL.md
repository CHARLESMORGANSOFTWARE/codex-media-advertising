---
name: media-campaign
description: Use when turning a brand, offer, audience, and rights-cleared assets into a validated multi-platform media campaign
---

# Media Campaign

Use this skill when the user wants a campaign brief, images, narration, a
vertical video, or destination-specific copy. Read `../../docs/platform-notes.md`
and the example manifests in `../../examples/` before asking for missing inputs.

## Clarify the brief

Confirm the brand voice, audience, offer, proof points, required and prohibited
claims, call to action, destination URL, accessibility text, music policy,
source-asset provenance, and publishing-rights confirmation. Confirm duration,
orientation, cadence, timezone, enabled destinations, and per-platform limits.
Do not invent regulated claims, rights, account identities, or customer media.

## Build and validate

1. Create a schema-versioned campaign JSON outside the repository. Keep secrets
   in private configuration references only.
2. Run `codex-media-ads campaign validate PATH --format json`. Fix every
   validation error before generating media; capture the stable `campaign_id`
   and `content_id`.
3. Run `codex-media-ads campaign build PATH --dry-run --format json` for the
   first pass. Inspect the build manifest, master media path, six destination
   records, optimization policy versions, and accessibility metadata.
4. Use the configured Codimage and narration providers through the plugin's
   creative pipeline. FFmpeg/ffprobe validation must succeed before queueing.
5. Ask for approval before a live build or publishing. A live build is
   `codex-media-ads campaign build PATH --format json`; it still reports each
   platform independently.

## Evidence and stop rules

The decisive artifacts are the validated manifest, build manifest, generated
master/variant paths, and per-platform metadata. Keep those paths under private
state and report redacted paths only when appropriate.

Do not switch accounts to make a publish succeed. Treat an identity mismatch as blocked work. Do not infer publication from a queued state or process exit; inspect the destination receipt and report its exact status, ID, URL, and path.

Stop on failed rights confirmation, unsupported claims, missing provider tools,
ambiguous FFmpeg output, or a destination validation error. Never repair a
manifest by weakening a platform policy or manually editing generated media.
