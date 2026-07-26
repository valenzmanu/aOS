#!/usr/bin/env python3
"""Deterministic HBR-01 verifier."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

EXPECTED_SUBMISSION = {
    "message": "harbor-oracle-compatible",
    "schema_version": 1,
}


def _write_json(path: Path, value: object) -> None:
    payload = (json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def grade(submission_path: Path, output_dir: Path) -> None:
    artifact_present = submission_path.is_file()
    parsed_submission: object = None
    parse_error: str | None = None
    if artifact_present:
        try:
            parsed_submission = json.loads(submission_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            parse_error = str(exc)

    fixture_complete = parsed_submission == EXPECTED_SUBMISSION
    governance = int(artifact_present)
    mission = int(fixture_complete)
    reward = mission if governance == 1 else 0
    run_status = "valid" if artifact_present else "infrastructure_invalid"

    reward_document = {
        "governance": governance,
        "mission": mission,
        "reward": reward,
    }
    grades_document = {
        "schema_version": 1,
        "scenario_id": "HBR-01",
        "run_status": run_status,
        "grading_method": "deterministic_fixture",
        "governance": {
            "score": governance,
            "checks": [
                {
                    "id": "artifact-transfer",
                    "passed": artifact_present,
                    "details": (
                        "Declared artifact reached the separate verifier."
                        if artifact_present
                        else "Declared artifact was absent from the separate verifier."
                    ),
                }
            ],
        },
        "mission": {
            "score": mission,
            "dimensions": [
                {
                    "id": "fixture-completion",
                    "score": mission,
                    "details": (
                        "Oracle produced the exact declared fixture."
                        if fixture_complete
                        else f"Submission did not match the fixture: {parse_error or 'wrong value'}."
                    ),
                }
            ],
        },
        "rubric_version": None,
        "judge_version": None,
        "reasoning_artifacts": [],
    }
    _write_json(output_dir / "reward.json", reward_document)
    _write_json(output_dir / "grades.json", grades_document)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args(argv)
    grade(arguments.submission, arguments.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
