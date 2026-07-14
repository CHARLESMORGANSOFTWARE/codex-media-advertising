from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from pydantic import ValidationError

from .automation.launchd import LaunchdBuilder, LaunchdManager, Schedule
from .config import redact
from .manifests import load_campaign
from .models import PublishRequest, PublishResult, PublishStatus
from .orchestrator import Orchestrator
from .publishing.base import ErrorCategory, redact_diagnostic
from .queueing import EnqueueResult
from .runtime import RuntimeConfigurationError, load_runtime
from .setup import SetupService, result_payload as setup_result_payload

__version__ = "0.1.0"


def _leaf(subparsers, name: str, help_text: str) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(name, help=help_text)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-media-ads")
    parser.add_argument("--version", action="store_true")
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path(
            os.environ.get("CODEX_MEDIA_ADS_STATE_ROOT", "~/.codex-media-ads")
        ).expanduser(),
    )
    commands = parser.add_subparsers(dest="command")

    setup = commands.add_parser("setup", help="Check dependencies and configure channels")
    setup.add_argument("--enable", action="append", default=[], metavar="PLATFORM")
    setup.add_argument("--config", type=Path, help="Nonsecret channel configuration JSON")
    setup.add_argument(
        "--import-secret",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Copy a credential file into private state without printing it",
    )
    setup.add_argument("--format", choices=("json", "text"), default="json")

    automation = commands.add_parser("automation", help="Manage background jobs")
    automation_commands = automation.add_subparsers(
        dest="automation_command", required=True
    )
    install = _leaf(automation_commands, "install", "Install a user LaunchAgent")
    install.add_argument("name", choices=("daily-short",))
    schedule_group = install.add_mutually_exclusive_group()
    schedule_group.add_argument("--interval", type=int)
    schedule_group.add_argument("--hour", type=int, default=9)
    install.add_argument("--minute", type=int, default=0)
    _leaf(automation_commands, "list", "List plugin-owned user LaunchAgents")
    remove = _leaf(automation_commands, "remove", "Remove a plugin-owned user LaunchAgent")
    remove.add_argument("name", choices=("daily-short",))

    campaign = commands.add_parser("campaign", help="Validate or build campaigns")
    campaign_commands = campaign.add_subparsers(dest="campaign_command", required=True)
    validate = _leaf(campaign_commands, "validate", "Validate a campaign manifest")
    validate.add_argument("campaign", type=Path)
    build = _leaf(campaign_commands, "build", "Build and optionally publish a campaign")
    build.add_argument("campaign", type=Path)
    build.add_argument("--dry-run", action="store_true")

    queue = commands.add_parser("queue", help="Manage queued publish requests")
    queue_commands = queue.add_subparsers(dest="queue_command", required=True)
    add = _leaf(queue_commands, "add", "Add a publish request to the queue")
    add.add_argument("request", type=Path)
    _leaf(queue_commands, "status", "Show queue status")

    publish = commands.add_parser("publish", help="Process or probe publishing")
    publish_commands = publish.add_subparsers(dest="publish_command", required=True)
    next_parser = _leaf(publish_commands, "next", "Process the next queued request")
    next_parser.add_argument("--dry-run", action="store_true")
    next_parser.add_argument("--schedule", help="Background schedule provenance marker")
    probe = _leaf(publish_commands, "probe", "Probe a configured platform identity")
    probe.add_argument("--platform", required=True)

    platform = commands.add_parser("platform", help="Pause or resume a platform account")
    platform_commands = platform.add_subparsers(dest="platform_command", required=True)
    for action in ("pause", "resume"):
        action_parser = _leaf(
            platform_commands, action, f"{action.title()} a platform account"
        )
        action_parser.add_argument("--platform", required=True)
        action_parser.add_argument("--account", default="")

    receipts = commands.add_parser("receipts", help="Inspect private publish receipts")
    receipt_commands = receipts.add_subparsers(dest="receipts_command", required=True)
    _leaf(receipt_commands, "show", "Show redacted receipt records")
    return parser


