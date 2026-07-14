from __future__ import annotations

import hashlib
import json
import shutil
import string
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests


@dataclass(frozen=True)
class ImageJob:
    prompt: str
    output_path: Path


@dataclass
class GenerationResult:
    outputs: list[Path]
    job_file: Path
    dependency: dict[str, object] | None = None


class ImageProvider(Protocol):
    command_identity: list[str]

    def generate(self, jobs: list[ImageJob]) -> GenerationResult: ...


class NarrationProvider(Protocol):
    command_identity: list[str]

    def synthesize(self, text: str, output_path: Path, voice: str) -> Path: ...


def _argument_array(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item for item in value
    ):
        raise TypeError(f"{label} must be a nonempty JSON argument array")
    return list(value)


class CodimageProvider:
    def __init__(
        self,
        project_root: Path,
        executable_prefix: list[str] | None = None,
        job_file: Path | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.executable_prefix = _argument_array(
            executable_prefix or ["uv", "run", "codimage"],
            "Codimage executable prefix",
        )
        self.job_file = Path(
            job_file or self.project_root / "codimage-jobs.jsonl"
        ).resolve()
        self.command_identity = [*self.executable_prefix, "batch"]

    def make_job(self, prompt: str, output_path: Path) -> dict[str, str]:
        return {"prompt": prompt, "out": str(Path(output_path).resolve())}

    def _invocation(self) -> list[str]:
        return [
            *self.executable_prefix,
            "batch",
            "--input",
            str(self.job_file),
            "--project-root",
            str(self.project_root),
            "--overwrite",
        ]

    def _dependency(self, detail: str) -> GenerationResult:
        return GenerationResult(
            outputs=[],
            job_file=self.job_file,
            dependency={
                "error_category": "dependency",
                "detail": detail,
                "next_action": self._invocation(),
                "job_file": str(self.job_file),
            },
        )

    def generate(self, jobs: list[ImageJob]) -> GenerationResult:
        self.job_file.parent.mkdir(parents=True, exist_ok=True)
        records = [self.make_job(job.prompt, job.output_path) for job in jobs]
        self.job_file.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
        )

        executable = self.executable_prefix[0]
        if shutil.which(executable) is None:
            return self._dependency(
                f"Codimage executable is unavailable: {executable}"
            )
        try:
            subprocess.run(
                self._invocation(),
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            return self._dependency(
                f"Codimage executable is unavailable: {executable}"
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            return self._dependency(f"Codimage is unavailable: {detail}")

        outputs = [Path(record["out"]) for record in records]
        missing = [str(path) for path in outputs if not path.is_file()]
        if missing:
            return self._dependency(
                "Codimage completed without expected output: " + ", ".join(missing)
            )
        return GenerationResult(outputs=outputs, job_file=self.job_file)


class CommandNarrationProvider:
    _allowed_fields = {"text_file", "output_path", "voice"}

    def __init__(self, command_template: list[str], work_dir: Path) -> None:
        self.command_template = _argument_array(
            command_template, "Narration command template"
        )
        self.work_dir = Path(work_dir).resolve()
        formatter = string.Formatter()
        for argument in self.command_template:
            for _, field_name, format_spec, conversion in formatter.parse(argument):
                if field_name is not None and field_name not in self._allowed_fields:
                    raise ValueError(f"unsupported placeholder: {field_name}")
                if format_spec or conversion:
                    raise ValueError("narration placeholders cannot use formatting")
        self.command_identity = list(self.command_template)

    def invocation(
        self, text: str, output_path: Path, voice: str = "alloy"
    ) -> list[str]:
        output_path = Path(output_path).resolve()
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        text_file = self.work_dir / "narration-text" / f"{digest}.txt"
        text_file.parent.mkdir(parents=True, exist_ok=True)
        text_file.write_text(text)
        values = {
            "text_file": str(text_file),
            "output_path": str(output_path),
            "voice": voice,
        }
        return [argument.format_map(values) for argument in self.command_template]

    def synthesize(self, text: str, output_path: Path, voice: str) -> Path:
        output_path = Path(output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            self.invocation(text, output_path, voice),
            check=True,
            capture_output=True,
        )
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError("narration command did not create audio output")
        return output_path


class CodovoxNarrationProvider(CommandNarrationProvider):
    def __init__(
        self,
        python_executable: Path,
        run_py: Path,
        work_dir: Path,
    ) -> None:
        super().__init__(
            [
                str(Path(python_executable)),
                str(Path(run_py).resolve()),
                "--text-file",
                "{text_file}",
                "--output-path",
                "{output_path}",
                "--voice",
                "{voice}",
            ],
            work_dir=work_dir,
        )


class SpeachesNarrationProvider:
    def __init__(
        self,
        endpoint: str,
        model: str = "speaches-ai/Kokoro-82M-v1.0-ONNX",
    ) -> None:
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("Speaches endpoint must be an HTTP endpoint")
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.command_identity = ["speaches", self.endpoint, self.model]

    def synthesize(self, text: str, output_path: Path, voice: str) -> Path:
        response = requests.post(
            self.endpoint,
            json={"input": text, "model": self.model, "voice": voice},
            timeout=120,
        )
        response.raise_for_status()
        output_path = Path(output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(response.content)
        if output_path.stat().st_size == 0:
            raise RuntimeError("Speaches returned empty audio output")
        return output_path
