"""Run E2E-DELEGATION-01 with one fresh Codex worker per execution mode."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.delegation.grade import _completed_collaboration_waits, _spawn_tasks, grade

MODEL = "gpt-5.6-sol"
SCENARIO_ID = "E2E-DELEGATION-01"
MODES = ("denied", "native-read-parallel")
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
    """Delegation trial infrastructure failed."""


def _allocate_trial(runs_root: Path, mode: str) -> Path:
    runs_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    for _ in range(32):
        trial = runs_root / f"{timestamp}-delegation-{mode}-{secrets.token_hex(4)}"
        try:
            trial.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return trial
    raise TrialError("could not allocate a unique delegation trial")


def _copy_regular_file(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise TrialError(f"trial input must be a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _materialize_trial(repository: Path, trial: Path) -> None:
    scenario = repository / "evals" / "delegation"
    _copy_regular_file(scenario / "README.md", trial / "README.md")
    _copy_regular_file(scenario / "TASK.md", trial / "TASK.md")
    _copy_regular_file(
        repository / "protocols" / "delegation.md",
        trial / "protocols" / "delegation.md",
    )
    for source in sorted((scenario / "fixtures").glob("*")):
        _copy_regular_file(source, trial / "inputs" / source.name)
    initialized = subprocess.run(
        ["git", "init", "--quiet", str(trial)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if initialized.returncode != 0:
        raise TrialError("could not initialize isolated trial Git root")


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


def _codex_command(executable: str, final_path: Path, mode: str) -> list[str]:
    feature_flag = "--enable" if mode == "native-read-parallel" else "--disable"
    command = [
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
        feature_flag,
        "multi_agent",
    ]
    if mode == "native-read-parallel":
        command.extend(("--enable", "multi_agent_v2"))
    command.extend([
        "--json",
        "--output-last-message",
        str(final_path),
        "-",
    ])
    return command


def _prompt(trial: Path, mode: str) -> bytes:
    task = (trial / "TASK.md").read_text()
    return f"{task}\n\n## Runner-selected execution mode\n\n`{mode}`\n".encode()


def _usage(trajectory: Path) -> dict[str, int]:
    usage: dict[str, int] = {}
    for line in trajectory.read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("type") != "turn.completed":
            continue
        observed = event.get("usage")
        if not isinstance(observed, dict):
            continue
        usage = {
            name: value
            for name, value in observed.items()
            if isinstance(name, str) and isinstance(value, int)
        }
    return usage


def run_trial(
    repository: Path,
    *,
    mode: str,
    runs_root: Path | None = None,
    host_environment: Mapping[str, str] | None = None,
    agent_runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    codex_executable: str | None = None,
    codex_version: str | None = None,
) -> tuple[Path, int]:
    if mode not in MODES:
        raise TrialError(f"unsupported delegation mode: {mode}")
    repository = repository.resolve()
    selected_runs_root = (
        repository / "evals" / ".runs"
        if runs_root is None
        else runs_root.expanduser().resolve()
    )
    trial = _allocate_trial(selected_runs_root, mode)
    _materialize_trial(repository, trial)
    if codex_executable is None or codex_version is None:
        executable, version = _codex_binary_and_version()
    else:
        executable, version = codex_executable, codex_version
    environment = _safe_environment(
        os.environ if host_environment is None else host_environment
    )
    final_path = trial / "final.txt"
    command = _codex_command(executable, final_path, mode)
    started = time.monotonic()
    completed = agent_runner(
        command,
        cwd=trial,
        env=environment,
        input=_prompt(trial, mode),
        capture_output=True,
        check=False,
    )
    elapsed_seconds = time.monotonic() - started
    trajectory = trial / "trajectory.jsonl"
    trajectory.write_bytes(bytes(completed.stdout or b""))
    (trial / "codex.stderr.log").write_bytes(bytes(completed.stderr or b""))
    declared_child_count = None
    summary_path = trial / "work" / "delegation-summary.json"
    if summary_path.is_file():
        declared = json.loads(summary_path.read_text()).get("child_count")
        if isinstance(declared, int):
            declared_child_count = declared
    spawn_tasks = _spawn_tasks(trajectory)
    metrics: dict[str, Any] = {
        "completed_collaboration_waits": _completed_collaboration_waits(trajectory),
        "declared_child_count": declared_child_count,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "mode": mode,
        "observed_spawn_count": len(spawn_tasks),
        "schema_version": 1,
        "usage": _usage(trajectory),
    }
    (trial / "metrics.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True) + "\n"
    )
    (trial / "run.json").write_text(
        json.dumps(
            {
                "codex_command": command,
                "codex_returncode": completed.returncode,
                "codex_version": version,
                "mode": mode,
                "model": MODEL,
                "scenario_id": SCENARIO_ID,
                "schema_version": 1,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    result = grade(trial, trajectory, mode)
    (trial / "grade.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    return trial, 0 if completed.returncode == 0 and result["status"] == "pass" else 1


def _write_comparison(baseline: Path, native: Path) -> None:
    baseline_metrics = json.loads((baseline / "metrics.json").read_text())
    native_metrics = json.loads((native / "metrics.json").read_text())
    comparison = {
        "baseline": baseline_metrics,
        "baseline_trial": baseline.name,
        "native": native_metrics,
        "native_trial": native.name,
        "schema_version": 1,
    }
    (native / "comparison.json").write_text(
        json.dumps(comparison, indent=2, sort_keys=True) + "\n"
    )


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=(*MODES, "both"), default="both")
    parser.add_argument("--runs-root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    modes = MODES if arguments.mode == "both" else (arguments.mode,)
    results: list[tuple[Path, int]] = []
    try:
        for mode in modes:
            trial, returncode = run_trial(
                _repository_root(),
                mode=mode,
                runs_root=arguments.runs_root,
            )
            results.append((trial, returncode))
            print(f"{SCENARIO_ID} {mode} trial: {trial}")
    except (OSError, TrialError, subprocess.SubprocessError, ValueError) as exc:
        print(f"{SCENARIO_ID} infrastructure error: {exc}", file=sys.stderr)
        return 2
    if arguments.mode == "both":
        _write_comparison(results[0][0], results[1][0])
    return 0 if all(returncode == 0 for _, returncode in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
