from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .control import (
    approve_mission,
    authorize_mission,
    create_mission,
    dispatch_mission,
    process_expired_workers,
)
from .store import AuthorizationError, Store, StoreError, TransitionError


def scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    text = str(value)
    if any(char in text for char in "\n:#[]{}") or text != text.strip():
        return json.dumps(text)
    return text


def render(data: dict[str, Any], help_lines: list[str] | None = None) -> None:
    for key, value in data.items():
        if isinstance(value, list):
            print(f"{key}[{len(value)}]:")
            for index, item in enumerate(value):
                print(f"  - index: {index}")
                if isinstance(item, dict):
                    for child_key, child_value in item.items():
                        print(f"    {child_key}: {scalar(child_value)}")
                else:
                    print(f"    value: {scalar(item)}")
        else:
            print(f"{key}: {scalar(value)}")
    lines = help_lines or []
    print(f"help[{len(lines)}]:")
    for line in lines:
        print(f"  - {line}")


def token_arg(args: argparse.Namespace) -> str | None:
    return getattr(args, "token", None) or os.getenv("AOS_LIAISON_TOKEN")


def daemon_status(store: Store) -> str:
    marker = store.home / "aosd.json"
    if not marker.exists():
        return "offline"
    try:
        return f"online pid={json.loads(marker.read_text())['pid']}"
    except (OSError, ValueError, KeyError):
        return "unknown"


def dashboard(store: Store) -> None:
    if not store.db_path.exists():
        render(
            {
                "aos": __version__,
                "daemon": "offline",
                "missions_count": 0,
                "state": "not_initialized",
            },
            ["Run `aos init` to initialize local state."],
        )
        return
    missions = store.missions()
    processes = store.processes()
    with store.connect() as db:
        lease = db.execute("SELECT holder, expires_at FROM leases WHERE kind='liaison'").fetchone()
    render(
        {
            "aos": __version__,
            "daemon": daemon_status(store),
            "liaison": dict(lease) if lease else "unclaimed",
            "missions_count": len(missions),
            "active_count": sum(
                m["state"] not in {"delivered", "failed", "cancelled"} for m in missions
            ),
            "processes_count": len(processes),
            "missions": [
                {"id": m["id"], "state": m["state"], "project": m["project"]} for m in missions[:20]
            ],
        },
        [
            "Use `aos watch` for the live flight board.",
            "Use `aos briefing` for full active context.",
        ],
    )


def watch_once(store: Store) -> str:
    if not store.db_path.exists():
        return "aOS daemon=offline missions=0 state=not_initialized\nNo missions. Run `aos init`."
    missions = store.missions()
    processes = {p["mission_id"]: p for p in store.processes()}
    active = [m for m in missions if m["state"] not in {"delivered", "cancelled"}]
    blocked = sum(m["state"] in {"failed", "non_convergent"} for m in active)
    approvals = sum(m["state"] == "awaiting_approval" for m in active)
    lines = [
        f"aOS daemon={daemon_status(store)} active={len(active)} blocked={blocked} awaiting_approval={approvals}"
    ]
    if not active:
        lines.append("No active missions.")
    for mission in active:
        process = processes.get(mission["id"])
        if process:
            cp = process["checkpoint"]
            detail = cp.get("current") or cp.get("blocker") or cp.get("phase") or "starting"
            lines.append(
                f"{mission['id']} {mission['state']} {process['backend']}/{process['model']} "
                f"process={process['id']} detail={json.dumps(detail)}"
            )
        else:
            lines.append(f"{mission['id']} {mission['state']} process=none")
    return "\n".join(lines)


def run_watch(store: Store, once: bool, interval: float) -> None:
    previous = None
    while True:
        try:
            current = watch_once(store)
        except (sqlite3.Error, StoreError) as exc:
            current = f"aOS daemon=offline state=unavailable\n{exc}"
        if current != previous:
            if not once and sys.stdout.isatty():
                print("\033[2J\033[H", end="")
            print(current, flush=True)
            previous = current
        if once:
            return
        time.sleep(interval)


