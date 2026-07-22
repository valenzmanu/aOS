from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .store import AuthorizationError, Store, StoreError, TransitionError, utcnow

MODEL_DEFAULTS = {
    "fake": "fake-deterministic",
    "cursor": "composer-2.5",
    "codex": "gpt-5.6-sol",
}


def create_mission(
    store: Store,
    token: str | None,
    project: str,
    objective: str,
    backends: list[str],
    models: list[str],
    verify_command: str | None,
    allow_anthropic: bool = False,
) -> str:
    store.require_liaison(token)
    if not objective.strip():
        raise StoreError("mission objective is required")
    project_path = Path(project).expanduser().resolve()
    if not project_path.exists():
        raise StoreError(f"project does not exist: {project_path}")
    unknown = set(backends) - set(MODEL_DEFAULTS)
    if unknown:
        raise StoreError(f"unknown backends: {', '.join(sorted(unknown))}")
    mission_id = "M-" + os.urandom(4).hex().upper()
    now = utcnow()
    with store.connect() as db:
        db.execute(
            """INSERT INTO missions
               (id, project, objective, state, allowed_backends, allowed_models,
                allow_anthropic, verify_command, created_at, updated_at)
               VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?)""",
            (
                mission_id,
                str(project_path),
                objective,
                json.dumps(backends),
                json.dumps(models),
                int(allow_anthropic),
                verify_command,
                now,
                now,
            ),
        )
        store._emit(
            db,
            "mission.created",
            "mission",
            mission_id,
            {
                "project": str(project_path),
                "allowed_backends": backends,
                "allowed_models": models,
                "allow_anthropic": allow_anthropic,
            },
        )
    return mission_id


def authorize_mission(store: Store, token: str | None, mission_id: str) -> None:
    store.require_liaison(token)
    store.transition_mission(mission_id, "authorized", {"draft"})


def approve_mission(store: Store, token: str | None, mission_id: str) -> None:
    store.require_liaison(token)
    store.transition_mission(mission_id, "delivered", {"awaiting_approval"})


def select_route(
    mission: dict[str, Any], requested_backend: str | None, requested_model: str | None
) -> tuple[str, str, str]:
    allowed_backends = mission["allowed_backends"]
    backend = requested_backend or allowed_backends[0]
    if backend not in allowed_backends:
        raise AuthorizationError(f"backend {backend!r} is not authorized by the mission contract")
    model = requested_model or MODEL_DEFAULTS[backend]
    allowed_models = mission["allowed_models"]
    if allowed_models and model not in allowed_models:
        raise AuthorizationError(f"model {model!r} is not authorized by the mission contract")
    anthropic = "claude" in model.lower() or "anthropic" in model.lower()
    if anthropic and not mission["allow_anthropic"]:
        raise AuthorizationError("Anthropic models require explicit mission authorization")
    reason = (
        f"explicit backend={backend}"
        if requested_backend
        else f"first authorized backend={backend}"
    )
    reason += f"; {'explicit' if requested_model else 'policy default'} model={model}"
    return backend, model, reason


def make_worktree(project: str, destination: Path, branch: str) -> Path:
    project_path = Path(project)
    check = subprocess.run(
        ["git", "-C", str(project_path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        raise StoreError(f"project is not a git repository: {project_path}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    add = subprocess.run(
        ["git", "-C", str(project_path), "worktree", "add", "-b", branch, str(destination), "HEAD"],
        capture_output=True,
        text=True,
    )
    if add.returncode != 0:
        raise StoreError(f"could not create worktree: {add.stderr.strip()}")
    return destination


def dispatch_mission(
    store: Store,
    token: str | None,
    mission_id: str,
    backend: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    store.require_liaison(token)
    mission = store.mission(mission_id)
    if mission["state"] != "authorized":
        raise TransitionError(
            f"mission must be authorized before dispatch (state={mission['state']})"
        )
    selected_backend, selected_model, reason = select_route(mission, backend, model)
    attempt = len(store.processes(mission_id)) + 1
    destination = store.home.parent / "projects" / f"{mission_id.lower()}-a{attempt}"
    branch = f"aos/{mission_id.lower()}/attempt-{attempt}"
    make_worktree(mission["project"], destination, branch)
    process_id, process_token = store.create_process(
        mission_id, selected_backend, selected_model, str(destination), reason
    )
    store.transition_mission(mission_id, "dispatched", {"authorized"})

    artifact = store.artifacts / f"{process_id}.jsonl"
    artifact.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stream = artifact.open("ab", buffering=0)
    env = os.environ.copy()
    env["AOS_PROCESS_TOKEN"] = process_token
    command = [
        sys.executable,
        "-m",
        "aos.worker",
        "--home",
        str(store.home),
        "--process",
        process_id,
    ]
    proc = subprocess.Popen(
        command,
        cwd=destination,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=stream,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    stream.close()
    store.set_process_pid(process_id, proc.pid)
    store.event(
        "process.spawned",
        "process",
        process_id,
        {"pid": proc.pid, "artifact": str(artifact), "routing_reason": reason},
    )
    return {
        "mission_id": mission_id,
        "process_id": process_id,
        "pid": proc.pid,
        "backend": selected_backend,
        "model": selected_model,
        "worktree": str(destination),
        "artifact": str(artifact),
        "routing_reason": reason,
    }


def run_verification(command: str | None, cwd: Path) -> tuple[bool, dict[str, Any]]:
    if not command:
        return True, {
            "command": None,
            "exit_code": 0,
            "summary": "no verification command configured",
        }
    try:
        args = shlex.split(command)
    except ValueError as exc:
        return False, {"command": command, "exit_code": 2, "summary": str(exc)}
    if not args:
        return False, {"command": command, "exit_code": 2, "summary": "empty command"}
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=600)
    output = (result.stdout + result.stderr)[-4000:]
    return result.returncode == 0, {
        "command": command,
        "exit_code": result.returncode,
        "summary": output.strip() or "command produced no output",
    }


def driver_command(backend: str, model: str, cwd: Path) -> list[str] | None:
    if backend == "fake":
        return None
    if backend == "codex":
        binary = shutil.which("codex")
        if not binary:
            raise StoreError("codex executable is not installed")
        return [
            binary,
            "exec",
            "--json",
            "--color",
            "never",
            "--sandbox",
            "workspace-write",
            "--cd",
            str(cwd),
            "--model",
            model,
            "-",
        ]
    if backend == "cursor":
        binary = shutil.which("cursor-agent")
        if not binary:
            raise StoreError("cursor-agent executable is not installed")
        return [
            binary,
            "--print",
            "--output-format",
            "stream-json",
            "--sandbox",
            "enabled",
            "--auto-review",
            "--trust",
            "--workspace",
            str(cwd),
            "--model",
            model,
        ]
    raise StoreError(f"unsupported backend: {backend}")


def process_expired_workers(store: Store) -> int:
    now = utcnow()
    changed = 0
    with store.connect() as db:
        rows = db.execute(
            "SELECT id, mission_id FROM processes WHERE state IN ('created','running') AND token_expires_at <= ?",
            (now,),
        ).fetchall()
        for row in rows:
            db.execute(
                "UPDATE processes SET state='orphaned', updated_at=? WHERE id=?", (now, row["id"])
            )
            store._emit(
                db,
                "process.orphaned",
                "process",
                row["id"],
                {"mission_id": row["mission_id"], "reason": "process lease expired"},
                "critical",
            )
            changed += 1
    return changed
