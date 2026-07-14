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

## Review remediation

### Additional RED evidence

- A focused review regression run produced `27 failed, 7 passed` for the reviewed gaps: permissive dry-run evidence, inert production setup wiring, unsafe schema persistence, destination symlinks, weak same-label plist ownership, unsafe uninstall overrides, stale writable probes, and non-executable tools.
- Follow-up edge regressions independently failed before fixes for configured nonstandard Chrome discovery, filesystem-root state overlap, and a safe temporary HOME whose ancestor is a system symlink.

### Remediations

- Non-injected `setup --enable` now loads the private runtime, derives exact configured account identity/mode, calls the real orchestrator probe, selects the configured adapter lazily, and passes an actual synthetic-media `dry_run=True` request to that adapter. API and browser adapters now explicitly attest both `dry_run=true` and `final_action_skipped=true`.
- The configured creative pipeline supplies FFmpeg, ffprobe, Codimage, narration, and nonstandard Chrome paths. Narration is exercised through its configured provider. Browser-only dependencies are reported but become required only for browser/auto channels.
- Setup configuration accepts only supported channel names and the `expected_identity`/`mode` whitelist. Unknown fields are rejected, and recursive secret-like keys including authorization, bearer, token, cookie, key, password, and client secret are rejected before persistence.
- Secret destinations are lexically constrained beneath the private state `secrets` directory. Existing parent and leaf components are inspected with `lstat`; symlinks, non-directories, and unsafe leaves are refused before the atomic write.
- LaunchAgent reinstall, listing, and removal now require the entire deterministic plugin-owned plist structure: label, executable and argument array, working directory, state environment namespace, private log paths, `RunAtLoad=false`, and one valid schedule. Same-label foreign or malformed plists are never booted out, overwritten, or deleted.
- Uninstall validates overrides before dry-run or mutation. Empty, root, HOME, state, state-overlapping, lexically ambiguous, symlinked plugin subpaths, and non-plugin-named locations are rejected; a trusted HOME with a system symlink ancestor remains safe for temporary-HOME use.
- Writable-state checks use unique temporary probes and clean them up, so a stale historical probe cannot block reruns. Tool checks require regular executable files.

### Final verification after remediation

- Focused setup/LaunchAgent/CLI suite: `87 passed`.
- Full plugin suite: `493 passed in 1.48s`.
- Temporary-HOME install and uninstall dry-runs passed without mutation or `launchctl`.
- Negative uninstall dry-runs rejected `/`, HOME, state root, and a state descendant with exit `2`.
- CLI help, `compileall -q`, `sh -n`, and `git diff --check` passed.

## Final review remediation

### RED evidence

- The final focused regression started at `16 failed, 1 passed`: twelve browser
  upload/submit readiness cases, one direct setup gate, one non-injected setup
  gate, and two physical uninstall alias cases.

### Remediations

- Browser dry-runs now inspect both required upload and final-submit controls.
  They return `skipped` with `final_action_skipped=true` only when both controls
  are visible; missing controls block with affirmative false evidence, and
  readiness-inspection errors fail safely with the same false evidence.
- Browser-mode setup independently requires `controls_ready=true` plus exact
  `upload=true` and `submit=true` evidence before background automation can be
  enabled. The non-injected production setup regression covers both the ready
  and blocked paths.
- Uninstall resolves HOME, install, and private-state paths physically before
  any dry-run output, CLI invocation, `launchctl`, or deletion. Canonical
  install/state overlap is rejected for both lexical `..` aliases and symlink
  aliases.

### Verification

- Focused RED-to-GREEN slice: `17 passed`.
- Browser/setup/CLI/LaunchAgent regression: `265 passed`.
- Full plugin suite: `509 passed in 1.86s`.
- Temporary-HOME install and uninstall positive dry-runs passed.
- Temporary-HOME `..` and symlink overlap smokes each exited `2`, preserved the
  install sentinel, and did not invoke `launchctl`.
- CLI help, `compileall -q`, `sh -n`, and `git diff --check` passed.

## Final operability remediation

### RED evidence

- The focused actionable-control regression started at `21 failed, 13 passed`:
  one missing Playwright enabled-state contract, six missing enabled-evidence
  cases, twelve disabled upload/submit cases across all six platforms, one
  direct setup gate, and one non-injected production setup gate.

### Remediations

- `BrowserPage` and its Playwright implementation now expose an enabled-state
  check backed by the resolved locator's `is_enabled()` operation.
- Browser dry-runs require both upload and submit controls to be visible and
  enabled. Evidence includes explicit per-control `controls` visibility and
  `controls_enabled` state. A disabled control blocks without uploading or
  clicking the final action.
- Browser-mode setup validates both per-control evidence maps independently;
  an inconsistent `controls_ready=true` claim cannot enable background work
  when either enabled flag is absent or false.

### Verification

- Focused actionable-control slice: `34 passed`.
- Browser/setup/CLI/LaunchAgent regression: `277 passed`.
- Final fresh full plugin suite: `521 passed in 1.90s`.
- Temporary-HOME install and uninstall dry-runs passed without mutation or
  `launchctl` invocation.
- `compileall -q`, `sh -n`, and `git diff --check` passed.
