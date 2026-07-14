from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from codex_media_ads.creative.pipeline import CreativePipeline, build_campaign
from codex_media_ads.creative.providers import (
    CodimageProvider,
    CodovoxNarrationProvider,
    CommandNarrationProvider,
    GenerationResult,
    ImageJob,
    SpeachesNarrationProvider,
)
from codex_media_ads.creative.render import (
    build_master_command,
    probe_media,
    render_master,
    render_variant,
)
from codex_media_ads.manifests import load_campaign


PLUGIN = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).with_name("fixtures")
FFMPEG = Path("/opt/homebrew/bin/ffmpeg")
FFPROBE = Path("/opt/homebrew/bin/ffprobe")


@pytest.fixture
def campaign():
    return load_campaign(PLUGIN / "examples" / "campaign.example.json")


@pytest.fixture
def codimage_provider(tmp_path: Path) -> CodimageProvider:
    return CodimageProvider(
        project_root=tmp_path,
        executable_prefix=[sys.executable, str(FIXTURES / "fake_codimage.py")],
    )


@pytest.fixture
def command_provider(tmp_path: Path) -> CommandNarrationProvider:
    executable = FIXTURES / "fake_tts.py"
    os.chmod(executable, 0o755)
    return CommandNarrationProvider(
        command_template=[
            str(executable),
            "--text-file",
            "{text_file}",
            "--output-path",
            "{output_path}",
            "--voice",
            "{voice}",
        ],
        work_dir=tmp_path,
    )


def test_codimage_job_uses_absolute_output(codimage_provider, tmp_path):
    job = codimage_provider.make_job(
        "A clean product scene. No text.", tmp_path / "scene.png"
    )
    assert Path(job["out"]).is_absolute()


def test_codimage_prefix_must_be_argument_array(tmp_path: Path):
    with pytest.raises(TypeError, match="JSON argument array"):
        CodimageProvider(project_root=tmp_path, executable_prefix="uv run codimage")


def test_codimage_generate_writes_jsonl_and_outputs(codimage_provider, tmp_path):
    output = tmp_path / "scene.png"
    result = codimage_provider.generate([ImageJob("Scene without text", output)])

    assert isinstance(result, GenerationResult)
    assert result.outputs == [output.resolve()]
    assert result.dependency is None
    assert json.loads(result.job_file.read_text().splitlines()[0])["out"] == str(
        output.resolve()
    )


def test_missing_codimage_keeps_job_and_returns_dependency(tmp_path: Path):
    provider = CodimageProvider(
        project_root=tmp_path,
        executable_prefix=[str(tmp_path / "missing-codimage")],
    )
    result = provider.generate([ImageJob("Scene", tmp_path / "scene.png")])

    assert result.outputs == []
    assert result.job_file.exists()
    assert result.dependency == {
        "error_category": "dependency",
        "detail": f"Codimage executable is unavailable: {tmp_path / 'missing-codimage'}",
        "next_action": [
            str(tmp_path / "missing-codimage"),
            "batch",
            "--input",
            str(result.job_file),
            "--project-root",
            str(tmp_path.resolve()),
            "--overwrite",
        ],
        "job_file": str(result.job_file),
    }


def test_narration_command_does_not_use_shell(command_provider, tmp_path):
    invocation = command_provider.invocation("Hello", tmp_path / "voice.wav")
    assert isinstance(invocation, list)
    assert invocation[0].endswith("fake_tts.py")


def test_narration_template_rejects_unknown_placeholder(tmp_path: Path):
    with pytest.raises(ValueError, match="unsupported placeholder"):
        CommandNarrationProvider(["tts", "{unsafe}"], work_dir=tmp_path)


def test_narration_template_rejects_shell_string(tmp_path: Path):
    with pytest.raises(TypeError, match="JSON argument array"):
        CommandNarrationProvider("tts {text_file}", work_dir=tmp_path)


def test_command_narration_synthesizes_audio(command_provider, tmp_path):
    output = command_provider.synthesize("Hello", tmp_path / "voice.wav", "alloy")
    assert output == (tmp_path / "voice.wav").resolve()
    assert output.stat().st_size > 44


def test_codovox_invocation_is_python_and_run_py_argument_array(tmp_path: Path):
    provider = CodovoxNarrationProvider(
        Path(sys.executable), tmp_path / "codovox" / "run.py", tmp_path
    )
    invocation = provider.invocation("Hello", tmp_path / "voice.wav", "af_heart")
    assert invocation[:2] == [
        sys.executable,
        str((tmp_path / "codovox" / "run.py").resolve()),
    ]


def test_speaches_writes_local_openai_compatible_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    captured = {}

    class Response:
        content = b"audio"

        def raise_for_status(self):
            return None

    def fake_post(endpoint, *, json, timeout):
        captured.update(endpoint=endpoint, json=json, timeout=timeout)
        return Response()

    monkeypatch.setattr(
        "codex_media_ads.creative.providers.requests.post", fake_post
    )
    provider = SpeachesNarrationProvider(
        "http://localhost:8000/v1/audio/speech", model="local-model"
    )
    output = provider.synthesize("Hello", tmp_path / "voice.mp3", "alloy")

    assert output.read_bytes() == b"audio"
    assert captured == {
        "endpoint": "http://localhost:8000/v1/audio/speech",
        "json": {"input": "Hello", "model": "local-model", "voice": "alloy"},
        "timeout": 120,
    }


