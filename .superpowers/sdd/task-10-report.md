# Task 10 Report: Setup, Authentication Probes, and Background Jobs

## Outcome

Implemented rerunnable setup checks, private secret import, conservative background-enable gates, deterministic macOS user LaunchAgents, setup and automation CLI commands, and rerunnable install/uninstall scripts.

## RED evidence

- Initial focused test run failed collection in exactly two modules because `codex_media_ads.setup` and `codex_media_ads.automation` did not exist.
- Added focused failing tests before each follow-up behavior change:
  - missing dependencies must block background enablement;
  - blocked checks must produce a blocked setup summary;
  - automation install must be refused before setup proof exists;
  - repeated LaunchAgent install must boot out the owned prior job;
  - Python older than 3.11 must be blocked;
  - secret sources with symlinked parents must be rejected;
  - install must never overwrite a non-plugin-owned plist.

## GREEN evidence

- Focused setup/LaunchAgent/CLI suite: `56 passed` before the final LaunchAgent ownership regression was added.
- Final complete plugin suite: `463 passed in 1.63s`.

## Security and behavior notes

- Setup reports `ok`, `blocked`, or `missing` for Python, FFmpeg, ffprobe, Chrome, Playwright Chromium, Codimage, narration, writable private state, and enabled adapters.
- Background channel enablement requires all checks, a synthetic FFmpeg render, authentication, normalized exact-identity equality, and a dry-run result that proves the final action was skipped.
- Secret import rejects symlinks (including symlinked parents), reads with `O_NOFOLLOW` where available, writes atomically, and enforces `0700` on the secrets directory and `0600` on copied files. CLI output includes paths only, never secret content.
- LaunchAgents use the default `com.codex-media-ads` namespace, absolute executable/work/state/log paths, exact `publish next --schedule daily-short --format json` arguments, `RunAtLoad=false`, and validated calendar/interval schedules.
- LaunchAgent writes are atomic. Bootstrap and bootout use argument arrays in the exact `gui/<uid>` user domain with no shell interpolation. Reinstall boots out only a validated plugin-owned plist; removal preserves state and refuses foreign or malformed files.
- `automation install` is blocked until private setup configuration records successful live-job gates.
- Install and uninstall scripts are quoted and rerunnable; dry-run performs no mutation or `launchctl` call, and uninstall preserves private state.

## Verification

- `HOME="$(mktemp -d)" scripts/install.sh --dry-run` — passed; reported no files changed and no LaunchAgent loaded.
- Temporary-HOME uninstall dry-run — passed; reported no files changed and no `launchctl` invocation.
- `python -m codex_media_ads.cli --help` — passed and displayed setup/automation plus all prior commands.
- Full `pytest -c plugins/codex-media-advertising/pyproject.toml` suite — `463 passed`.
- `compileall -q` — passed.
- `sh -n` for both scripts — passed.
- `git diff --check` — passed.
