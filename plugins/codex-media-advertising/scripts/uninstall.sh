#!/bin/sh
set -eu

if [ "${CODEX_MEDIA_ADS_INSTALL_ROOT+x}" = x ]; then
    INSTALL_ROOT=$CODEX_MEDIA_ADS_INSTALL_ROOT
else
    INSTALL_ROOT="$HOME/.local/share/codex-media-ads"
fi
STATE_ROOT=${CODEX_MEDIA_ADS_STATE_ROOT:-"$HOME/.codex-media-ads"}
CLI="$INSTALL_ROOT/venv/bin/codex-media-ads"
DRY_RUN=false

usage() {
    printf '%s\n' "usage: uninstall.sh [--dry-run]"
}

unsafe_install_root() {
    printf 'unsafe install root: %s\n' "$1" >&2
    exit 2
}

[ -n "${HOME:-}" ] || unsafe_install_root "HOME is empty"
[ "$HOME" != "/" ] || unsafe_install_root "HOME cannot be filesystem root"
[ -n "$INSTALL_ROOT" ] || unsafe_install_root "path is empty"
case "$INSTALL_ROOT" in
    /*) ;;
    *) unsafe_install_root "path must be absolute" ;;
esac
case "$STATE_ROOT" in
    /*) ;;
    *) unsafe_install_root "state path must be absolute" ;;
esac
[ "$STATE_ROOT" != "/" ] || unsafe_install_root "state path cannot be filesystem root"
case "$INSTALL_ROOT" in
    *//*|*/./*|*/../*|*/.|*/..) unsafe_install_root "path is not lexical" ;;
esac
case "$INSTALL_ROOT" in
    "$HOME"/*/codex-media-ads) ;;
    *) unsafe_install_root "path must be a plugin-named descendant of HOME" ;;
esac
case "$INSTALL_ROOT:$STATE_ROOT" in
    "$STATE_ROOT:$STATE_ROOT"|"$STATE_ROOT"/*:"$STATE_ROOT"|"$INSTALL_ROOT":"$INSTALL_ROOT"/*)
        unsafe_install_root "path overlaps private state"
        ;;
esac

canonical_path() {
    /usr/bin/python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

[ -x /usr/bin/python3 ] || unsafe_install_root "cannot resolve physical paths"
HOME_PHYSICAL=$(canonical_path "$HOME") || unsafe_install_root "cannot resolve HOME"
INSTALL_PHYSICAL=$(canonical_path "$INSTALL_ROOT") || unsafe_install_root "cannot resolve install path"
STATE_PHYSICAL=$(canonical_path "$STATE_ROOT") || unsafe_install_root "cannot resolve state path"

case "$INSTALL_PHYSICAL" in
    "$HOME_PHYSICAL"/*/codex-media-ads) ;;
    *) unsafe_install_root "physical path must be a plugin-named descendant of HOME" ;;
esac

is_same_or_descendant() {
    [ "$1" = "$2" ] && return 0
    case "$1" in
        "$2"/*) return 0 ;;
        *) return 1 ;;
    esac
}

if is_same_or_descendant "$INSTALL_PHYSICAL" "$STATE_PHYSICAL" ||
    is_same_or_descendant "$STATE_PHYSICAL" "$INSTALL_PHYSICAL"; then
    unsafe_install_root "physical path overlaps private state"
fi

component=$INSTALL_ROOT
while [ "$component" != "$HOME" ]; do
    [ ! -L "$component" ] || unsafe_install_root "path contains a symlink"
    component=${component%/*}
    [ -n "$component" ] || unsafe_install_root "path escapes HOME"
done

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
