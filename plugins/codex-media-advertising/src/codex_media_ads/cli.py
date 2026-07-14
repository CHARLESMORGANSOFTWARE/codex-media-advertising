from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .config import redact
from .manifests import load_campaign
from .models import PublishRequest, PublishResult, PublishStatus
from .orchestrator import Orchestrator
from .publishing.base import redact_diagnostic
from .queueing import EnqueueResult, QueueStore

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


def _runtime(state_root: Path, supplied: Orchestrator | None) -> Orchestrator:
    if supplied is not None:
        return supplied
    return Orchestrator(queue_store=QueueStore(state_root))


def _result_payload(result: PublishResult) -> dict[str, object]:
    ok = result.status not in {PublishStatus.BLOCKED, PublishStatus.FAILED}
    payload: dict[str, object] = {
        "ok": ok,
        "status": result.status.value,
    }
    if result.error_category:
        payload["error_category"] = result.error_category
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
    if result.status == PublishStatus.FAILED:
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
    argv: list[str] | None = None, *, orchestrator: Orchestrator | None = None
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

        service = _runtime(args.state_root, orchestrator)
        if args.command == "campaign" and args.campaign_command == "build":
            campaign = load_campaign(args.campaign)
            result = service.run_campaign(campaign, live=not args.dry_run)
            payload = {
                "ok": all(
                    item.status not in {PublishStatus.BLOCKED, PublishStatus.FAILED}
                    for item in result.platforms.values()
                ),
                "status": "built",
                "live_success_count": result.live_success_count,
                "build_manifest": result.build_manifest,
                "platforms": {
                    platform: _result_payload(item)
                    for platform, item in result.platforms.items()
                },
            }
            _emit(payload, output_format)
            statuses = {item.status for item in result.platforms.values()}
            if PublishStatus.FAILED in statuses:
                return 4
            if PublishStatus.BLOCKED in statuses:
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
