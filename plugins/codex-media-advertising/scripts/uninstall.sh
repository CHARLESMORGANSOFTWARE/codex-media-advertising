#!/bin/sh
set -eu

INSTALL_ROOT=${CODEX_MEDIA_ADS_INSTALL_ROOT:-"$HOME/.local/share/codex-media-ads"}
STATE_ROOT=${CODEX_MEDIA_ADS_STATE_ROOT:-"$HOME/.codex-media-ads"}
CLI="$INSTALL_ROOT/venv/bin/codex-media-ads"
DRY_RUN=false

usage() {
    printf '%s\n' "usage: uninstall.sh [--dry-run]"
}

if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=true
    shift
fi
if [ "$#" -ne 0 ]; then
    usage >&2
    exit 2
fi

if [ "$DRY_RUN" = true ]; then
    printf "dry-run: '%s' --state-root '%s' automation remove daily-short\n" "$CLI" "$STATE_ROOT"
    printf "dry-run: remove plugin install root '%s'\n" "$INSTALL_ROOT"
    printf "dry-run: preserve private state '%s'\n" "$STATE_ROOT"
    printf '%s\n' "dry-run: no files changed and launchctl was not invoked"
    exit 0
fi

if [ -x "$CLI" ]; then
    "$CLI" --state-root "$STATE_ROOT" automation remove daily-short >/dev/null
fi

if [ -d "$INSTALL_ROOT" ]; then
    rm -rf -- "$INSTALL_ROOT"
fi

printf '%s\n' "Uninstalled codex-media-ads; private state remains at $STATE_ROOT"
