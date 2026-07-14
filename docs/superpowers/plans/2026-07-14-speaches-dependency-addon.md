# Speaches Dependency Add-On Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add pinned Speaches, Kokoro, and Whisper dependencies to the existing media plugin installer and document use through the existing narration provider.

**Architecture:** A dependency lock records the upstream Speaches Git revision and required model IDs. A focused helper clones the pinned source under the private installation root and runs Speaches' documented `uv` setup; the current media pipeline and narration-provider interface remain unchanged.

**Tech Stack:** Python 3.11+ installer helper, Git, uv with managed Python 3.12, Speaches v0.7.0, Kokoro ONNX, faster-whisper, pytest, POSIX shell.

## Global Constraints

- This is additive; do not redesign existing media or publishing code.
- Pin `https://github.com/speaches-ai/speaches.git` to `22ba05d9c00dfb4302e2403d82ad786a48db3e3b`.
- Narration model: `speaches-ai/Kokoro-82M-v1.0-ONNX`.
- Transcription model: `Systran/faster-distil-whisper-small.en`.
- Install source, environments, models, and caches outside the Git checkout.
- Preserve installer idempotency, `--dry-run`, command narration, and Codovox narration.

---

### Task 1: Pinned Git Dependency Installer

**Files:**
- Create: `plugins/codex-media-advertising/dependencies/speech.lock.json`
- Create: `plugins/codex-media-advertising/scripts/install_speech.py`
- Modify: `plugins/codex-media-advertising/scripts/install.sh`
- Create: `plugins/codex-media-advertising/tests/test_speech_install.py`
- Modify: `plugins/codex-media-advertising/tests/test_launchd.py`

**Interfaces:**
- Consumes: `install.sh` values `PLUGIN_ROOT`, `INSTALL_ROOT`, and `DRY_RUN`.
- Produces: `install_speech.py --lock PATH --install-root PATH [--dry-run]`, installing source at `INSTALL_ROOT/speech/speaches` through `uv sync --frozen`.

- [ ] **Step 1: Write failing lock and dry-run tests**

```python
def test_speech_lock_pins_git_and_models(plugin_root):
    lock = json.loads((plugin_root / "dependencies/speech.lock.json").read_text())
    assert lock["git_url"] == "https://github.com/speaches-ai/speaches.git"
    assert lock["git_revision"] == "22ba05d9c00dfb4302e2403d82ad786a48db3e3b"
    assert lock["tts_model"] == "speaches-ai/Kokoro-82M-v1.0-ONNX"
    assert lock["stt_model"] == "Systran/faster-distil-whisper-small.en"


def test_speech_installer_dry_run_is_pinned_and_non_mutating(tmp_path, plugin_root):
    result = subprocess.run(
        [sys.executable, str(plugin_root / "scripts/install_speech.py"),
         "--lock", str(plugin_root / "dependencies/speech.lock.json"),
         "--install-root", str(tmp_path / "install"), "--dry-run"],
        check=True, capture_output=True, text=True,
    )
    assert "git clone" in result.stdout
    assert "22ba05d9c00dfb4302e2403d82ad786a48db3e3b" in result.stdout
    assert "uv sync --frozen" in result.stdout
    assert not (tmp_path / "install").exists()
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run:

```bash
env PYTHONPATH=plugins/codex-media-advertising/src /Applications/Telecodex30/.venv/bin/python3 -m pytest -c plugins/codex-media-advertising/pyproject.toml plugins/codex-media-advertising/tests/test_speech_install.py -q
```

Expected: fail because the lock and helper do not exist.

- [ ] **Step 3: Add the lock and minimal installer**

The lock file is exactly:

```json
{
  "schema_version": 1,
  "git_url": "https://github.com/speaches-ai/speaches.git",
  "git_revision": "22ba05d9c00dfb4302e2403d82ad786a48db3e3b",
  "python_version": "3.12",
  "uv_version": "0.10.12",
  "tts_model": "speaches-ai/Kokoro-82M-v1.0-ONNX",
  "stt_model": "Systran/faster-distil-whisper-small.en"
}
```

`install_speech.py` must validate the schema and 40-character revision, reject unsafe/symlinked install roots, print the complete plan in dry-run, install pinned `uv`, clone when absent, fetch the exact revision when present, detach-checkout the revision, verify `git rev-parse HEAD`, run `uv python install 3.12`, and run `uv sync --frozen`. Use `subprocess.run(..., check=True)` argument arrays, never a shell string.

- [ ] **Step 4: Invoke the helper from `install.sh`**

The existing dry-run branch prints the helper invocation without requiring a virtual environment. The normal branch invokes the helper after the base package install. It must preserve the current no-write/no-LaunchAgent dry-run contract.

- [ ] **Step 5: Run focused and full tests**

```bash
env PYTHONPATH=plugins/codex-media-advertising/src /Applications/Telecodex30/.venv/bin/python3 -m pytest -c plugins/codex-media-advertising/pyproject.toml plugins/codex-media-advertising/tests/test_speech_install.py plugins/codex-media-advertising/tests/test_launchd.py -q
env PYTHONPATH=plugins/codex-media-advertising/src /Applications/Telecodex30/.venv/bin/python3 -m pytest -c plugins/codex-media-advertising/pyproject.toml plugins/codex-media-advertising/tests -q
```

Expected: pass with no network calls from unit tests.

- [ ] **Step 6: Commit Task 1**

```bash
git add plugins/codex-media-advertising/dependencies plugins/codex-media-advertising/scripts plugins/codex-media-advertising/tests/test_speech_install.py plugins/codex-media-advertising/tests/test_launchd.py
git commit -m "feat: add pinned local speech dependencies"
```

---

### Task 2: Usage Instructions and Release Gate

**Files:**
- Modify: `README.md`
- Modify: `plugins/codex-media-advertising/docs/installation.md`
- Modify: `plugins/codex-media-advertising/docs/authentication.md`
- Modify: `plugins/codex-media-advertising/skills/media-onboarding/SKILL.md`
- Modify: `plugins/codex-media-advertising/tests/test_skills.py`
- Modify: `plugins/codex-media-advertising/tests/test_release_tools.py`

**Interfaces:**
- Consumes: installed `INSTALL_ROOT/speech/speaches` and existing `NarrationRuntimeConfig(provider="speaches", endpoint=...)`.
- Produces: exact start command, endpoint, models, configuration example, and validation sequence.

- [ ] **Step 1: Add failing documentation tests**

```python
def test_installation_documents_speech_dependency(plugin_root):
    text = (plugin_root / "docs/installation.md").read_text()
    assert "speech/speaches" in text
    assert "127.0.0.1:8000" in text
    assert "speaches-ai/Kokoro-82M-v1.0-ONNX" in text
    assert "Systran/faster-distil-whisper-small.en" in text
    assert '"provider": "speaches"' in text
    assert '"endpoint": "http://127.0.0.1:8000/v1/audio/speech"' in text
