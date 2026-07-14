#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)


parser = argparse.ArgumentParser()
parser.add_argument("command", choices=["batch"])
parser.add_argument("--input", required=True)
parser.add_argument("--project-root", required=True)
parser.add_argument("--overwrite", action="store_true")
args = parser.parse_args()

for line in Path(args.input).read_text().splitlines():
    job = json.loads(line)
    output = Path(job["out"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(PNG)
