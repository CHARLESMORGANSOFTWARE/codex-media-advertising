from __future__ import annotations

import argparse
import json

__version__ = "0.1.0"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codex-media-ads")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        print(json.dumps({"name": "codex-media-advertising", "version": __version__}))
    return 0