def _safe(value: object, key: str = "") -> object:
    value = redact(value, key)
    if isinstance(value, dict):
        return {
            str(item_key): _safe(item_value, str(item_key))
            for item_key, item_value in value.items()
            if str(item_key) != "account_id"
        }
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        return redact_diagnostic(value)
    if isinstance(value, Path):
        return str(value)
    return value


def _emit(payload: dict[str, object], output_format: str) -> None:
    safe = _safe(payload)
    if output_format == "json":
        print(json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str))
        return
    assert isinstance(safe, dict)
    for key, value in safe.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        print(f"{key}: {value}")


def _runtime(
    state_root: Path,
    supplied: Orchestrator | None,
    *,
    require_configuration: bool = False,
) -> Orchestrator:
    if supplied is not None:
        return supplied
    return load_runtime(
        state_root,
        require_configuration=require_configuration,
    )


def _launchd_manager() -> LaunchdManager:
    configured = os.environ.get("CODEX_MEDIA_ADS_EXECUTABLE")
    discovered = shutil.which("codex-media-ads")
    executable = Path(configured or discovered or (Path(sys.executable).parent / "codex-media-ads"))
    plugin_root = Path(__file__).resolve().parents[2]
    return LaunchdManager(
        LaunchdBuilder(executable=executable, working_directory=plugin_root)
    )


def _configured_tool(value: object) -> Path | None:
    text = os.fspath(value) if isinstance(value, (str, Path)) else ""
    if not text:
        return None
    path = Path(text).expanduser()
    if path.is_absolute():
        return path
    discovered = shutil.which(text)
    return Path(discovered) if discovered else None


def _setup_dry_run(service: Orchestrator, platform: str, state_root: Path) -> PublishResult:
    account = service.accounts[platform]
    adapter = service.router.select(account, platform)
    request = PublishRequest(
        content_id="setup-proof",
        revision=1,
        platform=platform,
        account=account,
        media_path=state_root / "health" / "setup-render.mp4",
        metadata={
            "caption": "Codex Media Ads setup proof",
            "description": "Codex Media Ads setup proof",
            "title": "Codex Media Ads setup proof",
        },
        idempotency_key=f"setup-proof-{platform}",
        dry_run=True,
    )
    return adapter.publish(request)


def _configured_setup_service(
    state_root: Path,
) -> tuple[SetupService, dict[str, dict[str, object]]]:
    state_root = Path(state_root).expanduser().absolute()
    runtime = load_runtime(state_root, require_configuration=True)
    defaults = {
        platform: {
            "expected_identity": account.expected_identity,
            "mode": account.mode,
        }
        for platform, account in runtime.accounts.items()
    }
    probes = {
        platform: (lambda name=platform: runtime.probe(name))
        for platform in runtime.accounts
    }
    dry_runs = {
        platform: (
            lambda name=platform: _setup_dry_run(runtime, name, state_root)
        )
        for platform in runtime.accounts
    }
    tool_paths: dict[str, Path | None] = {}
    browser_adapters = getattr(runtime.router, "browser_adapters", {})
    for lazy_adapter in browser_adapters.values():
        browser = getattr(lazy_adapter, "browser", None)
        chrome_path = getattr(browser, "chrome_path", None)
        if chrome_path is None:
            continue
        configured_chrome = Path(chrome_path).expanduser()
        if not configured_chrome.is_absolute():
            configured_chrome = Path(lazy_adapter.config_root) / configured_chrome
        tool_paths["chrome"] = configured_chrome.absolute()
        break
    narration_probe = None
    builder = runtime.builder
    if builder is not None:
        tool_paths["ffmpeg"] = _configured_tool(builder.ffmpeg)
        tool_paths["ffprobe"] = _configured_tool(builder.ffprobe)
        image_command = getattr(builder.image_provider, "command_identity", [])
        tool_paths["codimage"] = _configured_tool(
            image_command[0] if image_command else None
        )
        narration_command = getattr(
            builder.narration_provider, "command_identity", []
        )
        tool_paths["narration"] = _configured_tool(
            narration_command[0] if narration_command else None
        )

        def probe_narration() -> bool:
            output = state_root / "health" / "setup-narration.wav"
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            output.unlink(missing_ok=True)
            result = builder.narration_provider.synthesize(
                "Codex Media Ads setup proof.", output, builder.voice
            )
            ready = Path(result).is_file() and Path(result).stat().st_size > 0
            if ready:
                Path(result).chmod(0o600)
            return ready

        narration_probe = probe_narration
    return (
        SetupService(
            state_root,
            tool_paths=tool_paths,
            probes=probes,
            dry_runs=dry_runs,
            narration_probe=narration_probe,
        ),
        defaults,
    )


