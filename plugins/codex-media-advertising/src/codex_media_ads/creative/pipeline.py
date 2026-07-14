from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Literal

from ..models import CampaignManifest
from .providers import ImageJob, ImageProvider, NarrationProvider
from .render import (
    MASTER_RENDER_CACHE_VERSION,
    VARIANT_RENDER_CACHE_VERSION,
    build_master_command,
    build_variant_command,
    render_master,
    render_variant,
)


StageStatus = Literal["built", "reused", "blocked", "failed"]


@dataclass
class StageResult:
    status: StageStatus
    input_hash: str
    command_hash: str
    output_hash: str
    path: Path
    detail: str = ""

    def to_json(self) -> dict[str, str]:
        data = asdict(self)
        data["path"] = str(self.path)
        return data


@dataclass
class BuildResult:
    master_path: Path
    variant_paths: dict[str, Path]
    stages: dict[str, StageResult]
    manifest_path: Path
    dependency: dict[str, object] | None = None
    failure: dict[str, object] | None = None


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_json(value: object) -> str:
    return _sha_bytes(
        json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
    )


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _command_identity(provider: object) -> list[str]:
    identity = getattr(provider, "command_identity", None)
    if isinstance(identity, list) and all(isinstance(item, str) for item in identity):
        return identity
    return [provider.__class__.__module__, provider.__class__.__qualname__]


def _renderer_identity(
    renderer: Callable[..., Path],
    default_renderer: Callable[..., Path],
    default_version: str,
) -> tuple[list[str], bool]:
    if renderer is default_renderer:
        return [default_version], True
    identity = getattr(renderer, "cache_identity", None)
    if identity is None:
        identity = getattr(renderer, "version", None)
    if isinstance(identity, str) and identity:
        return [identity], True
    if isinstance(identity, list) and identity and all(
        isinstance(item, str) and item for item in identity
    ):
        return list(identity), True
    return ["custom-renderer-without-cache-identity"], False


def _safe_exception_detail(exc: Exception) -> str:
    return f"{type(exc).__name__}: stage execution failed"


def _failure(error_category: str, stage: str, detail: str) -> dict[str, object]:
    return {
        "error_category": error_category,
        "stage": stage,
        "detail": detail,
        "next_action": f"Resolve the {stage} failure and rerun that stage.",
    }


