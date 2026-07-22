from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import time
from pathlib import Path

from .control import driver_command, run_verification
from .store import Store, StoreError


def checkpoint(store: Store, process_id: str, token: str, state: str, **details: object) -> None:
    severity = "attention" if state == "blocked" else "critical" if state == "failed" else "info"
    store.update_process(process_id, token, state, details, severity)
    print(
        json.dumps({"type": "checkpoint", "process": process_id, "state": state, **details}),
        flush=True,
    )


def json_default(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()  # type: ignore[union-attr,no-any-return]
    if hasattr(value, "__dict__"):
        return vars(value)
    return str(value)


def run_cursor_sdk(store: Store, process_id: str, prompt: str, cwd: Path, model: str) -> None:
    api_key = os.getenv("CURSOR_API_KEY")
    if not api_key:
        raise StoreError("CURSOR_API_KEY is required when AOS_CURSOR_TRANSPORT=sdk")
    try:
        from cursor_sdk import Agent, AgentOptions, LocalAgentOptions, SandboxOptions
    except ImportError as exc:
        raise StoreError("install the Cursor transport with `uv sync --extra cursor`") from exc

    options = AgentOptions(
        api_key=api_key,
        model=model,
        local=LocalAgentOptions(
            cwd=cwd,
            auto_review=True,
            sandbox_options=SandboxOptions(enabled=True),
        ),
        agents={},
    )
    with Agent.create(options) as agent:
        store.set_process_session(process_id, str(agent.agent_id))
        run = agent.send(prompt)
        for event in run.stream():
            print(json.dumps(event, default=json_default), flush=True)
        result = run.wait()
        print(json.dumps(result, default=json_default), flush=True)
        result_usage = getattr(result, "usage", None)
        if result_usage:
            serialized_usage = json_default(result_usage)
            if isinstance(serialized_usage, dict):
                store.update_worker_session_metadata(process_id, {"usage": serialized_usage})
        status = str(result.status).lower()
        if not any(terminal in status for terminal in ("completed", "success", "finished")):
            raise StoreError(f"Cursor SDK run ended with status {result.status}")
        store.observe_provider(
            "cursor",
            "healthy",
            "cursor_python_sdk",
            "estimated",
            {"model": model},
        )


def fail_attempt_and_mission(store: Store, mission_id: str, attempt: int) -> None:
    store.update_attempt_state(mission_id, attempt, "failed")
    failed_attempts = sum(item["state"] == "failed" for item in store.attempts(mission_id))
    target = "non_convergent" if failed_attempts >= 3 else "failed"
    current = store.mission(mission_id)["state"]
    if current in {"dispatched", "implementing", "verifying"}:
        store.transition_mission(mission_id, target, {current})


def stream_cli_driver(
    store: Store,
    process: dict[str, object],
    process_id: str,
    token: str,
    command: list[str],
    prompt: str,
    cwd: Path,
) -> None:
    child = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=cwd,
        bufsize=1,
    )
    assert child.stdin is not None
    assert child.stdout is not None
    child.stdin.write(prompt)
    child.stdin.close()
    backend_handle_recorded = False
    last_heartbeat = time.monotonic()
    tail: list[str] = []
    usage: dict[str, object] = {}
    for line in child.stdout:
        print(line, end="", flush=True)
        tail.append(line[-1000:])
        tail = tail[-20:]
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            event = {}
        handle = event.get("session_id")
        if event.get("type") == "thread.started":
            handle = event.get("thread_id")
        if handle and not backend_handle_recorded:
            store.set_process_session(process_id, str(handle))
            backend_handle_recorded = True
        event_usage = event.get("usage")
        if isinstance(event_usage, dict):
            usage.update(
                {
                    key: value
                    for key, value in event_usage.items()
                    if isinstance(value, (int, float))
                }
            )
        if time.monotonic() - last_heartbeat >= 30:
            checkpoint(
                store,
                process_id,
                token,
                "running",
                phase="implementing",
                current=f"provider event: {event.get('type', 'output')}",
            )
            last_heartbeat = time.monotonic()
    return_code = child.wait()
    if usage:
        store.update_worker_session_metadata(process_id, {"usage": usage})
    if return_code != 0:
        error_tail = "\n".join(tail).lower()
        limited = "rate limit" in error_tail or "quota" in error_tail or "usage limit" in error_tail
        store.observe_provider(
            str(process["backend"]),
            "limited" if limited else "degraded",
            "runtime_error",
            "authoritative" if limited else "estimated",
            {"exit_code": return_code, "model": process["model"]},
        )
        raise StoreError(f"{process['backend']} worker exited with {return_code}")
    store.observe_provider(
        str(process["backend"]),
        "healthy",
        "successful_run",
        "estimated",
        {"exit_code": return_code, "model": process["model"]},
    )