def test_master_command_is_deterministic_argument_array(tmp_path: Path):
    command = build_master_command(
        tmp_path / "slides.txt",
        tmp_path / "voice.wav",
        tmp_path / "master.mp4",
        ffmpeg=FFMPEG,
    )
    assert command == [
        str(FFMPEG),
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(tmp_path / "slides.txt"),
        "-i",
        str(tmp_path / "voice.wav"),
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
        str(tmp_path / "master.mp4"),
    ]


class FakeImages:
    command_identity = ["fake-images-v1"]

    def generate(self, jobs: list[ImageJob]) -> GenerationResult:
        outputs = []
        for job in jobs:
            path = Path(job.output_path).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(job.prompt.encode())
            outputs.append(path)
        job_file = outputs[0].parent / "fake-jobs.jsonl"
        job_file.write_text("\n".join(str(path) for path in outputs) + "\n")
        return GenerationResult(outputs=outputs, job_file=job_file)


class FakeNarration:
    command_identity = ["fake-narration-v1"]

    def synthesize(self, text: str, output_path: Path, voice: str) -> Path:
        output_path = Path(output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(f"{voice}:{text}".encode())
        return output_path


def fake_master(images, audio, output, **kwargs):
    output = Path(output).resolve()
    output.write_bytes(b"master:" + b"|".join(Path(p).read_bytes() for p in images) + Path(audio).read_bytes())
    return output


def fake_variant(master, output, **kwargs):
    output = Path(output).resolve()
    output.write_bytes(Path(master).read_bytes() + b":variant")
    return output


@pytest.fixture
def pipeline(tmp_path: Path):
    return CreativePipeline(
        output_root=tmp_path / "build",
        image_provider=FakeImages(),
        narration_provider=FakeNarration(),
        master_renderer=fake_master,
        variant_renderer=fake_variant,
        ffmpeg=FFMPEG,
        ffprobe=FFPROBE,
    )


def test_master_render_is_reused_when_hash_matches(pipeline, campaign):
    first = pipeline.build(campaign)
    second = pipeline.build(campaign)
    assert first.master_path == second.master_path
    assert first.stages["render"].status == "built"
    assert second.stages["render"].status == "reused"


def test_changed_output_is_not_stale_reused(pipeline, campaign):
    first = pipeline.build(campaign)
    first.master_path.write_bytes(b"tampered")
    second = pipeline.build(campaign)
    assert second.stages["render"].status == "built"
    assert second.master_path.read_bytes().startswith(b"master:")


def test_stage_specific_force_only_rebuilds_requested_stage(pipeline, campaign):
    pipeline.build(campaign)
    result = pipeline.build(campaign, force={"render"})
    assert result.stages["images"].status == "reused"
    assert result.stages["narration"].status == "reused"
    assert result.stages["render"].status == "built"


def test_missing_codimage_blocks_build_with_dependency(tmp_path: Path, campaign):
    image_provider = CodimageProvider(
        project_root=tmp_path,
        executable_prefix=[str(tmp_path / "missing-codimage")],
    )
    pipeline = CreativePipeline(
        output_root=tmp_path / "build",
        image_provider=image_provider,
        narration_provider=FakeNarration(),
        master_renderer=fake_master,
        variant_renderer=fake_variant,
    )
    result = pipeline.build(campaign)

    assert result.stages["images"].status == "blocked"
    assert result.dependency["error_category"] == "dependency"
    assert Path(result.dependency["job_file"]).exists()
    assert not result.master_path.exists()


def test_build_manifest_has_campaign_input_command_audio_and_output_hashes(
    pipeline, campaign
):
    result = build_campaign(pipeline, campaign)
    manifest = json.loads(result.manifest_path.read_text())
    assert len(manifest["campaign_hash"]) == 64
    assert all(len(value) == 64 for value in manifest["input_hashes"].values())
    assert len(manifest["audio_hash"]) == 64
    assert len(manifest["stages"]["render"]["command_hash"]) == 64
    assert manifest["stages"]["render"]["output_hash"] == hashlib.sha256(
        result.master_path.read_bytes()
    ).hexdigest()


@pytest.mark.integration
def test_real_short_synthetic_ffmpeg_render(
    tmp_path: Path, command_provider: CommandNarrationProvider
):
    if not FFMPEG.exists() or not FFPROBE.exists():
        pytest.skip("Homebrew FFmpeg tools are unavailable")
    image = tmp_path / "scene.ppm"
    image.write_bytes(b"P6\n2 2\n255\n" + b"\x10\x20\x80" * 4)
    audio = command_provider.synthesize(
        "A short synthetic narration.", tmp_path / "voice.wav", "alloy"
    )

    master = render_master(
        [image], audio, tmp_path / "master.mp4", ffmpeg=FFMPEG, ffprobe=FFPROBE
    )
    variant = render_variant(
        master, tmp_path / "variant.mp4", ffmpeg=FFMPEG, ffprobe=FFPROBE
    )
    media = probe_media(variant, ffprobe=FFPROBE)

    assert media.video_codec == "h264"
    assert media.audio_codec == "aac"
    assert (media.width, media.height) == (1080, 1920)
    assert media.duration > 0
    assert variant.stat().st_size > 0
