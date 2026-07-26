"""Run E2E-RECOVERY-01 with one fresh Codex process.

The runner materializes and bootstraps the stalled mission. After Codex starts,
the runner performs no lifecycle operation; it only captures output and invokes
the read-only grader.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

MODEL = "gpt-5.6-sol"
SCENARIO_ID = "E2E-RECOVERY-01"
SAFE_HOST_ENVIRONMENT = {
    "CODEX_HOME",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "SHELL",
    "TERM",
    "TMPDIR",
    "USER",
    "UV_CACHE_DIR",
}
REQUEST = (
    "Recover a stalled worker without losing its partial work. Keep one attempt, process, and "
    "work order, then stop at verification.\n"
)
CONTRACT = """# Recovery contract

## Outcome

Recover the interrupted work in place and advance the mission to verification.

## Acceptance

- [ ] Preserve the first line already written by the stopped worker
- [ ] Append the exact required second line and final newline
- [ ] Use one attempt, one process, one work order, and non-overlapping worker authority
- [ ] Finish with a receipt bound to the replacement session and authority epoch

## Constraints

- Keep the existing workspace and admitted work order
- Never persist or print worker capability material
- Stop at verifying because no general verifier is available

## Out of scope

- Automatic monitoring, retry policy, verification, approval, or delivery

## Open questions

None.
"""
WORK_ORDER = """# Recovery work order

Complete `work/recovered.txt` in place.

Preserve its existing first line and append exactly:

```text
recovery completed by replacement
```

The finished file must include a final newline. Record completion notes and a receipt bound to the
current replacement session and authority epoch.
"""
AUTHORIZE = {
    "decision": "authorized",
    "decision_source_hash": hashlib.sha256(b"recovery-eval-authorization").hexdigest(),
    "human_principal": "human:recovery-eval",
    "schema_version": 1,
}
STALLED_WORKER = """#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
    exit 2
fi

output_path=$1
ready_path=$2
mkdir -p "$(dirname "$output_path")"
printf '%s\\n' 'partial work survived' >"$output_path"
: >"$ready_path"
trap 'exit 143' TERM INT
while :; do
    sleep 1
