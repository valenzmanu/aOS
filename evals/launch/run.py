"""Run one private live E2E-LAUNCH-01 worker-adapter trial."""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

SCENARIO_ID = "E2E-LAUNCH-01"
HARNESSES = ("codex", "cursor")


class TrialError(RuntimeError):
    """Live launch trial infrastructure failed."""


def _allocate_trial(runs_root: Path, worker_harness: str) -> Path:
    runs_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    for _ in range(32):
        trial = runs_root / f"{timestamp}-launch-{worker_harness}-{secrets.token_hex(4)}"
        try:
            trial.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return trial
    raise TrialError("could not allocate a unique live launch trial")


def _initialize_workspace(workspace: Path) -> None:
    workspace.mkdir()
    completed = subprocess.run(
        ["git", "init", "--quiet", str(workspace)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise TrialError("could not initialize isolated live workspace")


def _adapter_path(repository: Path, worker_harness: str) -> Path:
    return (
        repository
        / "distro"
        / "skills"
        / f"launch-{worker_harness}-worker"
        / "scripts"
        / "adapter.py"
    )


def _grade(
    *,
    trial: Path,
    worker_harness: str,
    model: str,
    reasoning_effort: str,
    billing_channel: str,
    adapter_returncode: int,
) -> dict[str, object]:
    result_path = trial / "workspace" / "launch-result.json"
    observation_path = trial / "output" / "observation.json"
    result = json.loads(result_path.read_text()) if result_path.is_file() else None
    observation = (
        json.loads(observation_path.read_text()) if observation_path.is_file() else {}
    )
    checks = [
        {
            "name": "adapter_completed",
            "passed": adapter_returncode == 0 and observation.get("exit_status") == 0,
        },
        {
            "name": "route_identity_preserved",
            "passed": all([
                observation.get("harness") == worker_harness,
                observation.get("model") == model,
                observation.get("reasoning_effort") == reasoning_effort,
                observation.get("billing_channel") == billing_channel,
                observation.get("billing_channel_source")
                == "admitted_route_argument",
            ]),
        },
        {
            "name": "normalized_observation",
            "passed": (
                isinstance(observation.get("version"), str)
                and bool(observation["version"])
                and isinstance(observation.get("usage"), dict)
            ),
        },
        {
            "name": "worker_result",
            "passed": result
            == {"scenario_id": SCENARIO_ID, "status": "complete"},
        },
    ]
    return {
        "checks": checks,
        "scenario_id": SCENARIO_ID,
        "status": "pass" if all(check["passed"] for check in checks) else "fail",
        "worker_harness": worker_harness,
    }


def run_live_trial(
    repository: Path,
    *,
    worker_harness: str,
    model: str,
    reasoning_effort: str,
    billing_channel: str,
    runs_root: Path | None = None,
    adapter_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[Path, int]:
    if worker_harness not in HARNESSES:
        raise TrialError(f"unsupported worker harness: {worker_harness}")
    repository = repository.resolve()
    selected_runs_root = (
        repository / "evals" / ".runs"
        if runs_root is None
        else runs_root.expanduser().resolve()
    )
    trial = _allocate_trial(selected_runs_root, worker_harness)
    workspace = trial / "workspace"
    _initialize_workspace(workspace)
    work_order = trial / "work-order.md"
    work_order.write_text(
        """# E2E-LAUNCH-01 live worker

Work only inside the current workspace. Do not inspect parent directories, credentials, or
environment variables. Do not use subagents.

Create `launch-result.json` with exactly this JSON value:

```json
{"scenario_id": "E2E-LAUNCH-01", "status": "complete"}
```

Then respond exactly: `E2E-LAUNCH-01 complete.`
"""
    )
    admitted_route = trial / "admitted-route.md"
    admitted_route.write_text(
        "\n".join([
            "# Admitted route",
            "",
            f"- worker harness: `{worker_harness}`",
            f"- model: `{model}`",
            f"- reasoning effort: `{reasoning_effort}`",
            f"- billing channel: `{billing_channel}`",
            "",
        ])
    )
    output_dir = trial / "output"
    command = [
        sys.executable,
        str(_adapter_path(repository, worker_harness)),
        "launch",
        "--model",
        model,
        "--reasoning-effort",
        reasoning_effort,
        "--billing-channel",
        billing_channel,
        "--workspace",
        str(workspace),
        "--work-order",
        str(work_order),
        "--admitted-route",
        str(admitted_route),
        "--output-dir",
        str(output_dir),
    ]
    completed = adapter_runner(
        command,
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    (trial / "runner.stdout.log").write_text(completed.stdout or "")
    (trial / "runner.stderr.log").write_text(completed.stderr or "")
    grade = _grade(
        trial=trial,
        worker_harness=worker_harness,
        model=model,
        reasoning_effort=reasoning_effort,
        billing_channel=billing_channel,
        adapter_returncode=completed.returncode,
    )
    (trial / "grade.json").write_text(
        json.dumps(grade, indent=2, sort_keys=True) + "\n"
    )
    (trial / "run.json").write_text(
        json.dumps(
            {
                "adapter_returncode": completed.returncode,
                "billing_channel": billing_channel,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "scenario_id": SCENARIO_ID,
                "schema_version": 1,
                "worker_harness": worker_harness,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return trial, 0 if grade["status"] == "pass" else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-harness", choices=HARNESSES, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning-effort", required=True)
    parser.add_argument("--billing-channel", required=True)
    parser.add_argument("--runs-root", type=Path)
    return parser


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        trial, returncode = run_live_trial(
            _repository_root(),
            worker_harness=arguments.worker_harness,
            model=arguments.model,
            reasoning_effort=arguments.reasoning_effort,
            billing_channel=arguments.billing_channel,
            runs_root=arguments.runs_root,
        )
    except (OSError, TrialError, subprocess.SubprocessError, ValueError) as exc:
        print(f"{SCENARIO_ID} infrastructure error: {exc}", file=sys.stderr)
        return 2
    print(f"{SCENARIO_ID} {arguments.worker_harness} trial: {trial}")
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
