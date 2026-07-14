from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


MASTER_RENDER_CACHE_VERSION = "ffmpeg-master-v1"
VARIANT_RENDER_CACHE_VERSION = "ffmpeg-variant-v1"


@dataclass(frozen=True)
class MediaProbe:
    video_codec: str
    audio_codec: str
    width: int
    height: int
    duration: float


def build_master_command(
    concat_file: Path,
    narration_path: Path,
    output_path: Path,
    *,
    ffmpeg: Path | str = "ffmpeg",
) -> list[str]:
    return [
        str(ffmpeg),
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-i",
        str(narration_path),
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,format=yuv420p",
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-level",
        "4.1",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-af",
        "loudnorm=I=-16:LRA=11:TP=-1.5",
        "-shortest",
        str(output_path),
    ]


def build_variant_command(
    master_path: Path,
    output_path: Path,
    *,
    ffmpeg: Path | str = "ffmpeg",
) -> list[str]:
    return [
        str(ffmpeg),
        "-y",
        "-i",
        str(master_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c",
        "copy",
        str(output_path),
    ]


def _probe_json(path: Path, ffprobe: Path | str) -> dict[str, object]:
    result = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _duration(path: Path, ffprobe: Path | str) -> float:
    data = _probe_json(path, ffprobe)
    duration = float(dict(data.get("format", {})).get("duration", 0))
    if duration <= 0:
        raise ValueError("media duration must be positive")
    return duration


def probe_media(
    path: Path, *, ffprobe: Path | str = "ffprobe"
) -> MediaProbe:
    path = Path(path).resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("rendered media must be a nonempty file")
    data = _probe_json(path, ffprobe)
    streams = data.get("streams", [])
    if not isinstance(streams, list):
        raise ValueError("ffprobe streams are invalid")
    video = next(
        (stream for stream in streams if stream.get("codec_type") == "video"), None
    )
    audio = next(
        (stream for stream in streams if stream.get("codec_type") == "audio"), None
    )
    if not isinstance(video, dict) or not isinstance(audio, dict):
        raise ValueError("render must contain video and audio streams")
    media = MediaProbe(
        video_codec=str(video.get("codec_name", "")),
        audio_codec=str(audio.get("codec_name", "")),
        width=int(video.get("width", 0)),
        height=int(video.get("height", 0)),
        duration=float(dict(data.get("format", {})).get("duration", 0)),
    )
    if media.video_codec != "h264":
        raise ValueError("render video codec must be h264")
    if media.audio_codec != "aac":
        raise ValueError("render audio codec must be aac")
    if (media.width, media.height) != (1080, 1920):
        raise ValueError("render dimensions must be 1080x1920")
    if media.duration <= 0:
        raise ValueError("render duration must be positive")
    return media


def _concat_path(output_path: Path) -> Path:
    return output_path.with_suffix(".concat.txt")


def _quoted_concat_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "'\\''")


def render_master(
    image_paths: list[Path],
    narration_path: Path,
    output_path: Path,
    *,
    ffmpeg: Path | str = "ffmpeg",
    ffprobe: Path | str = "ffprobe",
) -> Path:
    if not image_paths:
        raise ValueError("at least one image is required")
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration = _duration(Path(narration_path).resolve(), ffprobe)
    slide_duration = duration / len(image_paths)
    lines: list[str] = []
    for image_path in image_paths:
        lines.extend(
            [
                f"file '{_quoted_concat_path(Path(image_path))}'",
                f"duration {slide_duration:.9f}",
            ]
        )
    lines.append(f"file '{_quoted_concat_path(Path(image_paths[-1]))}'")
    concat_file = _concat_path(output_path)
    concat_file.write_text("\n".join(lines) + "\n")
    subprocess.run(
        build_master_command(
            concat_file, Path(narration_path).resolve(), output_path, ffmpeg=ffmpeg
        ),
        check=True,
        capture_output=True,
    )
    probe_media(output_path, ffprobe=ffprobe)
    return output_path


def render_variant(
    master_path: Path,
    output_path: Path,
    *,
    ffmpeg: Path | str = "ffmpeg",
    ffprobe: Path | str = "ffprobe",
) -> Path:
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        build_variant_command(
            Path(master_path).resolve(), output_path, ffmpeg=ffmpeg
        ),
        check=True,
        capture_output=True,
    )
    probe_media(output_path, ffprobe=ffprobe)
    return output_path
