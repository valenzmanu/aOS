from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

TASK_DIR = Path(__file__).parents[1] / "tasks" / "hbr-01"
EVALS_DIR = TASK_DIR.parents[1]


def test_hbr01_grader_writes_separate_numeric_grades(tmp_path: Path) -> None:
    submission = tmp_path / "submission.json"
    submission.write_text(
        json.dumps(
            {
                "message": "harbor-oracle-compatible",
                "schema_version": 1,
            }
        )
    )
    verifier_output = tmp_path / "verifier"

    result = subprocess.run(
        [
            sys.executable,
            str(TASK_DIR / "tests" / "grader.py"),
            "--submission",
            str(submission),
            "--output-dir",
            str(verifier_output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads((verifier_output / "reward.json").read_text()) == {
        "governance": 1,
        "mission": 1,
        "reward": 1,
    }
    grades = json.loads((verifier_output / "grades.json").read_text())
    assert grades["run_status"] == "valid"
    assert grades["governance"]["score"] == 1
    assert grades["mission"]["score"] == 1


def test_hbr01_declares_schema_13_and_a_separate_offline_verifier() -> None:
    config = tomllib.loads((TASK_DIR / "task.toml").read_text())

    assert config["schema_version"] == "1.3"
    assert config["task"]["name"] == "aos/hbr-01"
    assert config["artifacts"] == ["/app/submission.json"]
    assert config["agent"]["network_mode"] == "no-network"
    assert config["environment"]["network_mode"] == "no-network"
    assert config["verifier"]["environment_mode"] == "separate"
    assert config["verifier"]["network_mode"] == "no-network"
    assert config["verifier"]["environment"]["network_mode"] == "no-network"

    verifier_dockerfile = (TASK_DIR / "tests" / "Dockerfile").read_text()
    assert "FROM python:3.12-slim" in verifier_dockerfile
    assert "COPY test.sh grader.py /tests/" in verifier_dockerfile
    assert (TASK_DIR / "tests" / "test.sh").exists()
    assert (TASK_DIR / "solution" / "solve.sh").exists()


def test_eval_environment_pins_harbor_and_python() -> None:
    config = tomllib.loads((EVALS_DIR / "pyproject.toml").read_text())

    assert config["project"]["requires-python"] == "==3.12.*"
    assert config["project"]["dependencies"] == ["harbor==0.20.0"]
    assert (EVALS_DIR / ".python-version").read_text() == "3.12\n"


def test_hbr01_missing_declared_artifact_is_infrastructure_invalid(
    tmp_path: Path,
) -> None:
    verifier_output = tmp_path / "verifier"

    result = subprocess.run(
        [
            sys.executable,
            str(TASK_DIR / "tests" / "grader.py"),
            "--submission",
            str(tmp_path / "missing.json"),
            "--output-dir",
            str(verifier_output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads((verifier_output / "reward.json").read_text()) == {
        "governance": 0,
        "mission": 0,
        "reward": 0,
    }
    grades = json.loads((verifier_output / "grades.json").read_text())
    assert grades["run_status"] == "infrastructure_invalid"


def test_hbr01_wrong_fixture_keeps_governance_and_mission_separate(
    tmp_path: Path,
) -> None:
    submission = tmp_path / "submission.json"
    submission.write_text('{"message":"wrong","schema_version":1}\n')
    verifier_output = tmp_path / "verifier"

    result = subprocess.run(
        [
            sys.executable,
            str(TASK_DIR / "tests" / "grader.py"),
            "--submission",
            str(submission),
            "--output-dir",
            str(verifier_output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads((verifier_output / "reward.json").read_text()) == {
        "governance": 1,
        "mission": 0,
        "reward": 0,
    }
    grades = json.loads((verifier_output / "grades.json").read_text())
    assert grades["run_status"] == "valid"
    assert grades["governance"]["score"] == 1
    assert grades["mission"]["score"] == 0
