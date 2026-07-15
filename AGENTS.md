# Codex Media & Advertising Agent Instructions

This repository is a Codex marketplace containing the
`codex-media-advertising` plugin. It creates Codimage artwork, local
Speaches/Kokoro narration, FFmpeg video variants, durable publishing queues,
and receipt-backed posts for Instagram, TikTok, YouTube, X, Facebook, and
Threads.

## Start Here

Read these files before operating or modifying the plugin:

- `README.md`
- `plugins/codex-media-advertising/docs/installation.md`
- `plugins/codex-media-advertising/docs/authentication.md`
- `plugins/codex-media-advertising/docs/automations.md`
- `plugins/codex-media-advertising/docs/platform-notes.md`

Install the marketplace and plugin from the repository root:

```bash
codex plugin marketplace add "$PWD"
codex plugin add codex-media-advertising@personal
./plugins/codex-media-advertising/scripts/install.sh
```

Start a new Codex task after installation and invoke `$media-onboarding`.

## Operating Rules

- Run `codex-media-ads setup --format json` before creating automation or
  publishing. Treat every `missing` or `blocked` check as a hard stop.
- Use Codimage for campaign artwork, the installed Speaches/Kokoro stack for
  narration, and FFmpeg for the master and platform variants.
- Never switch social accounts to make a publish succeed. An identity mismatch
  is blocked work.
- A queue record or successful process exit is not publication proof. Inspect
  the destination receipt and report its exact status, platform ID, URL, and
  receipt path.
- Do not retry an `unknown` or `ambiguous_submit` result until the destination
  and receipts have been reconciled.
- Preserve duplicate suppression, daily caps, one-transient-retry behavior,
  platform isolation, and pause-after-two-failures behavior.
- Never commit credentials, account identifiers, Chrome profiles, generated
  media, queues, receipts, logs, model caches, or machine-specific paths.
- Runtime state belongs under `~/.codex-media-ads/`; installed dependencies
  belong under `~/.local/share/codex-media-ads/`.

## Development Checks

Use the package source explicitly when running tests from the repository root:

```bash
env PYTHONPATH=plugins/codex-media-advertising/src python3 -m pytest \
  -c plugins/codex-media-advertising/pyproject.toml \
  plugins/codex-media-advertising/tests -q
python3 plugins/codex-media-advertising/scripts/scan_release.py .
python3 plugins/codex-media-advertising/scripts/build_release.py
python3 "$HOME/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py" \
  plugins/codex-media-advertising
git diff --check
```

Keep changes additive and narrowly scoped. Update tests and user-facing
documentation whenever installer, workflow, provider, or publishing behavior
changes.
