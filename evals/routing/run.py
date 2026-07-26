"""Run E2E-ROUTING-01 with one fresh Codex liaison."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from evals.routing.grade import grade

MODEL = "gpt-5.6-sol"
SCENARIO_ID = "E2E-ROUTING-01"
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
    """Routing trial infrastructure failed."""


def _allocate_trial(runs_root: Path) -> Path:
    runs_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    for _ in range(32):
        trial = runs_root / f"{timestamp}-routing-{secrets.token_hex(4)}"
        try:
            trial.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return trial
    raise TrialError("could not allocate a unique routing trial")


def _copy_regular_file(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise TrialError(f"trial input must be a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _materialize_trial(repository: Path, trial: Path) -> None:
    scenario = repository / "evals" / "routing"
    _copy_regular_file(scenario / "README.md", trial / "README.md")
    _copy_regular_file(scenario / "TASK.md", trial / "TASK.md")
    _copy_regular_file(repository / "protocols" / "routing.md", trial / "protocols" / "routing.md")
    for source in sorted((scenario / "fixtures").glob("*")):
        _copy_regular_file(source, trial / "eval-fixture" / source.name)


def _safe_environment(host_environment: Mapping[str, str]) -> dict[str, str]:
    environment = {
        name: value for name, value in host_environment.items() if name in SAFE_HOST_ENVIRONMENT
    }
    environment["PYTHONUTF8"] = "1"
    return environment


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


def run_trial(
    repository: Path,
    *,
    runs_root: Path | None = None,
    host_environment: Mapping[str, str] | None = None,
    agent_runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    codex_executable: str | None = None,
    codex_version: str | None = None,
) -> tuple[Path, int]:
    repository = repository.resolve()
    selected_runs_root = (
        repository / "evals" / ".runs"
        if runs_root is None
        else runs_root.expanduser().resolve()
    )
    trial = _allocate_trial(selected_runs_root)
    _materialize_trial(repository, trial)
    if codex_executable is None or codex_version is None:
        executable, version = _codex_binary_and_version()
    else:
        executable, version = codex_executable, codex_version
    environment = _safe_environment(
        os.environ if host_environment is None else host_environment
    )
    final_path = trial / "final.txt"
    command = _codex_command(executable, final_path)
    completed = agent_runner(
        command,
        cwd=trial,
        env=environment,
        input=(trial / "TASK.md").read_bytes(),
        capture_output=True,
        check=False,
    )
    (trial / "trajectory.jsonl").write_bytes(bytes(completed.stdout or b""))
    (trial / "codex.stderr.log").write_bytes(bytes(completed.stderr or b""))
    (trial / "run.json").write_text(
        json.dumps(
            {
                "codex_command": command,
                "codex_returncode": completed.returncode,
                "codex_version": version,
                "model": MODEL,
                "scenario_id": SCENARIO_ID,
                "schema_version": 1,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    result = grade(trial)
    (trial / "grade.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return trial, 0 if completed.returncode == 0 and result["status"] == "pass" else 1


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        trial, returncode = run_trial(_repository_root(), runs_root=arguments.runs_root)
    except (OSError, TrialError, subprocess.SubprocessError) as exc:
        print(f"{SCENARIO_ID} infrastructure error: {exc}", file=sys.stderr)
        return 2
    print(f"{SCENARIO_ID} trial: {trial}")
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
