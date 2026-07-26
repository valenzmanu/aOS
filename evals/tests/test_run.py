from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evals.run import (
    EvalRunError,
    build_harbor_command,
    build_subprocess_environment,
    main,
    parse_job,
    run_scenario,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def _valid_grades() -> dict[str, object]:
    return {
        "schema_version": 1,
        "scenario_id": "HBR-01",
        "run_status": "valid",
        "grading_method": "deterministic_fixture",
        "governance": {
            "score": 1,
            "checks": [
                {
                    "id": "artifact-transfer",
                    "passed": True,
                    "details": "Declared artifact reached the separate verifier.",
                }
            ],
        },
        "mission": {
            "score": 1,
            "dimensions": [
                {
                    "id": "fixture-completion",
                    "score": 1,
                    "details": "Oracle produced the exact fixture.",
                }
            ],
        },
        "rubric_version": None,
        "judge_version": None,
        "reasoning_artifacts": [],
    }


def _write_valid_job(job_dir: Path) -> None:
    rewards = {"governance": 1, "mission": 1, "reward": 1}
    _write_json(
        job_dir / "result.json",
        {
            "n_total_trials": 1,
            "stats": {
                "n_completed_trials": 1,
                "n_errored_trials": 0,
                "n_running_trials": 0,
                "n_pending_trials": 0,
                "n_cancelled_trials": 0,
            },
            "trial_results": [
                {
                    "trial_name": "hbr-01__oracle__1",
                    "exception_info": None,
                    "verifier_result": {"rewards": rewards},
                }
            ],
        },
    )
    verifier = job_dir / "hbr-01__oracle__1" / "verifier"
    _write_json(verifier / "reward.json", rewards)
    _write_json(verifier / "grades.json", _valid_grades())


def test_parse_job_accepts_matching_two_grade_outputs(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    _write_valid_job(job_dir)

    parsed = parse_job(job_dir)

    assert parsed.scenario_id == "HBR-01"
    assert parsed.governance == 1
    assert parsed.mission == 1
    assert parsed.reward == 1


def test_parse_job_rejects_governance_failure_even_when_harbor_completed(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job"
    _write_valid_job(job_dir)
    rewards = {"governance": 0, "mission": 1, "reward": 0}
    result_path = job_dir / "result.json"
    result = json.loads(result_path.read_text())
    result["trial_results"][0]["verifier_result"]["rewards"] = rewards
    _write_json(result_path, result)
    verifier = job_dir / "hbr-01__oracle__1" / "verifier"
    _write_json(verifier / "reward.json", rewards)
    grades = _valid_grades()
    grades["governance"]["score"] = 0
    _write_json(verifier / "grades.json", grades)

    with pytest.raises(EvalRunError, match="governance grade failed"):
        parse_job(job_dir)


def test_parse_job_rejects_grades_that_do_not_match_the_canonical_schema(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job"
    _write_valid_job(job_dir)
    grades_path = job_dir / "hbr-01__oracle__1" / "verifier" / "grades.json"
    grades = json.loads(grades_path.read_text())
    del grades["grading_method"]
    _write_json(grades_path, grades)

    with pytest.raises(EvalRunError, match="grades schema"):
        parse_job(job_dir)


def test_harbor_command_uses_locked_python_312_oracle_environment(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    task_dir = tmp_path / "materialized" / "task"
    jobs_dir = tmp_path / "run" / "jobs"

    command = build_harbor_command(
        repository=repository,
        task_dir=task_dir,
        jobs_dir=jobs_dir,
        job_name="hbr-01",
    )

    assert command == [
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
        "hbr-01",
        "--n-attempts",
        "1",
        "--n-concurrent",
        "1",
        "--yes",
        "--quiet",
    ]


def test_harbor_subprocess_environment_excludes_credentials(tmp_path: Path) -> None:
    environment = build_subprocess_environment(
        {
            "PATH": "/usr/bin",
            "TMPDIR": "/tmp",
            "DOCKER_HOST": "unix:///tmp/docker.sock",
            "UV_CACHE_DIR": "/tmp/uv-cache",
            "OPENAI_API_KEY": "must-not-cross",
            "CODEX_API_KEY": "must-not-cross",
            "AOS_LIAISON_TOKEN": "must-not-cross",
        },
        runtime_home=tmp_path / "home",
    )

    assert environment == {
        "DOCKER_HOST": "unix:///tmp/docker.sock",
        "HARBOR_TELEMETRY": "off",
        "HOME": str(tmp_path / "home"),
        "PATH": "/usr/bin",
        "PYTHONUTF8": "1",
        "TMPDIR": "/tmp",
        "UV_CACHE_DIR": "/tmp/uv-cache",
    }


def test_run_scenario_materializes_invokes_and_parses_harbor(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).parents[2]
    observed: dict[str, object] = {}

    def fake_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        jobs_dir = Path(command[command.index("--jobs-dir") + 1])
        job_name = command[command.index("--job-name") + 1]
        _write_valid_job(jobs_dir / job_name)
        return subprocess.CompletedProcess(command, 0)

    parsed = run_scenario(
        repository=repository,
        scenario_id="HBR-01",
        runs_root=tmp_path / "runs",
        host_environment={
            "PATH": "/usr/bin",
            "OPENAI_API_KEY": "must-not-cross",
        },
        runner=fake_runner,
    )

    assert parsed.scenario_id == "HBR-01"
    assert "--locked" in observed["command"]
    assert "OPENAI_API_KEY" not in observed["environment"]
    assert parsed.job_result_path.is_relative_to(tmp_path / "runs")


def test_parse_job_rejects_verifier_files_from_a_different_trial(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job"
    _write_valid_job(job_dir)
    original = job_dir / "hbr-01__oracle__1"
    original.rename(job_dir / "unrelated-trial")

    with pytest.raises(EvalRunError, match="trial_name"):
        parse_job(job_dir)


def test_parse_only_cli_reports_canonical_scores(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_dir = tmp_path / "job"
    _write_valid_job(job_dir)

    assert main(["--parse-job", str(job_dir)]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "governance": 1.0,
        "mission": 1.0,
        "reward": 1.0,
        "run_status": "valid",
        "scenario_id": "HBR-01",
    }
