#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import struct
import wave
from pathlib import Path


parser = argparse.ArgumentParser()
parser.add_argument("--text-file", required=True)
parser.add_argument("--output-path", required=True)
parser.add_argument("--voice", required=True)
args = parser.parse_args()

assert Path(args.text_file).read_text()
output = Path(args.output_path)
output.parent.mkdir(parents=True, exist_ok=True)
sample_rate = 16_000
with wave.open(str(output), "wb") as audio:
    audio.setnchannels(1)
    audio.setsampwidth(2)
    audio.setframerate(sample_rate)
    frames = (
        struct.pack("<h", int(3_000 * math.sin(2 * math.pi * 440 * n / sample_rate)))
        for n in range(sample_rate // 4)
    )
    audio.writeframes(b"".join(frames))