def _channel_config(path: Path | None) -> dict[str, dict[str, object]]:
    if path is None:
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("setup configuration must be a JSON object")
    unknown = set(value) - {"channels"}
    if unknown:
        raise ValueError(
            "unknown setup configuration keys: " + ", ".join(sorted(unknown))
        )
    channels = value.get("channels")
    if not isinstance(channels, dict) or any(
        not isinstance(item, dict) for item in channels.values()
    ):
        raise ValueError("setup channels must be JSON objects")
    return {str(name): dict(item) for name, item in channels.items()}


def _background_ready(state_root: Path) -> bool:
    path = state_root / "config" / "setup.json"
    if path.is_symlink() or not path.is_file():
        return False
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    channels = value.get("channels") if isinstance(value, dict) else None
    return bool(channels) and isinstance(channels, dict) and all(
        isinstance(channel, dict) and channel.get("background_enabled") is True
        for channel in channels.values()
    )


def _result_payload(result: PublishResult) -> dict[str, object]:
    failed = result.status in {PublishStatus.FAILED, PublishStatus.UNKNOWN}
    ok = result.status != PublishStatus.BLOCKED and not failed
    payload: dict[str, object] = {
        "ok": ok,
        "status": PublishStatus.FAILED.value if failed else result.status.value,
    }
    if result.error_category or result.status == PublishStatus.UNKNOWN:
        payload["error_category"] = (
            ErrorCategory.AMBIGUOUS_SUBMIT.value
            if result.status == PublishStatus.UNKNOWN
            else result.error_category
        )
    if result.detail:
        payload["detail"] = result.detail
    next_action = result.evidence.get("next_action")
    if isinstance(next_action, str) and next_action:
        payload["next_action"] = next_action
    receipt_file = result.evidence.get("receipt_file")
    if isinstance(receipt_file, str) and receipt_file:
        payload["receipt_file"] = receipt_file
    public_evidence = {
        key: value
        for key, value in result.evidence.items()
        if key not in {"next_action", "receipt_file"}
    }
    if public_evidence:
        payload["evidence"] = public_evidence
    if result.platform_id:
        payload["platform_id"] = result.platform_id
    if result.post_url:
        payload["post_url"] = result.post_url
    return payload


def _result_exit(result: PublishResult) -> int:
    if result.status == PublishStatus.BLOCKED:
        return 3
    if result.status in {PublishStatus.FAILED, PublishStatus.UNKNOWN}:
        return 4
    return 0


def _failure(
    *,
    status: str,
    category: str,
    detail: str,
    next_action: str,
) -> dict[str, object]:
    return {
        "ok": False,
        "status": status,
        "error_category": category,
        "detail": detail,
        "next_action": next_action,
    }


def _account_id(service: Orchestrator, platform: str, supplied: str) -> str:
    if supplied:
        return supplied
    account = service.accounts.get(platform)
    if account is None:
        raise ValueError(f"{platform.title()} account is not configured.")
    return account.account_id


