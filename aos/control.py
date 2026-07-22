from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from .store import (
    AuthorizationError,
    Store,
    StoreError,
    TransitionError,
    future,
    token_hash,
    utcnow,
)

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
    store.create_contract_revision(
        mission_id,
        {
            "schema_version": 1,
            "objective": objective,
            "project": str(project_path),
            "allowed_backends": backends,
            "allowed_models": models,
            "allow_anthropic": allow_anthropic,
            "verification_command": verify_command,
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
    mission: dict[str, Any],
    requested_backend: str | None,
    requested_model: str | None,
    store: Store | None = None,
) -> tuple[str, str, str]:
    allowed_backends = mission["allowed_backends"]
    if requested_backend and requested_backend not in allowed_backends:
        raise AuthorizationError(
            f"backend {requested_backend!r} is not authorized by the mission contract"
        )
    candidates = [requested_backend] if requested_backend else list(allowed_backends)
    rejections: list[str] = []
    eligible: list[tuple[int, str, str, str]] = []
    for index, backend in enumerate(candidates):
        observation = store.latest_provider_status(backend) if store else None
        status = observation["status"] if observation else "unknown"
        if status in {"limited", "unhealthy"}:
            rejections.append(f"{backend} rejected: status={status}")
            continue
        model = requested_model or MODEL_DEFAULTS[backend]
        discovered_models = observation["details"].get("models", []) if observation else []
        if discovered_models and model not in discovered_models:
            rejections.append(f"{backend}/{model} rejected: absent from discovered catalog")
            continue
        allowed_models = mission["allowed_models"]
        if allowed_models and model not in allowed_models:
            rejections.append(f"{backend}/{model} rejected: model not authorized")
            continue
        anthropic = "claude" in model.lower() or "anthropic" in model.lower()
        if anthropic and not mission["allow_anthropic"]:
            rejections.append(f"{backend}/{model} rejected: Anthropic not authorized")
            continue
        health_rank = 1 if status == "degraded" else 0
        eligible.append((health_rank * 1000 + index, backend, model, status))
    if not eligible:
        detail = "; ".join(rejections) or "no authorized candidates"
        raise AuthorizationError(f"no eligible route: {detail}")
    _, backend, model, status = min(eligible)
    reason = (
        f"explicit backend={backend}"
        if requested_backend
        else f"highest-ranked eligible backend={backend}"
    )
    reason += f"; {'explicit' if requested_model else 'policy default'} model={model}"
    reason += f"; provider_status={status}"
    if rejections:
        reason += f"; alternatives: {' | '.join(rejections)}"
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


def work_order_content(
    store: Store,
    mission: dict[str, Any],
    process_id: str,
    retry_level: str | None = None,
) -> dict[str, Any]:
    contract = store.contract(mission["id"])
    content: dict[str, Any] = {
        "schema_version": 1,
        "mission_id": mission["id"],
        "process_id": process_id,
        "contract_revision": contract["revision"],
        "contract_hash": contract["content_hash"],
        "objective": mission["objective"],
        "constraints": [
            "Work only in the assigned git worktree.",
            "Do not spawn or delegate to other agents.",
            "Do not expose credentials, local state, or unrelated source content.",
            "Do not commit or push; aOS retains delivery authority.",
        ],
        "verification_command": mission["verify_command"],
        "output_contract": (
            "Implement the objective, verify it, and summarize files changed and remaining risks."
        ),
    }
    if retry_level:
        content["retry"] = {
            "level": retry_level,
            "instruction": "Inspect existing work and evidence; do not repeat a failed approach blindly.",
        }
    return content


def spawn_process_worker(store: Store, process_id: str, process_token: str) -> dict[str, Any]:
    process = next((item for item in store.processes() if item["id"] == process_id), None)
    if not process:
        raise StoreError(f"process not found: {process_id}")
    destination = Path(process["worktree"])
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
        {
            "pid": proc.pid,
            "artifact": str(artifact),
            "routing_reason": process["routing_reason"],
            "worker_session_id": process["session_id"],
        },
    )
    return {
        "mission_id": process["mission_id"],
        "process_id": process_id,
        "worker_session_id": process["session_id"],
        "pid": proc.pid,
        "backend": process["backend"],
        "model": process["model"],
        "worktree": str(destination),
        "artifact": str(artifact),
        "routing_reason": process["routing_reason"],
    }


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
    selected_backend, selected_model, reason = select_route(mission, backend, model, store)
    attempt = len(store.attempts(mission_id)) + 1
    destination = store.home.parent / "projects" / f"{mission_id.lower()}-a{attempt}"
    branch = f"aos/{mission_id.lower()}/attempt-{attempt}"
    make_worktree(mission["project"], destination, branch)
    store.create_attempt(mission_id, attempt, str(destination), branch)
    process_id, process_token = store.create_process(
        mission_id, selected_backend, selected_model, str(destination), reason, attempt=attempt
    )
    store.create_work_order(process_id, work_order_content(store, mission, process_id))
    store.create_worker_session(process_id)
    store.transition_mission(mission_id, "dispatched", {"authorized"})
    return spawn_process_worker(store, process_id, process_token)