done
"""


class TrialError(RuntimeError):
    """Trial infrastructure failed."""


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _allocate_trial(runs_root: Path) -> Path:
    runs_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    for _ in range(32):
        trial = runs_root / f"{timestamp}-recovery-{secrets.token_hex(4)}"
        try:
            trial.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return trial
    raise TrialError("could not allocate a unique recovery trial")


def _copy_regular_file(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise TrialError(f"trial input must be a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _write_text(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700 if executable else 0o600)


def _materialize_trial(repository: Path, trial: Path) -> None:
    scenario = repository / "evals" / "recovery"
    for name in ("pyproject.toml", "uv.lock", "LICENSE"):
        _copy_regular_file(repository / name, trial / name)
    _copy_regular_file(scenario / "README.md", trial / "README.md")
    _copy_regular_file(scenario / "TASK.md", trial / "TASK.md")
    _copy_regular_file(scenario / "distro" / "AGENTS.md", trial / "AGENTS.md")

    modules_destination = trial / "modules"
    modules_destination.mkdir()
    for source in sorted((repository / "modules").glob("*.py")):
        _copy_regular_file(source, modules_destination / source.name)

    _write_text(trial / "eval-fixture" / "request.md", REQUEST)
    _write_text(trial / "eval-fixture" / "contract.md", CONTRACT)
    _write_text(trial / "eval-fixture" / "work-order.md", WORK_ORDER)
    _write_text(
        trial / "eval-fixture" / "authorize.json",
        json.dumps(AUTHORIZE, indent=2, sort_keys=True) + "\n",
    )
    _write_text(
        trial / "eval-fixture" / "stalled-worker.sh",
        STALLED_WORKER,
        executable=True,
    )


def _environment(
    host_environment: Mapping[str, str],
    *,
    trial: Path,
    liaison_session_id: str,
    worker_token: str,
) -> dict[str, str]:
    environment = {
        name: value for name, value in host_environment.items() if name in SAFE_HOST_ENVIRONMENT
    }
    environment.update(
        {
            "AOS_LIAISON_SESSION_ID": liaison_session_id,
            "AOS_MISSIONS_ROOT": str(trial / "missions"),
            "AOS_STATE_ROOT": str(trial / "state"),
            "AOS_WORKER_TOKEN": worker_token,
            "PYTHONUTF8": "1",
        }
    )
    return environment


def _run_bootstrap(trial: Path, environment: Mapping[str, str]) -> bytes:
    commands = (
        ["uv", "run", "aos-mission", "init", "--op", "OP-RECOVERY-INIT"],
        [
            "uv",
            "run",
            "aos-mission",
            "intake",
            "M-RECOVERY",
            "--request",
            "eval-fixture/request.md",
            "--op",
            "OP-RECOVERY-INTAKE",
        ],
        [
            "uv",
            "run",
            "aos-mission",
            "propose-contract",
            "M-RECOVERY",
            "--contract",
            "eval-fixture/contract.md",
            "--op",
            "OP-RECOVERY-PROPOSE",
            "--expect",
            "1",
        ],
        [
            "uv",
            "run",
            "aos-mission",
            "authorize",
            "M-RECOVERY",
            "--decision",
            "eval-fixture/authorize.json",
            "--op",
            "OP-RECOVERY-AUTHORIZE",
            "--expect",
            "1",
        ],
        [
            "uv",
            "run",
            "aos-mission",
            "dispatch",
            "M-RECOVERY",
            "--work-order",
            "eval-fixture/work-order.md",
            "--backend",
            "codex",
            "--model",
            MODEL,
            "--workspace",
            "recovery-worktree",
            "--budget-seconds",
            "600",
            "--op",
            "OP-RECOVERY-DISPATCH",
            "--expect",
            "2",
        ],
        [
            "uv",
            "run",
            "aos-mission",
            "start",
            "M-RECOVERY",
            "--op",
            "OP-RECOVERY-START",
            "--expect",
            "3",
        ],
    )
    captured = bytearray()
    for index, command in enumerate(commands):
        completed = subprocess.run(
            command,
            cwd=trial,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
        captured.extend(
            json.dumps(
                {
                    "command": command,
                    "returncode": completed.returncode,
                    "stderr": completed.stderr.decode(errors="replace"),
                    "stdout": completed.stdout.decode(errors="replace"),
                },
                ensure_ascii=True,
                sort_keys=True,
            ).encode()
            + b"\n"
        )
        if completed.returncode != 0:
            raise TrialError(
                f"bootstrap command failed ({completed.returncode}): {' '.join(command)}"
            )
        if index == 0:
            _write_text(
                trial
                / "state"
                / "liaison"
                / "sessions"
                / "LS-RECOVERY-BOOTSTRAP"
                / "session.md",
                (
                    "# Liaison session LS-RECOVERY-BOOTSTRAP\n\n"
                    "Harness: deterministic eval bootstrap\n"
                    "Focus: materialize M-RECOVERY through the initial stalled worker\n"
                ),
            )
    return bytes(captured)


def _stop_worker_after_partial_edit(
    trial: Path,
    environment: Mapping[str, str],
) -> dict[str, object]:
    output = trial / "work" / "recovered.txt"
    ready = trial / "work" / "partial.ready"
    worker = subprocess.Popen(
        [
            "sh",
            "eval-fixture/stalled-worker.sh",
            "work/recovered.txt",
            "work/partial.ready",
        ],
        cwd=trial,
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 5
    while not ready.is_file() and worker.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if not ready.is_file():
        worker.kill()
        worker.wait(timeout=3)
        raise TrialError("stalled worker did not produce its partial edit")
    worker.terminate()
    try:
        returncode = worker.wait(timeout=3)
    except subprocess.TimeoutExpired:
        worker.kill()
        returncode = worker.wait(timeout=3)
    ready.unlink(missing_ok=True)
    partial = output.read_bytes()
    if partial != b"partial work survived\n":
        raise TrialError("stalled worker wrote unexpected partial content")
    return {
        "partial_sha256": hashlib.sha256(partial).hexdigest(),
        "returncode": returncode,
        "terminated_after_partial_edit": True,
    }


def _codex_binary_and_version() -> tuple[str, str]:
    executable = shutil.which("codex")
    if executable is None:
        raise TrialError("codex CLI is not installed")
    completed = subprocess.run(
        [executable, "--version"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        raise TrialError("could not read codex CLI version")
    return executable, completed.stdout.strip()


def _codex_command(executable: str, final_path: Path) -> list[str]:
    return [
        executable,
        "exec",
        "--model",
        MODEL,
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--json",
        "--output-last-message",
        str(final_path),
        "-",
    ]


def _write_bytes(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    path.chmod(0o600)


def run_trial(
    repository: Path,
    *,
    runs_root: Path | None = None,
    host_environment: Mapping[str, str] | None = None,
) -> tuple[Path, int]:
    repository = repository.resolve()
    scenario = repository / "evals" / "recovery"
    selected_runs_root = (
        repository / "evals" / ".runs"
        if runs_root is None
        else runs_root.expanduser().resolve()
    )
    trial = _allocate_trial(selected_runs_root)
    host = os.environ if host_environment is None else host_environment
    old_token = "aos-recovery-old-" + secrets.token_hex(32)
    replacement_token = "aos-recovery-replacement-" + secrets.token_hex(32)
    bootstrap_environment = _environment(
        host,
        trial=trial,
        liaison_session_id="LS-RECOVERY-BOOTSTRAP",
        worker_token=old_token,
    )
    agent_environment = _environment(
        host,
        trial=trial,
        liaison_session_id="LS-RECOVERY-FRESH",
        worker_token=replacement_token,
    )

    final_temporary: Path | None = None
    try:
        _materialize_trial(repository, trial)
        bootstrap = _run_bootstrap(trial, bootstrap_environment)
        _write_bytes(trial / "bootstrap.jsonl", bootstrap)
        stall = _stop_worker_after_partial_edit(trial, bootstrap_environment)
        _write_text(
            trial / "eval-fixture" / "stall.json",
            json.dumps(stall, indent=2, sort_keys=True) + "\n",
        )
        executable, codex_version = _codex_binary_and_version()

        with tempfile.NamedTemporaryFile(
            prefix="aos-recovery-final-",
            suffix=".txt",
            delete=False,
        ) as stream:
            final_temporary = Path(stream.name)

        command = _codex_command(executable, final_temporary)

        # Agent-runtime boundary: after launch, this runner only waits and
        # records process output. It performs no mission lifecycle operation.
        completed = subprocess.run(
            command,
            cwd=trial,
            env=agent_environment,
            input=(trial / "TASK.md").read_bytes(),
            capture_output=True,
            check=False,
        )
        trajectory = bytes(completed.stdout or b"")
        stderr = bytes(completed.stderr or b"")
        final = final_temporary.read_bytes() if final_temporary.is_file() else b""
        trajectory_path = trial / "trajectory.jsonl"
        final_path = trial / "final.txt"
        _write_bytes(trajectory_path, trajectory)
        _write_bytes(trial / "codex.stderr.log", stderr)
        _write_bytes(final_path, final)
        _write_text(
            trial / "run.json",
            json.dumps(
                {
                    "codex_command": command,
                    "codex_returncode": completed.returncode,
                    "codex_version": codex_version,
                    "model": MODEL,
                    "scenario_id": SCENARIO_ID,
                    "schema_version": 1,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        graded = subprocess.run(
            [
                sys.executable,
                str(scenario / "grade.py"),
                "--trial",
                str(trial),
                "--trajectory",
                str(trajectory_path),
                "--final",
                str(final_path),
                "--output",
                str(trial / "grade.json"),
            ],
            cwd=repository,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
        sys.stdout.buffer.write(graded.stdout or b"")
        sys.stderr.buffer.write(graded.stderr or b"")
        return trial, 0 if completed.returncode == 0 and graded.returncode == 0 else 1
    finally:
        if final_temporary is not None:
            final_temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs-root",
        type=Path,
        help="override the ignored evals/.runs output directory",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        trial, returncode = run_trial(
            _repository_root(),
            runs_root=arguments.runs_root,
        )
    except (OSError, TrialError, subprocess.SubprocessError) as exc:
        print(f"{SCENARIO_ID} infrastructure error: {exc}", file=sys.stderr)
        return 2
    print(f"{SCENARIO_ID} trial: {trial}")
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