```

- [ ] **Step 2: Run the focused test and confirm RED**

```bash
env PYTHONPATH=plugins/codex-media-advertising/src /Applications/Telecodex30/.venv/bin/python3 -m pytest -c plugins/codex-media-advertising/pyproject.toml plugins/codex-media-advertising/tests/test_skills.py -q
```

Expected: fail until the dependency usage is documented.

- [ ] **Step 3: Document the existing-provider flow**

Document this start command:

```bash
cd "$HOME/.local/share/codex-media-ads/speech/speaches"
"$HOME/.local/share/codex-media-ads/venv/bin/uv" run uvicorn --factory \
  --host 127.0.0.1 --port 8000 speaches.main:create_app
```

Document this existing creative-runtime fragment:

```json
{
  "narration": {
    "provider": "speaches",
    "endpoint": "http://127.0.0.1:8000/v1/audio/speech",
    "model": "speaches-ai/Kokoro-82M-v1.0-ONNX"
  },
  "voice": "af_heart"
}
```

State that first use downloads models to private cache, Kokoro creates narration, Whisper provides transcription, and `codex-media-ads setup --format json` must show `narration_provider: ok` before video creation.

- [ ] **Step 4: Extend release-scanner tests**

Add cases proving `speech/speaches/.venv/`, model-cache paths, and model/audio binaries are blocked from tracked files and archives while the lock file and documentation references are allowed.

- [ ] **Step 5: Run the complete release gate**

```bash
env PYTHONPATH=plugins/codex-media-advertising/src /Applications/Telecodex30/.venv/bin/python3 -m pytest -c plugins/codex-media-advertising/pyproject.toml plugins/codex-media-advertising/tests -q
/usr/bin/python3 plugins/codex-media-advertising/scripts/scan_release.py .
/usr/bin/python3 plugins/codex-media-advertising/scripts/build_release.py
/usr/bin/python3 /Users/charlesemorganiv/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/codex-media-advertising
git diff --check
git status --short
```

Expected: all gates pass; the archive contains the lock and instructions but no speech checkout, environment, cache, models, or generated audio.

- [ ] **Step 6: Commit and publish**

```bash
git add README.md plugins/codex-media-advertising/docs plugins/codex-media-advertising/skills/media-onboarding/SKILL.md plugins/codex-media-advertising/tests
git commit -m "docs: add local speech setup instructions"
git push origin main
```