def doctor(store: Store) -> tuple[dict[str, Any], bool]:
    root = Path.cwd()
    checks = [
        {
            "name": "python",
            "status": "ok" if sys.version_info >= (3, 11) else "fail",
            "detail": sys.version.split()[0],
        },
        {
            "name": "git",
            "status": "ok" if shutil.which("git") else "fail",
            "detail": shutil.which("git"),
        },
        {
            "name": "aos_state",
            "status": "ok" if store.db_path.exists() else "warn",
            "detail": str(store.db_path),
        },
        {
            "name": "AGENTS.md",
            "status": "ok" if (root / "AGENTS.md").exists() else "warn",
            "detail": "liaison rules",
        },
        {
            "name": "cursor_rule",
            "status": "ok" if (root / ".cursor/rules/aos-liaison.mdc").exists() else "warn",
            "detail": "cooperative boundary",
        },
        {
            "name": "codex_rule",
            "status": "ok" if (root / ".codex/config.toml").exists() else "warn",
            "detail": "cooperative boundary",
        },
        {
            "name": "cursor_agent",
            "status": "ok" if shutil.which("cursor-agent") else "warn",
            "detail": "optional provider",
        },
        {
            "name": "codex",
            "status": "ok" if shutil.which("codex") else "warn",
            "detail": "optional provider",
        },
        {
            "name": "CURSOR_API_KEY",
            "status": "ok" if os.getenv("CURSOR_API_KEY") else "warn",
            "detail": "set"
            if os.getenv("CURSOR_API_KEY")
            else "not set; Cursor CLI login may still work",
        },
    ]
    healthy = all(item["status"] != "fail" for item in checks)
    return {"status": "ok" if healthy else "failed", "checks": checks}, healthy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aos", description="Durable control plane for coding agents"
    )
    parser.add_argument("--home", type=Path, default=Path(os.getenv("AOS_HOME", "state")))
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="initialize the durable state store")
    sub.add_parser("doctor", help="check local setup without printing secret values")
    watch = sub.add_parser("watch", help="render the read-only mission flight board")
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--interval", type=float, default=1.0)
    sub.add_parser("briefing", help="reconstruct active work and attention from durable state")
    events = sub.add_parser("events", help="show journal events")
    events.add_argument("--since", type=int, default=0)

    liaison = sub.add_parser("liaison", help="manage the singleton liaison lease")
    liaison_sub = liaison.add_subparsers(dest="liaison_command", required=True)
    claim = liaison_sub.add_parser("claim")
    claim.add_argument("--harness", required=True)
    claim.add_argument("--ttl", type=int, default=600)
    release = liaison_sub.add_parser("release")
    release.add_argument("--token")
    sync = liaison_sub.add_parser("sync")
    sync.add_argument("--token")
    sync.add_argument("--since", type=int, default=0)

    mission = sub.add_parser("mission", help="create and govern mission contracts")
    mission_sub = mission.add_subparsers(dest="mission_command", required=True)
    create = mission_sub.add_parser("create")
    create.add_argument("--token")
    create.add_argument("--project", required=True)
    create.add_argument("--objective", required=True)
    create.add_argument("--backend", action="append", choices=["fake", "cursor", "codex"])
    create.add_argument("--model", action="append", default=[])
    create.add_argument("--verify")
    create.add_argument("--allow-anthropic", action="store_true")
    authorize = mission_sub.add_parser("authorize")
    authorize.add_argument("mission_id")
    authorize.add_argument("--token")
    view = mission_sub.add_parser("view")
    view.add_argument("mission_id")

    dispatch = sub.add_parser("dispatch", help="route and spawn an authorized mission")
    dispatch.add_argument("mission_id")
    dispatch.add_argument("--token")
    dispatch.add_argument("--backend", choices=["fake", "cursor", "codex"])
    dispatch.add_argument("--model")
    approve = sub.add_parser("approve", help="approve verified work for delivery")
    approve.add_argument("mission_id")
    approve.add_argument("--token")
    sub.add_parser("recover", help="mark expired running processes orphaned")
    return parser


