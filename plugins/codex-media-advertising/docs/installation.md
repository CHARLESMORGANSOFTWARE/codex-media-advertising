# Installation

Codex Media & Advertising is a public marketplace repository. It is designed
for macOS 13 or newer, Python 3.11+, Git, Chrome/Chromium, FFmpeg and a Codex
installation. API and browser extras are optional per destination.

## Install from GitHub

```bash
git clone https://github.com/ORG/codex-media-advertising.git
cd codex-media-advertising
./plugins/codex-media-advertising/scripts/install.sh
```

Add the checkout's marketplace to Codex using the Codex plugin marketplace
command or the Plugins UI, then install `codex-media-advertising` from that
marketplace. Start a new Codex task after installation and invoke
`$media-onboarding`; a task that started before installation does not reliably
discover newly installed skills.

The installer creates an isolated Python environment, installs the package and
optional local dependencies available on the machine, and leaves all runtime
state outside the checkout. It does not create credentials, launch jobs, or
publish anything.

## First setup

```bash
codex-media-ads setup --format json
codex-media-ads setup --import-secret NAME=PATH --format json
codex-media-ads publish probe --platform instagram --format json
codex-media-ads campaign validate examples/campaign.example.json --format json
```

Use your own nonsecret channel configuration with `expected_identity` and
`mode` (`api`, `browser`, or `auto`). Resolve every required `blocked` or
`missing` check before enabling a destination. Follow `$media-onboarding` for
the identity and dry-run gates.

## Upgrade and uninstall

Pull a reviewed release, reinstall with the same script, and start a new task.
When developing a local checkout, update its Codex plugin cachebuster with the
plugin-creator helper before testing a changed skill:

```bash
python3 "$CODEX_HOME/skills/.system/plugin-creator/scripts/update_plugin_cachebuster.py" \
  plugins/codex-media-advertising
```

Uninstall the package and Codex plugin from the Codex UI or CLI, then run
`./plugins/codex-media-advertising/scripts/uninstall.sh` only if you also want
to remove plugin-owned LaunchAgents. The script preserves
`~/.codex-media-ads/` so receipts, configuration, and private media survive an
upgrade; remove that directory separately only after an explicit backup and
retention decision.

Never commit the private state root, credentials, browser profiles, generated
media, queues, receipts, logs, or machine-specific paths.
