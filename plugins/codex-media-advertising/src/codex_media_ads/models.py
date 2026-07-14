from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from .config import SENSITIVE_KEYS


class PublishStatus(StrEnum):
    PUBLISHED = "published"
    SUBMITTED = "submitted"
    SCHEDULED = "scheduled"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    FAILED = "failed"
    UNKNOWN = "unknown"


class AccountConfig(BaseModel):
    account_id: str = Field(min_length=1)
    expected_identity: str = Field(min_length=1)
    mode: str = "auto"
    secret_file: Path | None = None
    chrome_profile: str | None = None
    cdp_url: str | None = None


class PublishRequest(BaseModel):
    content_id: str
    revision: int = Field(ge=1)
    platform: str
    account: AccountConfig
    media_path: Path
    metadata: dict[str, object]
    idempotency_key: str
    dry_run: bool = False


class PublishResult(BaseModel):
    status: PublishStatus
    platform_id: str = ""
    post_url: str = ""
    evidence: dict[str, object] = Field(default_factory=dict)
    error_category: str = ""
    detail: str = ""


Destination = Literal["instagram", "tiktok", "youtube", "x", "facebook", "threads"]


def _reject_sensitive_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).lower()
            if any(part in normalized_key for part in SENSITIVE_KEYS):
                raise ValueError(f"manifest contains secret-bearing key: {key}")
            _reject_sensitive_keys(item)
    elif isinstance(value, list):
        for item in value:
            _reject_sensitive_keys(item)


def _without_output_paths(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _without_output_paths(item)
            for key, item in value.items()
            if key.lower() not in {"output_path", "output_paths"}
            and not key.lower().endswith("_output_path")
        }
    if isinstance(value, list):
        return [_without_output_paths(item) for item in value]
    return value


class CampaignManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(min_length=1)
    brand: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    rights_confirmed: Literal[True]
    audience: str = Field(min_length=1)
    offer: str = Field(min_length=1)
    proof_points: list[str]
    calls_to_action: list[str]
    visual_prompts: list[str]
    narration: str = Field(min_length=1)
    duration_seconds: int = Field(ge=1)
    destinations: list[Destination] = Field(min_length=1)
    timezone: str = Field(min_length=1)
    schedule: list[datetime]
    daily_cap: int = Field(ge=1)
    retry_limit: int = Field(ge=0)
    failure_pause_threshold: int = Field(ge=1)

    @model_validator(mode="before")
    @classmethod
    def reject_embedded_secrets(cls, value: object) -> object:
        _reject_sensitive_keys(value)
        return value

    @computed_field(return_type=str)
    @property
    def content_id(self) -> str:
        payload = self.model_dump(
            mode="json",
            exclude={"content_id", "schedule"},
        )
        payload = _without_output_paths(payload)
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()[:24]
