# Managed Local Speech Runtime Design

## Goal

Make narrated video creation plug and play after plugin installation. A user
should be able to ask Codex to create a video without separately installing or
configuring a text-to-speech endpoint. The plugin must create imagery through
the configured Codimage provider, narrate through a managed local Speaches
runtime, render through FFmpeg, and then queue or publish only to the
destinations the user directs.

## Scope

The plugin will install and manage a private local Speaches runtime. It will
download pinned Python dependencies and the following required models into
private user state, never into the Git checkout:

- TTS: `speaches-ai/Kokoro-82M-v1.0-ONNX`
- STT: `Systran/faster-distil-whisper-small.en`

Kokoro narration is required for every video build. Whisper is provisioned for
local transcription, caption verification, and future caption generation; a
Whisper failure must not be represented as a successful narrated-video build.

## Architecture

`install.sh` installs the base media package and a pinned speech dependency set
into a separate virtual environment beneath
`~/.local/share/codex-media-ads/speech/`. It creates no global Python packages
and stores model/cache data beneath the private media state root.

The runtime manager owns one loopback-only Speaches process. It starts the
service on an unused localhost port, writes its PID and redacted health record
under the private state root, waits for the OpenAI-compatible `/v1/models`
endpoint, then requests or verifies both pinned models. It must reject a remote
endpoint, a non-loopback bind, a stale PID, or model identity mismatch.

The default creative runtime is a managed `speaches` narration provider. Users
do not provide an endpoint for this path. Existing explicit `command`,
`codovox`, and external Speaches configurations remain opt-in advanced routes;
they cannot silently replace the managed local default.

## Operational Flow

1. Plugin installation provisions the speech virtual environment and downloads
   the required dependencies.
2. `codex-media-ads setup` starts the local service, confirms both model IDs,
   synthesizes a short Kokoro audio fixture, and performs a short Whisper
   transcription fixture.
3. A campaign build calls the managed narration provider for narration, renders
   the master and destination variants with FFmpeg, and persists only private
   build artifacts and receipts.
4. Publishing remains a separate, receipt-backed action. A build never posts
   unless the user has queued or directed publishing to a destination.
5. Background automation may enable only after the managed speech health check,
   identity probes, and final-action-skipping destination dry runs pass.

## Failure Handling and Safety

- If dependency installation, service startup, model download, narration, or
  transcription fails, setup is `blocked` and video creation stops before
  rendering or queueing.
- The process binds only to `127.0.0.1`; it receives no credentials by default.
- Model files, caches, logs, PID records, generated media, queues, and receipts
  are private user state and are excluded from Git and release archives.
- The installer is idempotent, supports `--dry-run`, reports actionable repair
  commands, and never starts a LaunchAgent or publishes content.
- Existing ownership, symlink, permission, account-identity, duplicate, cap,
  retry, pause, and ambiguous-submit protections remain unchanged.

## Tests and Acceptance Criteria

- Installer tests prove the speech environment and pinned dependencies are
  planned in dry-run and provisioned in normal mode.
- Runtime tests cover loopback-only startup, health readiness, exact model
  verification, stale-process recovery, and rejection of remote/mismatched
  endpoints.
- Setup tests require a real synthetic Kokoro narration and Whisper transcript
  before a channel can be background-enabled.
- Campaign tests prove the default configuration uses managed narration and
  blocks before rendering when speech is unavailable.
- Release tests ensure no models, caches, credentials, or runtime artifacts are
  committed or packaged.