def run(home: Path, process_id: str, token: str) -> int:
    store = Store(home)
    process = store.require_process(process_id, token)
    mission = store.mission(process["mission_id"])
    cwd = Path(process["worktree"])
    store.transition_mission(mission["id"], "implementing", {"dispatched"})
    checkpoint(store, process_id, token, "running", phase="started", current="preparing work order")
    work_order = store.work_order(process_id)["content"]
    prompt = "You are an aOS worker. Follow this versioned work order exactly:\n" + json.dumps(
        work_order, indent=2
    )
    try:
        use_cursor_sdk = (
            process["backend"] == "cursor"
            and os.getenv("AOS_CURSOR_TRANSPORT", "cli").lower() == "sdk"
        )
        command = (
            None if use_cursor_sdk else driver_command(process["backend"], process["model"], cwd)
        )
        if use_cursor_sdk:
            checkpoint(
                store,
                process_id,
                token,
                "running",
                phase="implementing",
                current="executing Cursor Python SDK worker",
            )
            run_cursor_sdk(store, process_id, prompt, cwd, process["model"])
        elif command:
            checkpoint(
                store,
                process_id,
                token,
                "running",
                phase="implementing",
                current=f"executing {process['backend']} worker",
            )
            stream_cli_driver(store, process, process_id, token, command, prompt, cwd)
        else:
            checkpoint(
                store,
                process_id,
                token,
                "running",
                phase="implementing",
                current="deterministic fake driver completed",
            )
            store.observe_provider(
                "fake", "healthy", "built_in", "authoritative", {"model": process["model"]}
            )
        store.transition_mission(mission["id"], "verifying", {"implementing"})
        checkpoint(
            store, process_id, token, "running", phase="verifying", current="running verifier"
        )
        passed, evidence = run_verification(mission["verify_command"], cwd)
        if not passed:
            checkpoint(store, process_id, token, "failed", phase="verifying", verification=evidence)
            fail_attempt_and_mission(store, mission["id"], process["attempt"])
            return 1
        checkpoint(store, process_id, token, "completed", phase="completed", verification=evidence)
        store.update_attempt_state(mission["id"], process["attempt"], "completed")
        store.transition_mission(mission["id"], "awaiting_approval", {"verifying"})
        store.event(
            "claim.completed",
            "mission",
            mission["id"],
            {"process_id": process_id, "verification": evidence},
            "approval",
        )
        return 0
    except Exception as exc:
        try:
            if process["backend"] != "fake":
                latest = store.latest_provider_status(process["backend"])
                if not latest or latest["source"] != "runtime_error":
                    store.observe_provider(
                        process["backend"],
                        "degraded",
                        "worker_exception",
                        "estimated",
                        {"model": process["model"]},
                    )
            checkpoint(store, process_id, token, "failed", phase="failed", blocker=str(exc))
            fail_attempt_and_mission(store, mission["id"], process["attempt"])
        except Exception:
            pass
        print(json.dumps({"type": "error", "message": str(exc)}), flush=True)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--process", required=True)
    args = parser.parse_args()
    token = os.environ.pop("AOS_PROCESS_TOKEN", None)
    if not token:
        raise SystemExit("AOS_PROCESS_TOKEN is required")
    return run(args.home, args.process, token)


if __name__ == "__main__":
    raise SystemExit(main())