def execute(args: argparse.Namespace, store: Store) -> int:
    if not args.command:
        dashboard(store)
        return 0
    if args.command == "init":
        store.initialize()
        render(
            {"status": "initialized", "home": str(store.home), "database": str(store.db_path)},
            ["Run `aos liaison claim --harness <name>`."],
        )
    elif args.command == "doctor":
        result, healthy = doctor(store)
        render(
            result, ["Warnings identify optional providers or guardrails; failures must be fixed."]
        )
        return 0 if healthy else 1
    elif args.command == "watch":
        run_watch(store, args.once, args.interval)
    elif args.command == "events":
        events = store.events_since(args.since)
        render(
            {"count": len(events), "events": events},
            ["Use `--since <last-id>` for incremental reads."],
        )
    elif args.command == "briefing":
        missions = [m for m in store.missions() if m["state"] not in {"delivered", "cancelled"}]
        render(
            {"missions_count": len(missions), "missions": missions, "processes": store.processes()},
            ["Use `aos mission view <id>` to inspect one mission."],
        )
    elif args.command == "liaison":
        if args.liaison_command == "claim":
            token, expiry = store.claim_liaison(args.harness, args.ttl)
            render(
                {
                    "status": "claimed",
                    "holder": args.harness,
                    "expires_at": expiry,
                    "liaison_token": token,
                },
                [
                    "Export this value as AOS_LIAISON_TOKEN; it is shown once and never stored in plaintext.",
                    "Start `aos watch` in a dedicated terminal.",
                ],
            )
        elif args.liaison_command == "release":
            store.release_liaison(token_arg(args))
            render({"status": "released"}, ["Another harness may now claim the liaison lease."])
        else:
            store.require_liaison(token_arg(args))
            events = store.events_since(args.since, significant_only=True)
            render(
                {
                    "count": len(events),
                    "cursor": events[-1]["id"] if events else args.since,
                    "events": events,
                },
                ["Persist the returned cursor in liaison context and pass it to the next sync."],
            )
    elif args.command == "mission":
        if args.mission_command == "create":
            mission_id = create_mission(
                store,
                token_arg(args),
                args.project,
                args.objective,
                args.backend or ["fake"],
                args.model,
                args.verify,
                args.allow_anthropic,
            )
            render(
                {"status": "draft", "mission_id": mission_id},
                [f"Review, then run `aos mission authorize {mission_id}`."],
            )
        elif args.mission_command == "authorize":
            authorize_mission(store, token_arg(args), args.mission_id)
            render(
                {"status": "authorized", "mission_id": args.mission_id},
                [f"Run `aos dispatch {args.mission_id}`."],
            )
        else:
            mission = store.mission(args.mission_id)
            render(
                {"mission": mission, "processes": store.processes(args.mission_id)},
                ["Inspect artifact paths locally; they may contain sensitive source context."],
            )
    elif args.command == "dispatch":
        result = dispatch_mission(store, token_arg(args), args.mission_id, args.backend, args.model)
        render(
            result,
            [
                "Use `aos watch` to follow progress.",
                f"Use `aos mission view {args.mission_id}` for evidence.",
            ],
        )
    elif args.command == "approve":
        approve_mission(store, token_arg(args), args.mission_id)
        render(
            {"status": "delivered", "mission_id": args.mission_id},
            ["Review and merge the worktree branch using your normal Git workflow."],
        )
    elif args.command == "recover":
        count = process_expired_workers(store)
        render(
            {"orphaned_count": count},
            ["Inspect orphaned work with `aos briefing` before retrying."],
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = Store(args.home)
    try:
        return execute(args, store)
    except (AuthorizationError, TransitionError, StoreError, sqlite3.Error) as exc:
        render(
            {"error": type(exc).__name__, "message": str(exc)},
            ["Run `aos doctor` and check command authorization/state."],
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
