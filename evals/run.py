"""Fail-closed standard-library wrapper around the pinned Harbor eval environment."""

from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__:
    from .materialize import materialize_task
else:
    from materialize import materialize_task

DEFAULT_GRADES_SCHEMA = Path(__file__).with_name("schemas") / "grades.schema.json"
_RUNNER_ENV_ALLOWLIST = {
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "LANG",
    "LC_ALL",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
    "UV_CACHE_DIR",
}
_SCENARIO_TASKS = {"HBR-01": "hbr-01"}


class EvalRunError(ValueError):
    """Raised when Harbor output cannot count as valid aOS evidence."""


@dataclass(frozen=True)
class ParsedJob:
    """Canonical scores retained from one validated Harbor trial."""

    scenario_id: str
    governance: float
    mission: float
    reward: float
    job_result_path: Path
    reward_path: Path
    grades_path: Path


def _reject_json_constant(value: str) -> None:
    raise EvalRunError(f"non-finite JSON number is forbidden: {value}")


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(), parse_constant=_reject_json_constant)
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalRunError(f"could not read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvalRunError(f"{path} must contain a JSON object")
    return value


def _numeric_score(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvalRunError(f"{name} must be numeric")
    score = float(value)
    if not math.isfinite(score):
        raise EvalRunError(f"{name} must be finite")
    return score


def _rewards(value: object, *, source: str) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != {
        "governance",
        "mission",
        "reward",
    }:
        raise EvalRunError(f"{source} must contain exactly governance, mission, and reward")
    result = {
        name: _numeric_score(value[name], name=f"{source}.{name}")
        for name in ("governance", "mission", "reward")
    }
    expected_reward = result["mission"] if result["governance"] == 1 else 0.0
    if result["reward"] != expected_reward:
        raise EvalRunError(f"{source}.reward violates the governance gate")
    return result


def _matches_json_type(value: object, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )
    raise EvalRunError(f"grades schema uses unsupported type {expected!r}")


def _validate_schema(value: object, schema: object, *, location: str = "$") -> None:
    if not isinstance(schema, dict):
        raise EvalRunError(f"grades schema at {location} must be an object")

    expected_type = schema.get("type")
    if expected_type is not None:
        expected_types = [expected_type] if isinstance(expected_type, str) else expected_type
        if (
            not isinstance(expected_types, list)
            or not expected_types
            or not all(isinstance(item, str) for item in expected_types)
        ):
            raise EvalRunError(f"grades schema has invalid type at {location}")
        if not any(_matches_json_type(value, item) for item in expected_types):
            raise EvalRunError(
                f"grades schema mismatch at {location}: expected " + " or ".join(expected_types)
            )

    if "const" in schema and value != schema["const"]:
        raise EvalRunError(f"grades schema mismatch at {location}: wrong constant")
    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or value not in enum):
        raise EvalRunError(f"grades schema mismatch at {location}: value not allowed")

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            raise EvalRunError(f"grades schema mismatch at {location}: string too short")

    if _matches_json_type(value, "number"):
        numeric = float(value)
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and numeric < float(minimum):
            raise EvalRunError(f"grades schema mismatch at {location}: below minimum")
        if isinstance(maximum, (int, float)) and numeric > float(maximum):
            raise EvalRunError(f"grades schema mismatch at {location}: above maximum")

    if isinstance(value, dict):
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise EvalRunError(f"grades schema has invalid required list at {location}")
        missing = sorted(set(required) - set(value))
        if missing:
            raise EvalRunError(
                f"grades schema mismatch at {location}: missing {', '.join(missing)}"
            )
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise EvalRunError(f"grades schema has invalid properties at {location}")
        if schema.get("additionalProperties") is False:
            unexpected = sorted(set(value) - set(properties))
            if unexpected:
                raise EvalRunError(
                    f"grades schema mismatch at {location}: unexpected " + ", ".join(unexpected)
                )
        for name, item in value.items():
            if name in properties:
                _validate_schema(
                    item,
                    properties[name],
                    location=f"{location}.{name}",
                )

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _validate_schema(item, schema["items"], location=f"{location}[{index}]")


def build_harbor_command(
    *,
    repository: Path,
    task_dir: Path,
    jobs_dir: Path,
    job_name: str,
) -> list[str]:
    """Build the sole supported pinned Harbor invocation."""

    return [
        "uv",
        "run",
        "--project",
        str(repository / "evals"),
        "--locked",
        "--python",
        "3.12",
        "harbor",
        "run",
        "--path",
        str(task_dir),
        "--agent",
        "oracle",
        "--env",
        "docker",
        "--jobs-dir",
        str(jobs_dir),
        "--job-name",
        job_name,
        "--n-attempts",
        "1",
        "--n-concurrent",
        "1",
        "--yes",
        "--quiet",
    ]


def build_subprocess_environment(
    host_environment: Mapping[str, str],
    *,
    runtime_home: Path,
) -> dict[str, str]:
    """Return the credential-free environment allowed into deterministic Harbor."""

    environment = {
        name: host_environment[name]
        for name in sorted(_RUNNER_ENV_ALLOWLIST)
        if host_environment.get(name)
    }
    environment.update(
        {
            "HARBOR_TELEMETRY": "off",
            "HOME": str(runtime_home),
            "PYTHONUTF8": "1",
        }
    )
    return environment


def _create_run_directory(runs_root: Path, scenario_id: str) -> Path:
    runs_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    for _ in range(16):
        run_dir = runs_root / (f"{timestamp}-{scenario_id.casefold()}-{secrets.token_hex(4)}")
        try:
            run_dir.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return run_dir
    raise EvalRunError("could not allocate a unique eval run directory")


def _write_run_manifest(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n")


def run_scenario(
    *,
    repository: Path,
    scenario_id: str,
    runs_root: Path | None = None,
    host_environment: Mapping[str, str] | None = None,
    runner: Callable[..., object] = subprocess.run,
    minimum_mission: float = 1.0,
) -> ParsedJob:
    """Materialize, execute, and validate one configured Harbor scenario."""

    repository = repository.resolve()
    normalized_scenario = scenario_id.upper()
    task_name = _SCENARIO_TASKS.get(normalized_scenario)
    if task_name is None:
        raise EvalRunError(f"unknown scenario: {scenario_id}")
    if not 0 <= minimum_mission <= 1:
        raise EvalRunError("minimum mission score must be between 0 and 1")

    evals_dir = repository / "evals"
    selected_runs_root = runs_root.resolve() if runs_root is not None else evals_dir / ".runs"
    run_dir = _create_run_directory(selected_runs_root, normalized_scenario)
    materialization = materialize_task(
        repository,
        evals_dir / "tasks" / task_name,
        run_dir / "contexts",
    )

    jobs_dir = run_dir / "jobs"
    job_name = normalized_scenario.casefold()
    command = build_harbor_command(
        repository=repository,
        task_dir=materialization.task_dir,
        jobs_dir=jobs_dir,
        job_name=job_name,
    )
    runtime_home = run_dir / "home"
    runtime_home.mkdir(mode=0o700)
    environment = build_subprocess_environment(
        os.environ if host_environment is None else host_environment,
        runtime_home=runtime_home,
    )
    _write_run_manifest(
        run_dir / "run.json",
        {
            "schema_version": 1,
            "scenario_id": normalized_scenario,
            "harbor_version": "0.20.0",
            "python_version": "3.12",
            "task_schema_version": "1.3",
            "materialization_digest": materialization.digest,
            "command": command,
        },
    )

    completed = runner(
        command,
        cwd=repository,
        env=environment,
        check=False,
    )
    returncode = getattr(completed, "returncode", None)
    if returncode != 0:
        raise EvalRunError(f"Harbor exited unsuccessfully with status {returncode!r}")

    parsed = parse_job(jobs_dir / job_name)
    if parsed.scenario_id != normalized_scenario:
        raise EvalRunError(f"expected scenario {normalized_scenario}, got {parsed.scenario_id}")
    if parsed.mission < minimum_mission:
        raise EvalRunError(
            f"mission grade {parsed.mission} is below configured minimum {minimum_mission}"
        )
    return parsed


def parse_job(
    job_dir: Path,
    *,
    grades_schema_path: Path = DEFAULT_GRADES_SCHEMA,
) -> ParsedJob:
    """Validate a single-trial Harbor job and return its two separate grades."""

    job_dir = job_dir.resolve()
    job_result_path = job_dir / "result.json"
    result = _load_json_object(job_result_path)
    if result.get("n_total_trials") != 1:
        raise EvalRunError("job must declare exactly one trial")

    stats = result.get("stats")
    if not isinstance(stats, dict):
        raise EvalRunError("job result is missing stats")
    expected_counts = {
        "n_completed_trials": 1,
        "n_errored_trials": 0,
        "n_running_trials": 0,
        "n_pending_trials": 0,
        "n_cancelled_trials": 0,
    }
    if any(stats.get(name) != count for name, count in expected_counts.items()):
        raise EvalRunError("job stats do not describe one cleanly completed trial")

    trials = result.get("trial_results")
    if not isinstance(trials, list) or len(trials) != 1:
        raise EvalRunError("job result must retain exactly one trial result")
    trial = trials[0]
    if not isinstance(trial, dict) or trial.get("exception_info") is not None:
        raise EvalRunError("trial contains an exception or malformed result")
    verifier_result = trial.get("verifier_result")
    if not isinstance(verifier_result, dict):
        raise EvalRunError("trial is missing verifier_result")
    result_rewards = _rewards(
        verifier_result.get("rewards"),
        source="result.json verifier rewards",
    )

    grades_paths = list(job_dir.glob("*/verifier/grades.json"))
    reward_paths = list(job_dir.glob("*/verifier/reward.json"))
    if len(grades_paths) != 1 or len(reward_paths) != 1:
        raise EvalRunError("job must retain exactly one verifier grades.json and reward.json")
    grades_path = grades_paths[0]
    reward_path = reward_paths[0]
    if grades_path.parent != reward_path.parent:
        raise EvalRunError("grades.json and reward.json must come from the same trial")
    trial_name = trial.get("trial_name")
    if (
        not isinstance(trial_name, str)
        or not trial_name
        or grades_path.parent.parent.name != trial_name
    ):
        raise EvalRunError("retained verifier directory does not match trial_name")

    file_rewards = _rewards(_load_json_object(reward_path), source="reward.json")
    if file_rewards != result_rewards:
        raise EvalRunError("Harbor result rewards do not match retained reward.json")

    grades = _load_json_object(grades_path)
    grades_schema = _load_json_object(grades_schema_path)
    _validate_schema(grades, grades_schema)
    if grades.get("run_status") != "valid":
        raise EvalRunError("grades.json run_status is not valid")
    scenario_id = grades.get("scenario_id")
    if not isinstance(scenario_id, str) or not scenario_id:
        raise EvalRunError("grades.json scenario_id must be a non-empty string")
    governance = grades.get("governance")
    mission = grades.get("mission")
    if not isinstance(governance, dict) or not isinstance(mission, dict):
        raise EvalRunError("grades.json must retain governance and mission objects")
    grade_rewards = {
        "governance": _numeric_score(governance.get("score"), name="grades.json governance.score"),
        "mission": _numeric_score(mission.get("score"), name="grades.json mission.score"),
        "reward": file_rewards["reward"],
    }
    if grade_rewards != file_rewards:
        raise EvalRunError("grades.json scores do not match reward.json")
    if file_rewards["governance"] != 1:
        raise EvalRunError("governance grade failed")

    return ParsedJob(
        scenario_id=scenario_id,
        governance=file_rewards["governance"],
        mission=file_rewards["mission"],
        reward=file_rewards["reward"],
        job_result_path=job_result_path,
        reward_path=reward_path,
        grades_path=grades_path,
    )


def _score_summary(parsed: ParsedJob) -> dict[str, object]:
    return {
        "scenario_id": parsed.scenario_id,
        "run_status": "valid",
        "governance": parsed.governance,
        "mission": parsed.mission,
        "reward": parsed.reward,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run pinned aOS Harbor evaluations or validate retained output."
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--all",
        action="store_true",
        help="Run every configured automatable scenario.",
    )
    selection.add_argument(
        "--scenario",
        action="append",
        choices=sorted(_SCENARIO_TASKS),
        help="Run one configured scenario; may be repeated.",
    )
    selection.add_argument(
        "--parse-job",
        type=Path,
        help="Validate one already-retained Harbor job without running Docker.",
    )
    arguments = parser.parse_args(argv)

    try:
        if arguments.parse_job is not None:
            print(json.dumps(_score_summary(parse_job(arguments.parse_job)), sort_keys=True))
            return 0

        repository = Path(__file__).resolve().parent.parent
        scenario_ids = sorted(_SCENARIO_TASKS) if arguments.all else arguments.scenario
        for scenario_id in scenario_ids:
            parsed = run_scenario(
                repository=repository,
                scenario_id=scenario_id,
            )
            print(json.dumps(_score_summary(parsed), sort_keys=True))
    except EvalRunError as exc:
        print(f"eval failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