def main(
    argv: list[str] | None = None,
    *,
    orchestrator: Orchestrator | None = None,
    setup_service: SetupService | None = None,
    launchd_manager: LaunchdManager | None = None,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.version:
        _emit(
            {"name": "codex-media-advertising", "version": __version__},
            "json",
        )
        return 0
    if args.command is None:
        parser.print_help()
        return 0

    output_format = getattr(args, "format", "json")
    try:
        if args.command == "setup":
            defaults: dict[str, dict[str, object]] = {}
            supplied_setup_service = setup_service is not None
            setup_service = setup_service or SetupService(args.state_root)
            imported: list[str] = []
            for specification in args.import_secret:
                if "=" not in specification:
                    raise ValueError("secret import must use NAME=PATH")
                name, source = specification.split("=", 1)
                destination = setup_service.import_secret(Path(source), name)
                imported.append(str(destination))
            if args.enable and not supplied_setup_service:
                setup_service, defaults = _configured_setup_service(args.state_root)
            supplied_channels = _channel_config(args.config)
            channels = {
                name: {**defaults.get(name, {}), **supplied_channels.get(name, {})}
                for name in set(defaults) | set(supplied_channels)
            }
            result = setup_service.configure(
                enabled=args.enable,
                channels=channels,
            )
            payload = setup_result_payload(result)
            if imported:
                payload["imported_secret_files"] = imported
            _emit(payload, output_format)
            return 0 if payload["ok"] else 3

        if args.command == "automation":
            launchd_manager = launchd_manager or _launchd_manager()
            if args.automation_command == "install":
                if not _background_ready(args.state_root):
                    _emit(
                        _failure(
                            status="blocked",
                            category="configuration",
                            detail="Background automation has not passed the setup gates.",
                            next_action=(
                                "Run codex-media-ads setup and resolve every blocked "
                                "check before installing automation."
                            ),
                        ),
                        output_format,
                    )
                    return 3
                schedule = (
                    Schedule(interval=args.interval)
                    if args.interval is not None
                    else Schedule(calendar={"Hour": args.hour, "Minute": args.minute})
                )
                path = launchd_manager.install(
                    args.name, state_root=args.state_root, schedule=schedule
                )
                _emit(
                    {
                        "ok": True,
                        "status": "installed",
                        "automation": args.name,
                        "plist": str(path),
                    },
                    output_format,
                )
                return 0
            if args.automation_command == "list":
                _emit(
                    {
                        "ok": True,
                        "status": "ok",
                        "automations": launchd_manager.list(),
                    },
                    output_format,
                )
                return 0
            if args.automation_command == "remove":
                removed = launchd_manager.remove(
                    args.name, state_root=args.state_root
                )
                _emit(
                    {
                        "ok": True,
                        "status": "removed" if removed else "not-installed",
                        "automation": args.name,
                        "state_preserved": True,
                    },
                    output_format,
                )
                return 0

        if args.command == "campaign" and args.campaign_command == "validate":
            campaign = load_campaign(args.campaign)
            _emit(
                {
                    "ok": True,
                    "status": "valid",
                    "campaign_id": campaign.campaign_id,
                    "content_id": campaign.content_id,
                    "destinations": campaign.destinations,
                },
                output_format,
            )
            return 0

        requires_runtime = args.command == "publish" or (
            args.command == "campaign" and args.campaign_command == "build"
        )
        service = _runtime(
            args.state_root,
            orchestrator,
            require_configuration=requires_runtime,
        )
        if args.command == "campaign" and args.campaign_command == "build":
            if service.builder is None:
                runtime_path = args.state_root / "config" / "runtime.json"
                raise RuntimeConfigurationError(
                    f"Creative runtime configuration is missing in {runtime_path}.",
                    "Add the creative image and narration provider configuration before building a campaign.",
                )
            campaign = load_campaign(args.campaign)
            result = service.run_campaign(campaign, live=not args.dry_run)
            statuses = {item.status for item in result.platforms.values()}
            has_failed = bool(
                statuses.intersection(
                    {PublishStatus.FAILED, PublishStatus.UNKNOWN}
                )
            )
            has_blocked = PublishStatus.BLOCKED in statuses
            payload = {
                "ok": all(
                    item.status
                    not in {
                        PublishStatus.BLOCKED,
                        PublishStatus.FAILED,
                        PublishStatus.UNKNOWN,
                    }
                    for item in result.platforms.values()
                ),
                "status": (
                    "failed" if has_failed else "blocked" if has_blocked else "built"
                ),
                "live_success_count": result.live_success_count,
                "build_manifest": result.build_manifest,
                "platforms": {
                    platform: _result_payload(item)
                    for platform, item in result.platforms.items()
                },
            }
            _emit(payload, output_format)
            if has_failed:
                return 4
            if has_blocked:
                return 3
            return 0

        if args.command == "queue" and args.queue_command == "add":
            request = PublishRequest.model_validate_json(args.request.read_text())
            result = service.add_to_queue(request)
            if isinstance(result, PublishResult):
                payload = _result_payload(result)
                exit_code = _result_exit(result)
            else:
                assert isinstance(result, EnqueueResult)
                payload = {
                    "ok": True,
                    "status": result.status,
                    "queue_file": str(result.path) if result.path else "",
                }
                exit_code = 0
            _emit(payload, output_format)
            return exit_code

        if args.command == "queue" and args.queue_command == "status":
            _emit({"ok": True, **service.queue_status()}, output_format)
            return 0

        if args.command == "publish" and args.publish_command == "next":
            result = service.process_next(live=not args.dry_run)
            _emit(_result_payload(result), output_format)
            return _result_exit(result)

        if args.command == "publish" and args.publish_command == "probe":
            probe = service.probe(args.platform)
            if probe.authenticated:
                payload = {"ok": True, "status": "ready", "platform": args.platform}
                exit_code = 0
            else:
                payload = _failure(
                    status="blocked",
                    category=(
                        probe.error_category.value
                        if probe.error_category is not None
                        else "authentication"
                    ),
                    detail=probe.detail or f"{args.platform.title()} account probe failed.",
                    next_action=probe.next_action
                    or f"Run codex-media-ads publish probe --platform {args.platform}.",
                )
                exit_code = 3
            _emit(payload, output_format)
            return exit_code

        if args.command == "platform":
            account_id = _account_id(service, args.platform, args.account)
            action = args.platform_command
            getattr(service.pause_store, action)(args.platform, account_id)
            _emit(
                {"ok": True, "status": f"{action}d", "platform": args.platform},
                output_format,
            )
            return 0

        if args.command == "receipts" and args.receipts_command == "show":
            _emit({"ok": True, "status": "ok", "receipts": service.receipts()}, output_format)
            return 0

        raise ValueError("unsupported command")
    except RuntimeConfigurationError as exc:
        _emit(
            _failure(
                status="invalid",
                category="configuration",
                detail=exc.detail,
                next_action=exc.next_action,
            ),
            output_format,
        )
        return 2
    except (ValidationError, json.JSONDecodeError, OSError, ValueError) as exc:
        if isinstance(exc, OSError):
            detail = "The requested file could not be read."
        elif isinstance(exc, ValueError) and not isinstance(exc, ValidationError):
            detail = redact_diagnostic(str(exc))
        else:
            detail = "The input failed validation."
        _emit(
            _failure(
                status="invalid",
                category="configuration" if isinstance(exc, OSError) else "validation",
                detail=detail,
                next_action="Correct the input or configuration and retry.",
            ),
            output_format,
        )
        return 2
    except Exception:
        _emit(
            _failure(
                status="failed",
                category="internal",
                detail="The command failed without exposing private diagnostics.",
                next_action="Review the private plugin logs and retry safely.",
            ),
            output_format,
        )
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