def retry_mission(
    store: Store,
    token: str | None,
    mission_id: str,
    level: str,
    backend: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    store.require_liaison(token)
    mission = store.mission(mission_id)
    if mission["state"] != "failed":
        raise TransitionError(f"only failed missions can retry (state={mission['state']})")
    if level not in {"session", "process", "attempt"}:
        raise StoreError(f"unknown retry level: {level}")
    processes = store.processes(mission_id)
    if not processes:
        raise StoreError("mission has no process to retry")
    previous = processes[-1]

    if level == "session":
        process_id = previous["id"]
        process_token = store.rotate_process_authority(process_id)
        store.create_work_order(
            process_id, work_order_content(store, mission, process_id, retry_level=level)
        )
        store.create_worker_session(process_id)
        store.update_attempt_state(mission_id, previous["attempt"], "active")
    elif level == "process":
        selected_backend, selected_model, reason = select_route(mission, backend, model, store)
        process_id, process_token = store.create_process(
            mission_id,
            selected_backend,
            selected_model,
            previous["worktree"],
            f"process retry; {reason}",
            attempt=previous["attempt"],
        )
        store.create_work_order(
            process_id, work_order_content(store, mission, process_id, retry_level=level)
        )
        store.create_worker_session(process_id)
        store.update_attempt_state(mission_id, previous["attempt"], "active")
    else:
        selected_backend, selected_model, reason = select_route(mission, backend, model, store)
        attempt = len(store.attempts(mission_id)) + 1
        destination = store.home.parent / "projects" / f"{mission_id.lower()}-a{attempt}"
        branch = f"aos/{mission_id.lower()}/attempt-{attempt}"
        make_worktree(mission["project"], destination, branch)
        store.create_attempt(mission_id, attempt, str(destination), branch)
        process_id, process_token = store.create_process(
            mission_id,
            selected_backend,
            selected_model,
            str(destination),
            f"attempt retry; {reason}",
            attempt=attempt,
        )
        store.create_work_order(
            process_id, work_order_content(store, mission, process_id, retry_level=level)
        )
        store.create_worker_session(process_id)

    store.event(
        "retry.started",
        "mission",
        mission_id,
        {"level": level, "process_id": process_id, "previous_process_id": previous["id"]},
    )
    store.transition_mission(mission_id, "dispatched", {"failed"})
    return spawn_process_worker(store, process_id, process_token)


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


def discover_provider(backend: str) -> dict[str, Any]:
    if backend == "fake":
        return {
            "backend": "fake",
            "available": True,
            "authenticated": True,
            "models": ["fake-deterministic"],
            "catalog_source": "built_in",
        }
    if backend == "cursor":
        binary = shutil.which("cursor-agent")
        if not binary:
            return {
                "backend": backend,
                "available": False,
                "authenticated": False,
                "models": [],
                "catalog_source": "cursor_agent_cli",
            }
        status = subprocess.run([binary, "status"], capture_output=True, text=True, timeout=20)
        catalog = subprocess.run(
            [binary, "--list-models"], capture_output=True, text=True, timeout=30
        )
        models = []
        if catalog.returncode == 0:
            for line in catalog.stdout.splitlines():
                if " - " in line and not line.startswith(("Available ", " ")):
                    models.append(line.split(" - ", 1)[0].strip())
        return {
            "backend": backend,
            "available": True,
            "authenticated": status.returncode == 0,
            "models": models,
            "catalog_source": "cursor_agent_cli",
        }
    if backend == "codex":
        binary = shutil.which("codex")
        if not binary:
            return {
                "backend": backend,
                "available": False,
                "authenticated": False,
                "models": [],
                "catalog_source": "unavailable",
            }
        status = subprocess.run(
            [binary, "login", "status"], capture_output=True, text=True, timeout=20
        )
        version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20)
        return {
            "backend": backend,
            "available": True,
            "authenticated": status.returncode == 0,
            "models": [],
            "catalog_source": "not_exposed_by_codex_cli",
            "version": version.stdout.strip() if version.returncode == 0 else "unknown",
        }
    raise StoreError(f"unsupported backend: {backend}")


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
            """SELECT id, mission_id, attempt, session_id FROM processes
               WHERE state IN ('created','running') AND token_expires_at <= ?""",
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
            if row["session_id"]:
                db.execute(
                    "UPDATE worker_sessions SET state='orphaned', ended_at=? WHERE id=?",
                    (now, row["session_id"]),
                )
            db.execute(
                """UPDATE attempts SET state='orphaned', updated_at=?
                   WHERE mission_id=? AND number=?""",
                (now, row["mission_id"], row["attempt"]),
            )
            mission = db.execute(
                "SELECT state FROM missions WHERE id=?", (row["mission_id"],)
            ).fetchone()
            if mission and mission["state"] in {"dispatched", "implementing", "verifying"}:
                db.execute(
                    "UPDATE missions SET state='failed', updated_at=? WHERE id=?",
                    (now, row["mission_id"]),
                )
                store._emit(
                    db,
                    "mission.transitioned",
                    "mission",
                    row["mission_id"],
                    {"from": mission["state"], "to": "failed", "reason": "process orphaned"},
                    "critical",
                )
            changed += 1
    return changed


