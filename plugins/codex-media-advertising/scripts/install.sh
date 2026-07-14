#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PLUGIN_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
INSTALL_ROOT=${CODEX_MEDIA_ADS_INSTALL_ROOT:-"$HOME/.local/share/codex-media-ads"}
STATE_ROOT=${CODEX_MEDIA_ADS_STATE_ROOT:-"$HOME/.codex-media-ads"}
PYTHON_BIN=${PYTHON_BIN:-python3}
DRY_RUN=false

usage() {
    printf '%s\n' "usage: install.sh [--dry-run]"
}

if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=true
    shift
fi
if [ "$#" -ne 0 ]; then
    usage >&2
    exit 2
fi

print_command() {
    printf 'dry-run:'
    for argument in "$@"; do
        printf ' %s' "$(printf '%s' "$argument" | sed "s/'/'\\\\''/g; s/^/'/; s/$/'/")"
    done
    printf '\n'
}

if [ "$DRY_RUN" = true ]; then
    print_command mkdir -p "$INSTALL_ROOT" "$STATE_ROOT"
    print_command chmod 700 "$INSTALL_ROOT" "$STATE_ROOT"
    print_command "$PYTHON_BIN" -m venv "$INSTALL_ROOT/venv"
    print_command "$INSTALL_ROOT/venv/bin/python" -m pip install --upgrade "$PLUGIN_ROOT"
    printf '%s\n' "dry-run: no files changed and no LaunchAgent loaded"
    exit 0
fi

mkdir -p "$INSTALL_ROOT" "$STATE_ROOT"
chmod 700 "$INSTALL_ROOT" "$STATE_ROOT"
"$PYTHON_BIN" -m venv "$INSTALL_ROOT/venv"
"$INSTALL_ROOT/venv/bin/python" -m pip install --upgrade "$PLUGIN_ROOT"

printf '%s\n' "Installed codex-media-ads at $INSTALL_ROOT/venv/bin/codex-media-ads"
printf '%s\n' "Private state is preserved at $STATE_ROOT"
printf '%s\n' "Run codex-media-ads setup before installing background automation."
