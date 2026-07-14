from __future__ import annotations

from pathlib import Path


PRIVATE_MODES = 0o700
SECRET_FILE_MODE = 0o600
SENSITIVE_KEYS = {
    "token",
    "secret",
    "password",
    "cookie",
    "authorization",
    "api_key",
}


def redact(value: object, key: str = "") -> object:
    if any(part in key.lower() for part in SENSITIVE_KEYS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def _containing_checkout(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def state_layout(
    root: Path | None = None, *, checkout: Path | None = None
) -> dict[str, Path]:
    if root is None:
        root = Path.home() / ".codex-media-ads"
    root = Path(root).expanduser().resolve()
    checkout = (
        Path(checkout).expanduser().resolve()
        if checkout is not None
        else _containing_checkout(root)
    )
    if checkout is not None and _is_within(root, checkout):
        raise ValueError("private state must be outside the Git checkout")

    root.mkdir(mode=PRIVATE_MODES, parents=True, exist_ok=True)
    root.chmod(PRIVATE_MODES)

    queue_root = root / "queue"
    queue_root.mkdir(mode=PRIVATE_MODES, parents=True, exist_ok=True)
    queue_root.chmod(PRIVATE_MODES)

    relative_paths = (
        "config",
        "secrets",
        "browser-profiles",
        "campaigns",
        "generated",
        "queue/pending",
        "queue/claims",
        "queue/completed",
        "queue/failed",
        "receipts",
        "health",
        "logs",
    )
    layout = {relative: root / relative for relative in relative_paths}
    for path in layout.values():
        path.mkdir(mode=PRIVATE_MODES, parents=True, exist_ok=True)
        path.chmod(PRIVATE_MODES)
    return layout
