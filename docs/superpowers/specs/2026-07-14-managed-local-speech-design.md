# Speaches Dependency Add-On Design

## Goal

Add the existing local Speaches, Kokoro, and Whisper stack to the media plugin
as a pinned Git dependency, with installation and usage instructions. This is
an add-on to the current plugin, not a redesign.

## Scope

- Add a tracked dependency manifest that pins
  `https://github.com/speaches-ai/speaches.git` to exact revision
  `22ba05d9c00dfb4302e2403d82ad786a48db3e3b` (`v0.7.0`).
- Extend the existing installer to clone or update that revision under the
  existing private installation root, never inside the repository checkout.
- Install Speaches in its own environment using its documented `uv` workflow.
- Document the required local models:
  - `speaches-ai/Kokoro-82M-v1.0-ONNX` for narration.
  - `Systran/faster-distil-whisper-small.en` for transcription.
- Document how to start Speaches on localhost and configure the plugin's
  already-existing `SpeachesNarrationProvider` endpoint.

## Non-Goals

- No changes to campaign schemas, creative pipeline structure, CLI command
  design, queueing, receipts, social adapters, or background publishing.
- No vendoring model files or Speaches source into the release archive.
- No new service manager, daemon protocol, or endpoint abstraction.
- No removal of the existing command or Codovox narration providers.

## Safety and Acceptance

The installer remains idempotent and supports `--dry-run`. Git is required;
network or dependency failures stop installation with an actionable error.
Speech source and model caches remain private local dependencies and the
release scanner must keep rejecting them if they appear in tracked files.

Tests must prove that dry-run includes the pinned Git checkout and `uv` install,
normal installation uses the exact revision, and documentation includes the
localhost endpoint plus both model IDs. The complete existing test and release
gates must remain green.