def signal_process(store: Store, token: str | None, process_id: str, action: str) -> dict[str, Any]:
    store.require_liaison(token)
    if action not in {"pause", "resume", "cancel"}:
        raise StoreError(f"unsupported process signal: {action}")
    with store.connect() as db:
        process = db.execute("SELECT * FROM processes WHERE id=?", (process_id,)).fetchone()
    if not process:
        raise StoreError(f"process not found: {process_id}")
    if not process["pid"]:
        raise StoreError(f"process has no live operating-system handle: {process_id}")
    required_states = {
        "pause": {"created", "running"},
        "resume": {"paused"},
        "cancel": {"created", "running", "paused"},
    }
    if process["state"] not in required_states[action]:
        raise TransitionError(f"cannot {action} process in state {process['state']}")
    os_signal = {
        "pause": signal.SIGSTOP,
        "resume": signal.SIGCONT,
        "cancel": signal.SIGTERM,
    }[action]
    try:
        os.kill(process["pid"], os_signal)
    except ProcessLookupError as exc:
        raise StoreError(f"operating-system process is no longer present: {process_id}") from exc

    target = {"pause": "paused", "resume": "running", "cancel": "cancelled"}[action]
    now = utcnow()
    with store.connect() as db:
        if action == "cancel":
            db.execute(
                """UPDATE processes
                   SET state='cancelled', token_hash=?, token_expires_at=?, updated_at=?
                   WHERE id=?""",
                (token_hash(secrets.token_urlsafe(32)), now, now, process_id),
            )
            if process["session_id"]:
                db.execute(
                    "UPDATE worker_sessions SET state='cancelled', ended_at=? WHERE id=?",
                    (now, process["session_id"]),
                )
            db.execute(
                """UPDATE attempts SET state='cancelled', updated_at=?
                   WHERE mission_id=? AND number=?""",
                (now, process["mission_id"], process["attempt"]),
            )
        else:
            db.execute(
                "UPDATE processes SET state=?, token_expires_at=?, updated_at=? WHERE id=?",
                (target, future(900), now, process_id),
            )
            if process["session_id"]:
                db.execute(
                    "UPDATE worker_sessions SET state=? WHERE id=?",
                    (target, process["session_id"]),
                )
        store._emit(
            db,
            f"process.{target}",
            "process",
            process_id,
            {"mission_id": process["mission_id"], "action": action},
            "attention" if action == "cancel" else "info",
        )
    if action == "cancel":
        current = store.mission(process["mission_id"])["state"]
        if current in {"dispatched", "implementing", "verifying", "failed"}:
            store.transition_mission(process["mission_id"], "cancelled", {current})
    return {
        "process_id": process_id,
        "mission_id": process["mission_id"],
        "action": action,
        "state": target,
    }
