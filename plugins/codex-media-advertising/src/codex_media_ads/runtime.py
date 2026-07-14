from __future__ import annotations

import atexit
import json
import os
import stat
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import state_layout
from .creative.pipeline import CreativePipeline
from .creative.providers import (
    CodimageProvider,
    CodovoxNarrationProvider,
    CommandNarrationProvider,
    SpeachesNarrationProvider,
)
from .models import AccountConfig
from .orchestrator import Orchestrator
from .publishing.base import PublisherAdapter, SUPPORTED_PLATFORMS
from .publishing.router import PublisherRouter
from .queueing import QueueStore


class RuntimeConfigurationError(ValueError):
    def __init__(self, detail: str, next_action: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self.next_action = next_action


class CodimageRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["codimage"]
    project_root: Path
    executable_prefix: list[str] | None = None
    job_file: Path | None = None


class NarrationRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["command", "codovox", "speaches"]
    command_template: list[str] | None = None
    work_dir: Path | None = None
    python_executable: Path | None = None
    run_py: Path | None = None
    endpoint: str | None = None
    model: str = "speaches-ai/Kokoro-82M-v1.0-ONNX"

    @model_validator(mode="after")
    def validate_provider_fields(self) -> NarrationRuntimeConfig:
        if self.provider == "command" and (
            not self.command_template or self.work_dir is None
        ):
            raise ValueError("command narration requires command_template and work_dir")
        if self.provider == "codovox" and (
            self.python_executable is None
            or self.run_py is None
            or self.work_dir is None
        ):
            raise ValueError(
                "codovox narration requires python_executable, run_py, and work_dir"
            )
        if self.provider == "speaches" and not self.endpoint:
            raise ValueError("speaches narration requires endpoint")
        return self


class CreativeRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: CodimageRuntimeConfig
    narration: NarrationRuntimeConfig
    voice: str = "alloy"
    ffmpeg: Path | str = "ffmpeg"
    ffprobe: Path | str = "ffprobe"


class BrowserRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chrome_path: Path
    profile_source: Path
    extra_args: list[str] = Field(default_factory=list)
    startup_timeout: float = Field(default=15.0, gt=0)


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accounts: dict[str, AccountConfig]
    creative: CreativeRuntimeConfig | None = None
    browser: BrowserRuntimeConfig | None = None
    daily_cap: int = Field(default=20, ge=1)
    timezone: str = "UTC"
    retry_limit: int = Field(default=1, ge=0)
    failure_pause_threshold: int = Field(default=2, ge=1)

    @model_validator(mode="after")
    def validate_accounts(self) -> RuntimeConfig:
        unsupported = sorted(set(self.accounts) - set(SUPPORTED_PLATFORMS))
        if unsupported:
            raise ValueError(
                "unsupported configured platforms: " + ", ".join(unsupported)
            )
        for platform, account in self.accounts.items():
            if account.mode.strip().casefold() not in {"api", "browser", "auto"}:
                raise ValueError(f"invalid account mode for {platform}: {account.mode}")
        return self


def _resolve(path: Path | None, base: Path) -> Path | None:
    if path is None:
        return None
    path = path.expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _absolute_without_resolving(path: Path, base: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = base / expanded
    return Path(os.path.abspath(os.path.normpath(os.fspath(expanded))))


def _secure_secret_path(path: Path, *, base: Path, secrets_root: Path) -> Path:
    candidate = _absolute_without_resolving(path, base)
    root = _absolute_without_resolving(secrets_root, secrets_root.parent)
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeConfigurationError(
            "Configured API secret files must remain inside the private secrets directory.",
            f"Move the secret file under {root} and update runtime.json.",
        ) from exc
    if not relative.parts:
        raise RuntimeConfigurationError(
            "The private secrets directory cannot be used as a secret file.",
            f"Configure an owner-only regular JSON file under {root}.",
        )

    current = root
    components = (root, *(root / Path(*relative.parts[:index]) for index in range(1, len(relative.parts) + 1)))
    for index, current in enumerate(components):
        try:
            info = current.lstat()
        except OSError as exc:
            raise RuntimeConfigurationError(
                f"Cannot access the configured API secret path component: {current}",
                "Create the private secret path with owner-only permissions and retry.",
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeConfigurationError(
                f"Configured API secret paths cannot contain symlinks: {current}",
                "Replace the symlink with an owner-only regular directory or file inside the private secrets directory.",
            )
        is_leaf = index == len(components) - 1
        expected_type = stat.S_ISREG if is_leaf else stat.S_ISDIR
        if not expected_type(info.st_mode):
            kind = "file" if is_leaf else "directory"
            raise RuntimeConfigurationError(
                f"Configured API secret path component is not a regular {kind}: {current}",
                "Repair the private secret path and retry.",
            )
        if stat.S_IMODE(info.st_mode) & 0o077:
            expected_mode = "0600" if is_leaf else "0700"
            raise RuntimeConfigurationError(
                f"Configured API secret path components must be owner-only: {current}",
                f"Set mode {expected_mode} on {current} and retry.",
            )
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise RuntimeConfigurationError(
                f"Configured API secret path components must be owned by the current user: {current}",
                "Correct the secret path ownership and retry.",
            )
    return candidate


def _read_runtime_config(path: Path) -> RuntimeConfig:
    if path.is_symlink() or not path.is_file():
        raise RuntimeConfigurationError(
            f"Runtime configuration is missing: {path}",
            f"Create the private runtime configuration at {path}.",
        )
    stat = path.stat()
    if hasattr(os, "getuid") and stat.st_uid != os.getuid():
        raise RuntimeConfigurationError(
            "Runtime configuration must be owned by the current user.",
            f"Correct ownership and set mode 0600 on {path}.",
        )
    if stat.st_mode & 0o077:
        raise RuntimeConfigurationError(
            "Runtime configuration must not be accessible by group or other users.",
            f"Run chmod 600 on {path}.",
        )
    return RuntimeConfig.model_validate(json.loads(path.read_text()))


def _resolved_accounts(
    accounts: dict[str, AccountConfig], config_root: Path, secrets_root: Path
) -> dict[str, AccountConfig]:
    return {
        platform: account.model_copy(
            update={
                "secret_file": (
                    _secure_secret_path(
                        account.secret_file,
                        base=config_root,
                        secrets_root=secrets_root,
                    )
                    if account.secret_file is not None
                    else None
                )
            }
        )
        for platform, account in accounts.items()
    }


def _creative_pipeline(
    config: CreativeRuntimeConfig,
    *,
    config_root: Path,
    output_root: Path,
) -> CreativePipeline:
    image = config.image
    project_root = _resolve(image.project_root, config_root)
    assert project_root is not None
    image_provider = CodimageProvider(
        project_root=project_root,
        executable_prefix=image.executable_prefix,
        job_file=_resolve(image.job_file, config_root),
    )
    narration = config.narration
    if narration.provider == "command":
        assert narration.command_template is not None
        work_dir = _resolve(narration.work_dir, config_root)
        assert work_dir is not None
        narration_provider = CommandNarrationProvider(
            narration.command_template,
            work_dir,
        )
    elif narration.provider == "codovox":
        python_executable = _resolve(narration.python_executable, config_root)
        run_py = _resolve(narration.run_py, config_root)
        work_dir = _resolve(narration.work_dir, config_root)
        assert python_executable is not None and run_py is not None and work_dir is not None
        narration_provider = CodovoxNarrationProvider(
            python_executable,
            run_py,
            work_dir,
        )
    else:
        assert narration.endpoint is not None
        narration_provider = SpeachesNarrationProvider(
            narration.endpoint,
            narration.model,
        )
    ffmpeg = config.ffmpeg
    if isinstance(ffmpeg, Path):
        ffmpeg = _resolve(ffmpeg, config_root)
        assert ffmpeg is not None
    ffprobe = config.ffprobe
    if isinstance(ffprobe, Path):
        ffprobe = _resolve(ffprobe, config_root)
        assert ffprobe is not None
    return CreativePipeline(
        output_root=output_root,
        image_provider=image_provider,
        narration_provider=narration_provider,
        voice=config.voice,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )


class _LazyBrowserAdapter:
    def __init__(
        self,
        platform: str,
        account: AccountConfig,
        browser: BrowserRuntimeConfig | None,
        state_root: Path,
        config_root: Path,
    ) -> None:
        self.platform = platform
        self.account = account
        self.browser = browser
        self.state_root = state_root
        self.config_root = config_root
        self._adapter: PublisherAdapter | None = None
        atexit.register(self.close)

    def _get(self) -> PublisherAdapter:
        if self._adapter is not None:
            return self._adapter
        from .publishing.browser_adapters import (
            BrowserPublisher,
            PlaywrightBrowserPage,
        )

        if self.account.cdp_url:
            page = PlaywrightBrowserPage.connect(self.account.cdp_url)
            self._adapter = BrowserPublisher(self.platform, page)
            return self._adapter
        if self.browser is None or not self.account.chrome_profile:
            raise RuntimeConfigurationError(
                f"Browser runtime is not configured for {self.platform}.",
                "Configure browser.chrome_path, browser.profile_source, and the account chrome_profile.",
            )
        from .publishing.chrome import launch_chrome

        chrome_path = _resolve(self.browser.chrome_path, self.config_root)
        profile_source = _resolve(self.browser.profile_source, self.config_root)
        assert chrome_path is not None and profile_source is not None
        chrome = launch_chrome(
            chrome_path=chrome_path,
            profile_source=profile_source,
            profile_name=self.account.chrome_profile,
            state_root=self.state_root,
            extra_args=self.browser.extra_args,
            startup_timeout=self.browser.startup_timeout,
        )
        self._adapter = BrowserPublisher.from_managed_chrome(self.platform, chrome)
        return self._adapter

    def probe_auth(self, account: AccountConfig):
        return self._get().probe_auth(account)

    def validate(self, request):
        return self._get().validate(request)

    def publish(self, request):
        return self._get().publish(request)

    def close(self) -> None:
        if self._adapter is None:
            return
        close = getattr(self._adapter, "close", None)
        if close is not None:
            close()
        self._adapter = None


def load_runtime(
    state_root: Path,
    *,
    require_configuration: bool = False,
) -> Orchestrator:
    layout = state_layout(state_root)
    root = layout["config"].parent
    queue_store = QueueStore(root)
    runtime_path = layout["config"] / "runtime.json"
    if not runtime_path.is_file():
        if require_configuration:
            raise RuntimeConfigurationError(
                f"Runtime configuration is missing: {runtime_path}",
                f"Create the private runtime configuration at {runtime_path}.",
            )
        return Orchestrator(queue_store=queue_store)

    config = _read_runtime_config(runtime_path)
    config_root = runtime_path.parent
    accounts = _resolved_accounts(
        config.accounts,
        config_root,
        layout["secrets"],
    )

    from .publishing.api_adapters import MetaPublisher, XPublisher, YouTubePublisher

    api_adapters: dict[str, PublisherAdapter] = {}
    if "instagram" in accounts:
        api_adapters["instagram"] = MetaPublisher("instagram")
    if "facebook" in accounts:
        api_adapters["facebook"] = MetaPublisher("facebook")
    if "youtube" in accounts:
        api_adapters["youtube"] = YouTubePublisher()
    if "x" in accounts:
        api_adapters["x"] = XPublisher()

    browser_adapters: dict[str, PublisherAdapter] = {}
    for platform, account in accounts.items():
        if account.mode.strip().casefold() not in {"browser", "auto"}:
            continue
        if account.cdp_url or (
            config.browser is not None and account.chrome_profile
        ):
            browser_adapters[platform] = _LazyBrowserAdapter(
                platform,
                account,
                config.browser,
                root,
                config_root,
            )

    builder = (
        _creative_pipeline(
            config.creative,
            config_root=config_root,
            output_root=layout["generated"],
        )
        if config.creative is not None
        else None
    )
    adapters = {**browser_adapters, **api_adapters}
    return Orchestrator(
        queue_store=queue_store,
        adapters=adapters,
        router=PublisherRouter(
            api_adapters=api_adapters,
            browser_adapters=browser_adapters,
        ),
        accounts=accounts,
        builder=builder,
        daily_cap=config.daily_cap,
        timezone_name=config.timezone,
        retry_limit=config.retry_limit,
        failure_pause_threshold=config.failure_pause_threshold,
    )
