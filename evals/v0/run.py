"""Run the permanent E2E-00 tracer with one fresh Codex process.

The runner owns only trial materialization and the two fixture bootstrap
operations.  Once Codex starts, the agent is the lifecycle runtime.  The
runner does not invoke ``aos-v0`` again; it only captures output and starts the
read-only grader.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

CODEX_VERSION = "codex-cli 0.144.1"
MODEL = "gpt-5.6-sol"
SCENARIO_ID = "E2E-00"
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


class TrialError(RuntimeError):
    """The trial infrastructure failed."""


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _allocate_trial(runs_root: Path) -> Path:
    runs_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    for _ in range(32):
        trial = runs_root / f"{timestamp}-e2e-00-{secrets.token_hex(4)}"
        try:
            trial.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return trial
    raise TrialError("could not allocate a unique E2E-00 trial directory")


def _copy_regular_file(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise TrialError(f"trial input must be a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _materialize_trial(repository: Path, trial: Path) -> None:
    scenario = repository / "evals" / "v0"
    for name in ("pyproject.toml", "uv.lock", "LICENSE"):
        _copy_regular_file(repository / name, trial / name)
    _copy_regular_file(scenario / "README.md", trial / "README.md")

    modules_source = repository / "modules"
    modules_destination = trial / "modules"
    modules_destination.mkdir()
    for source in sorted(modules_source.glob("*.py")):
        _copy_regular_file(source, modules_destination / source.name)

    _copy_regular_file(scenario / "distro" / "AGENTS.md", trial / "AGENTS.md")
    _copy_regular_file(
        scenario / "distro" / "protocols" / "mission.md",
        trial / "protocols" / "mission.md",
    )
    _copy_regular_file(scenario / "TASK.md", trial / "TASK.md")
    _copy_regular_file(
        scenario / "fixtures" / "objective.md",
        trial / "eval-fixture" / "objective.md",
    )
    _copy_regular_file(
        scenario / "fixtures" / "expected-result.txt",
        trial / "eval-fixture" / "expected-result.txt",
    )
    _copy_regular_file(
        scenario / "fixtures" / "fake-worker.sh",
        trial / "eval-fixture" / "fake-worker.sh",
    )
    for name in ("authorize.json", "approve.json"):
        _copy_regular_file(
            scenario / "fixtures" / "decisions" / name,
            trial / "decisions" / name,
        )


def _safe_environment(
    host_environment: Mapping[str, str],
    *,
    trial: Path,
    liaison_session_id: str,
) -> dict[str, str]:
    environment = {
        name: value for name, value in host_environment.items() if name in SAFE_HOST_ENVIRONMENT
    }
    environment.update(
        {
            "AOS_LIAISON_SESSION_ID": liaison_session_id,
            "AOS_MISSIONS_ROOT": str(trial / "missions"),
            "AOS_STATE_ROOT": str(trial / "state"),
            "PYTHONUTF8": "1",
        }
    )
    return environment


def _run_bootstrap(
    trial: Path,
    environment: Mapping[str, str],
    *,
    runner: object = subprocess.run,
) -> bytes:
    commands = (
        ["uv", "run", "aos-v0", "init", "--op", "OP-V0-INIT"],
        [
            "uv",
            "run",
            "aos-v0",
            "setup",
            "M-V0",
            "--op",
            "OP-V0-SETUP",
            "--objective",
            "eval-fixture/objective.md",
        ],
    )
    captured = bytearray()
    for command in commands:
        completed = runner(  # type: ignore[operator]
            command,
            cwd=trial,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        stdout = bytes(completed.stdout or b"")
        stderr = bytes(completed.stderr or b"")
        captured.extend(
            json.dumps(
                {
                    "command": command,
                    "returncode": int(completed.returncode),
                    "stderr": stderr.decode("utf-8", errors="replace"),
                    "stdout": stdout.decode("utf-8", errors="replace"),
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
    return bytes(captured)


def _codex_binary_and_version() -> tuple[str, str]:
    executable = shutil.which("codex")
    if executable is None:
        raise TrialError("codex CLI is not installed")
    resolved = Path(executable).resolve()
    observed_version: str | None = None
    for parent in (resolved, *resolved.parents):
        if parent.name == "0.144.1":
            observed_version = parent.name
            break
        package_json = parent / "package.json"
        if not package_json.is_file():
            continue
        try:
            package = json.loads(package_json.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(package, dict)
            and package.get("name") in {"@openai/codex", "codex"}
            and isinstance(package.get("version"), str)
        ):
            observed_version = package["version"]
            break
    if observed_version != "0.144.1":
        raise TrialError(
            f"E2E-00 requires codex-cli 0.144.1 and could not verify that version from {resolved}"
        )
    return executable, CODEX_VERSION


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


def _write_manifest(
    path: Path,
    *,
    codex_version: str,
    command: Sequence[str],
    codex_returncode: int,
) -> None:
    value = {
        "schema_version": 1,
        "scenario_id": SCENARIO_ID,
        "codex_version": codex_version,
        "model": MODEL,
        "codex_command": list(command),
        "codex_returncode": codex_returncode,
    }
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def run_trial(
    repository: Path,
    *,
    runs_root: Path | None = None,
    host_environment: Mapping[str, str] | None = None,
) -> tuple[Path, int]:
    """Materialize and execute one E2E-00 trial."""

    repository = repository.resolve()
    scenario = repository / "evals" / "v0"
    selected_runs_root = (
        (repository / "evals" / ".runs") if runs_root is None else runs_root.expanduser().resolve()
    )
    trial = _allocate_trial(selected_runs_root)
    liaison_session_id = "LS-V0"
    environment = _safe_environment(
        os.environ if host_environment is None else host_environment,
        trial=trial,
        liaison_session_id=liaison_session_id,
    )

    final_temporary: Path | None = None
    try:
        _materialize_trial(repository, trial)
        executable, codex_version = _codex_binary_and_version()
        bootstrap = _run_bootstrap(trial, environment)
        _write_bytes(trial / "bootstrap.jsonl", bootstrap)

        with tempfile.NamedTemporaryFile(
            prefix="aos-e2e-00-final-",
            suffix=".txt",
            delete=False,
        ) as stream:
            final_temporary = Path(stream.name)

        command = _codex_command(executable, final_temporary)
        objective = (trial / "TASK.md").read_bytes()

        # Constitutional handoff: after this process starts, the runner makes
        # no lifecycle calls.  Only the agent may invoke aos-v0.
        completed = subprocess.run(
            command,
            cwd=trial,
            env=environment,
            input=objective,
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
        _write_manifest(
            trial / "run.json",
            codex_version=codex_version,
            command=command,
            codex_returncode=int(completed.returncode),
        )

        grade_command = [
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
        ]
        graded = subprocess.run(
            grade_command,
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
        print(f"E2E-00 infrastructure error: {exc}", file=sys.stderr)
        return 2
    print(f"E2E-00 trial: {trial}")
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