class CreativePipeline:
    def __init__(
        self,
        output_root: Path,
        image_provider: ImageProvider,
        narration_provider: NarrationProvider,
        *,
        voice: str = "alloy",
        ffmpeg: Path | str = "ffmpeg",
        ffprobe: Path | str = "ffprobe",
        master_renderer: Callable[..., Path] = render_master,
        variant_renderer: Callable[..., Path] = render_variant,
    ) -> None:
        self.output_root = Path(output_root).resolve()
        self.image_provider = image_provider
        self.narration_provider = narration_provider
        self.voice = voice
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.master_renderer = master_renderer
        self.variant_renderer = variant_renderer

    def _build_directory(self, campaign_id: str) -> Path:
        candidate = (self.output_root / campaign_id).resolve()
        try:
            relative = candidate.relative_to(self.output_root)
        except ValueError as exc:
            raise ValueError(
                "campaign build directory must be a strict descendant of output_root"
            ) from exc
        if relative == Path("."):
            raise ValueError(
                "campaign build directory must be a strict descendant of output_root"
            )
        return candidate

    @staticmethod
    def _validate_output_targets(build_dir: Path, targets: list[Path]) -> None:
        for target in targets:
            try:
                relative = target.relative_to(build_dir)
            except ValueError as exc:
                raise ValueError(
                    "output target escapes campaign build directory"
                ) from exc
            current = build_dir
            for part in relative.parts:
                current = current / part
                if current.is_symlink():
                    raise ValueError(
                        f"symlinked output component is forbidden: {current}"
                    )
            try:
                target.resolve().relative_to(build_dir)
            except ValueError as exc:
                raise ValueError(
                    "output target escapes campaign build directory"
                ) from exc

    @staticmethod
    def _write_manifest(
        manifest_path: Path,
        campaign_hash: str,
        input_hashes: dict[str, str],
        audio_hash: str,
        stage_records: dict[str, object],
        *,
        dependency: dict[str, object] | None = None,
        failure: dict[str, object] | None = None,
    ) -> None:
        manifest: dict[str, object] = {
            "campaign_hash": campaign_hash,
            "input_hashes": input_hashes,
            "audio_hash": audio_hash,
            "stages": stage_records,
        }
        if dependency is not None:
            manifest["dependency"] = dependency
        if failure is not None:
            manifest["failure"] = failure
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )

    @staticmethod
    def _load_manifest(path: Path) -> dict[str, object]:
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _can_reuse(
        previous: dict[str, object],
        input_hash: str,
        command_hash: str,
        output_paths: list[Path],
    ) -> bool:
        if (
            previous.get("input_hash") != input_hash
            or previous.get("command_hash") != command_hash
        ):
            return False
        expected = previous.get("output_hashes")
        if not isinstance(expected, dict):
            return False
        return all(
            path.is_file()
            and expected.get(str(path)) == _sha_file(path)
            for path in output_paths
        )

    @staticmethod
    def _stage(
        status: StageStatus,
        input_hash: str,
        command_hash: str,
        paths: list[Path],
        path: Path,
        detail: str = "",
    ) -> tuple[StageResult, dict[str, object]]:
        output_hashes = {
            str(output): _sha_file(output)
            for output in paths
            if output.is_file()
        }
        if len(output_hashes) == 1:
            output_hash = next(iter(output_hashes.values()))
        else:
            output_hash = _sha_json(output_hashes) if output_hashes else ""
        result = StageResult(
            status=status,
            input_hash=input_hash,
            command_hash=command_hash,
            output_hash=output_hash,
            path=path,
            detail=detail,
        )
        record: dict[str, object] = result.to_json()
        record["output_hashes"] = output_hashes
        return result, record

    def build(
        self, campaign: CampaignManifest, force: set[str] | None = None
    ) -> BuildResult:
        force = set(force or ())
        build_dir = self._build_directory(campaign.campaign_id)
        images_dir = build_dir / "images"
        variants_dir = build_dir / "variants"
        manifest_path = build_dir / "build-manifest.json"
        master_path = build_dir / "master.mp4"
        audio_path = build_dir / "narration.wav"
        image_paths = [
            images_dir / f"scene-{index:03d}.png"
            for index, _ in enumerate(campaign.visual_prompts, 1)
        ]
        variant_targets = {
            destination: variants_dir / f"{destination}.mp4"
            for destination in campaign.destinations
        }
        self._validate_output_targets(
            build_dir,
            [
                images_dir,
                variants_dir,
                manifest_path,
                master_path,
                master_path.with_suffix(".concat.txt"),
                audio_path,
                *image_paths,
                *variant_targets.values(),
            ],
        )
        build_dir.mkdir(parents=True, exist_ok=True)
        images_dir.mkdir(parents=True, exist_ok=True)
        variants_dir.mkdir(parents=True, exist_ok=True)

        previous = self._load_manifest(manifest_path)
        previous_stages = previous.get("stages", {})
        if not isinstance(previous_stages, dict):
            previous_stages = {}
        stages: dict[str, StageResult] = {}
        stage_records: dict[str, object] = {}
        campaign_hash = _sha_json(
            campaign.model_dump(mode="json", exclude={"content_id"})
        )

        image_input_hash = _sha_json(campaign.visual_prompts)
        image_command_hash = _sha_json(_command_identity(self.image_provider))
        previous_images = previous_stages.get("images", {})
        if not isinstance(previous_images, dict):
            previous_images = {}
        reuse_images = "images" not in force and self._can_reuse(
            previous_images, image_input_hash, image_command_hash, image_paths
        )
        if reuse_images:
            status: StageStatus = "reused"
        else:
            generation = self.image_provider.generate(
                [
                    ImageJob(prompt=prompt, output_path=path)
                    for prompt, path in zip(campaign.visual_prompts, image_paths)
                ]
            )
            if generation.dependency is not None:
                stage, record = self._stage(
                    "blocked",
                    image_input_hash,
                    image_command_hash,
                    [],
                    images_dir,
                )
                stages["images"] = stage
                stage_records["images"] = record
                self._write_manifest(
                    manifest_path,
                    campaign_hash,
                    {},
                    "",
                    stage_records,
                    dependency=generation.dependency,
                )
                return BuildResult(
                    master_path=master_path,
                    variant_paths={},
                    stages=stages,
                    manifest_path=manifest_path,
                    dependency=generation.dependency,
                )
            image_paths = generation.outputs
            status = "built"
        stage, record = self._stage(
            status,
            image_input_hash,
            image_command_hash,
            image_paths,
            images_dir,
        )
        stages["images"] = stage
        stage_records["images"] = record
        input_hashes = {str(path): _sha_file(path) for path in image_paths}

        narration_input_hash = _sha_json(
            {"text": campaign.narration, "voice": self.voice}
        )
        narration_command_hash = _sha_json(
            _command_identity(self.narration_provider)
        )
        previous_narration = previous_stages.get("narration", {})
        if not isinstance(previous_narration, dict):
            previous_narration = {}
        if "narration" not in force and self._can_reuse(
            previous_narration,
            narration_input_hash,
            narration_command_hash,
            [audio_path],
        ):
            narration_status: StageStatus = "reused"
        else:
            try:
                audio_path = self.narration_provider.synthesize(
                    campaign.narration, audio_path, self.voice
                )
            except Exception as exc:
                detail = _safe_exception_detail(exc)
                failure = _failure("dependency", "narration", detail)
                stage, record = self._stage(
                    "failed",
                    narration_input_hash,
                    narration_command_hash,
                    [],
                    audio_path,
                    detail,
                )
                stages["narration"] = stage
                stage_records["narration"] = record
                self._write_manifest(
                    manifest_path,
                    campaign_hash,
                    input_hashes,
                    "",
                    stage_records,
                    failure=failure,
                )
                return BuildResult(
                    master_path=master_path,
                    variant_paths={},
                    stages=stages,
                    manifest_path=manifest_path,
                    failure=failure,
                )
            narration_status = "built"
        stage, record = self._stage(
            narration_status,
            narration_input_hash,
            narration_command_hash,
            [audio_path],
            audio_path,
        )
        stages["narration"] = stage
        stage_records["narration"] = record

        audio_hash = _sha_file(audio_path)
        render_input_hash = _sha_json(
            {"images": input_hashes, "audio": audio_hash}
        )
        concat_path = master_path.with_suffix(".concat.txt")
        render_identity, render_reusable = _renderer_identity(
            self.master_renderer,
            render_master,
            MASTER_RENDER_CACHE_VERSION,
        )
        render_command_hash = _sha_json(
            {
                "renderer": render_identity,
                "command": build_master_command(
                    concat_path, audio_path, master_path, ffmpeg=self.ffmpeg
                ),
            }
        )
        previous_render = previous_stages.get("render", {})
        if not isinstance(previous_render, dict):
            previous_render = {}
        if render_reusable and "render" not in force and self._can_reuse(
            previous_render,
            render_input_hash,
            render_command_hash,
            [master_path],
        ):
            render_status: StageStatus = "reused"
        else:
            try:
                master_path = self.master_renderer(
                    image_paths,
                    audio_path,
                    master_path,
                    ffmpeg=self.ffmpeg,
                    ffprobe=self.ffprobe,
                )
            except Exception as exc:
                detail = _safe_exception_detail(exc)
                failure = _failure("render", "render", detail)
                stage, record = self._stage(
                    "failed",
                    render_input_hash,
                    render_command_hash,
                    [],
                    master_path,
                    detail,
                )
                stages["render"] = stage
                stage_records["render"] = record
                self._write_manifest(
                    manifest_path,
                    campaign_hash,
                    input_hashes,
                    audio_hash,
                    stage_records,
                    failure=failure,
                )
                return BuildResult(
                    master_path=master_path,
                    variant_paths={},
                    stages=stages,
                    manifest_path=manifest_path,
                    failure=failure,
                )
            render_status = "built"
        stage, record = self._stage(
            render_status,
            render_input_hash,
            render_command_hash,
            [master_path],
            master_path,
        )
        stages["render"] = stage
        stage_records["render"] = record

        variant_paths: dict[str, Path] = {}
        master_hash = _sha_file(master_path)
        for destination in campaign.destinations:
            stage_name = f"variant:{destination}"
            variant_path = variant_targets[destination]
            variant_input_hash = _sha_json(
                {"master": master_hash, "destination": destination}
            )
            variant_identity, variant_reusable = _renderer_identity(
                self.variant_renderer,
                render_variant,
                VARIANT_RENDER_CACHE_VERSION,
            )
            variant_command_hash = _sha_json(
                {
                    "renderer": variant_identity,
                    "command": build_variant_command(
                        master_path, variant_path, ffmpeg=self.ffmpeg
                    ),
                }
            )
            previous_variant = previous_stages.get(stage_name, {})
            if not isinstance(previous_variant, dict):
                previous_variant = {}
            if (
                variant_reusable
                and "variants" not in force
                and stage_name not in force
                and self._can_reuse(
                    previous_variant,
                    variant_input_hash,
                    variant_command_hash,
                    [variant_path],
                )
            ):
                variant_status: StageStatus = "reused"
            else:
                try:
                    self.variant_renderer(
                        master_path,
                        variant_path,
                        ffmpeg=self.ffmpeg,
                        ffprobe=self.ffprobe,
                    )
                except Exception as exc:
                    detail = _safe_exception_detail(exc)
                    failure = _failure("render", stage_name, detail)
                    stage, record = self._stage(
                        "failed",
                        variant_input_hash,
                        variant_command_hash,
                        [],
                        variant_path,
                        detail,
                    )
                    stages[stage_name] = stage
                    stage_records[stage_name] = record
                    self._write_manifest(
                        manifest_path,
                        campaign_hash,
                        input_hashes,
                        audio_hash,
                        stage_records,
                        failure=failure,
                    )
                    return BuildResult(
                        master_path=master_path,
                        variant_paths=variant_paths,
                        stages=stages,
                        manifest_path=manifest_path,
                        failure=failure,
                    )
                variant_status = "built"
            stage, record = self._stage(
                variant_status,
                variant_input_hash,
                variant_command_hash,
                [variant_path],
                variant_path,
            )
            stages[stage_name] = stage
            stage_records[stage_name] = record
            variant_paths[destination] = variant_path

        self._write_manifest(
            manifest_path,
            campaign_hash,
            input_hashes,
            audio_hash,
            stage_records,
        )
        return BuildResult(
            master_path=master_path,
            variant_paths=variant_paths,
            stages=stages,
            manifest_path=manifest_path,
        )


def build_campaign(
    pipeline: CreativePipeline,
    campaign: CampaignManifest,
    force: set[str] | None = None,
) -> BuildResult:
    return pipeline.build(campaign, force=force)
