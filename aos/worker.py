from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
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
        status = str(result.status).lower()
        if not any(terminal in status for terminal in ("completed", "success", "finished")):
            raise StoreError(f"Cursor SDK run ended with status {result.status}")


def run(home: Path, process_id: str, token: str) -> int:
    store = Store(home)
    process = store.require_process(process_id, token)
    mission = store.mission(process["mission_id"])
    cwd = Path(process["worktree"])
    store.transition_mission(mission["id"], "implementing", {"dispatched"})
    checkpoint(store, process_id, token, "running", phase="started", current="preparing work order")
    work_order = {
        "schema_version": 1,
        "mission_id": mission["id"],
        "process_id": process_id,
        "objective": mission["objective"],
        "constraints": [
            "Work only in the assigned git worktree.",
            "Do not spawn or delegate to other agents.",
            "Do not expose credentials, local state, or unrelated source content.",
            "Do not commit or push; aOS retains delivery authority.",
        ],
        "verification_command": mission["verify_command"],
        "output_contract": "Implement the objective, verify it, and summarize files changed and remaining risks.",
    }
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
            child = subprocess.run(
                command,
                input=prompt,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                timeout=3600,
            )
            print(child.stdout, end="", flush=True)
            if child.returncode != 0:
                raise StoreError(f"{process['backend']} worker exited with {child.returncode}")
        else:
            checkpoint(
                store,
                process_id,
                token,
                "running",
                phase="implementing",
                current="deterministic fake driver completed",
            )
        store.transition_mission(mission["id"], "verifying", {"implementing"})
        checkpoint(
            store, process_id, token, "running", phase="verifying", current="running verifier"
        )
        passed, evidence = run_verification(mission["verify_command"], cwd)
        if not passed:
            checkpoint(store, process_id, token, "failed", phase="verifying", verification=evidence)
            store.transition_mission(mission["id"], "failed", {"verifying"})
            return 1
        checkpoint(store, process_id, token, "completed", phase="completed", verification=evidence)
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
            checkpoint(store, process_id, token, "failed", phase="failed", blocker=str(exc))
            current = store.mission(mission["id"])["state"]
            if current in {"dispatched", "implementing", "verifying"}:
                store.transition_mission(mission["id"], "failed", {current})
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
