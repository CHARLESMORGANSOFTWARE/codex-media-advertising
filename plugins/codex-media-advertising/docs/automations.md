# Background automations

Automations are user LaunchAgents on macOS. They call the installed
`codex-media-ads` command with a private state-root environment and deterministic
arguments; credentials never appear in a plist.

## Create one safely

1. Validate the campaign and run `campaign build --dry-run`.
2. Run setup for each intended destination. It must prove dependencies,
   synthetic FFmpeg/narration output, exact account identity, and a dry run that
   skips the final action while proving controls are actionable.
3. Install the schedule:

   ```bash
   codex-media-ads automation install daily-short --hour 9 --minute 0
   # or: codex-media-ads automation install daily-short --interval 3600
   ```

4. Verify `codex-media-ads automation list --format json` and
   `codex-media-ads queue status --format json`. Inspect receipts after the
   first run with `codex-media-ads receipts show --format json`.

The queue is atomic and lease-backed. It enforces daily caps, duplicate
suppression, one transient retry, and a pause after two consecutive live
failures for a destination/account pair. A paused destination does not stop
other destinations in the same campaign.

## Inspect, pause, resume, remove

Use `automation list` to see plugin-owned jobs. Pause a single pair with
`platform pause --platform PLATFORM --account ACCOUNT`; resume only after a
successful probe or explicit user decision. Remove a job with
`automation remove daily-short`; private state and receipts remain intact.

Do not edit a plist by hand, launch duplicate workers, delete queue files, or
reset a lease to force progress. Use `$media-operations` to classify a blocked,
failed, or ambiguous run and preserve the decisive receipt.
