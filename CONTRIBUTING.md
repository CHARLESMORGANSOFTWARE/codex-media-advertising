# Contributing

Contributions should preserve the plugin's safety contracts: no credentials,
browser state, generated media, private receipts, queues, logs, or machine
specific paths in Git.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e 'plugins/codex-media-advertising[test,browser,youtube]'
python -m pytest plugins/codex-media-advertising/tests -q
```

Use test-first changes. Keep platform adapters isolated and prove success with
platform-specific IDs, URLs, or confirmation evidence. Live posting is never
part of CI; use the fully mocked six-platform test for orchestration changes.

Before opening a pull request, run:

```bash
python plugins/codex-media-advertising/scripts/scan_release.py .
python plugins/codex-media-advertising/scripts/build_release.py .
python -m compileall -q plugins/codex-media-advertising/src
git diff --check
```

Explain behavior changes, migration needs, and the verification commands in
the pull request. Do not add a dependency without documenting why it is needed
and whether it belongs in an optional extra.
