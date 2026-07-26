from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from evals.delegation.grade import grade
from evals.delegation.run import run_trial


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _write_valid_native_outputs(trial: Path) -> None:
    _write_json(trial / "work" / "synthesis.json", {
        "cross_lane": {
            "common_control": "validate-before-admit",
            "highest_severity": "critical",
            "total_impacted": 23,
        },
        "lane_findings": {
            "authority": {
                "impacted": 7,
                "owner": "runtime",
                "severity": "critical",
            },
            "evidence": {
                "impacted": 11,
                "owner": "verification",
                "severity": "high",
            },
            "privacy": {
                "impacted": 5,
                "owner": "distro",
                "severity": "high",
            },
        },
        "schema_version": 1,
    })
    _write_json(trial / "work" / "delegation-summary.json", {
        "child_count": 3,
        "depth": 1,
        "mode": "native-read-parallel",
        "parent_integrated": True,
        "schema_version": 1,
        "tasks": ["authority", "evidence", "privacy"],
    })


def test_delegation_grader_accepts_three_bounded_native_children_and_parent_synthesis(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).parents[2]
    trial = tmp_path / "trial"
    _write_json(
        trial / "work" / "synthesis.json",
        {
            "cross_lane": {
                "common_control": "validate-before-admit",
                "highest_severity": "critical",
                "total_impacted": 23,
            },
            "lane_findings": {
                "authority": {
                    "impacted": 7,
                    "owner": "runtime",
                    "severity": "critical",
                },
                "evidence": {
                    "impacted": 11,
                    "owner": "verification",
                    "severity": "high",
                },
                "privacy": {
                    "impacted": 5,
                    "owner": "distro",
                    "severity": "high",
                },
            },
            "schema_version": 1,
        },
    )
    _write_json(
        trial / "work" / "delegation-summary.json",
        {
            "child_count": 3,
            "depth": 1,
            "mode": "native-read-parallel",
            "parent_integrated": True,
            "schema_version": 1,
            "tasks": ["authority", "evidence", "privacy"],
        },
    )
    trajectory = trial / "trajectory.jsonl"
    trajectory.parent.mkdir(parents=True, exist_ok=True)
    trajectory.write_text(
        "\n".join(
            json.dumps(
                {
                    "arguments": {"task_name": task},
                    "tool": "spawn_agent",
                    "type": "collaboration_tool_call",
                }
            )
            for task in ("authority", "evidence", "privacy")
        )
        + "\n"
    )
    output = trial / "grade.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(repository / "evals" / "delegation" / "grade.py"),
            "--trial",
            str(trial),
            "--trajectory",
            str(trajectory),
            "--mode",
            "native-read-parallel",
            "--output",
            str(output),
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    result = json.loads(output.read_text())
    assert result["status"] == "pass"


def test_delegation_grader_accepts_denied_baseline_with_parent_only(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).parents[2]
    trial = tmp_path / "trial"
    _write_json(
        trial / "work" / "synthesis.json",
        {
            "cross_lane": {
                "common_control": "validate-before-admit",
                "highest_severity": "critical",
                "total_impacted": 23,
            },
            "lane_findings": {
                "authority": {
                    "impacted": 7,
                    "owner": "runtime",
                    "severity": "critical",
                },
                "evidence": {
                    "impacted": 11,
                    "owner": "verification",
                    "severity": "high",
                },
                "privacy": {
                    "impacted": 5,
                    "owner": "distro",
                    "severity": "high",
                },
            },
            "schema_version": 1,
        },
    )
    _write_json(
        trial / "work" / "delegation-summary.json",
        {
            "child_count": 0,
            "depth": 0,
            "mode": "denied",
            "parent_integrated": True,
            "schema_version": 1,
            "tasks": [],
        },
    )
    trajectory = trial / "trajectory.jsonl"
    trajectory.parent.mkdir(parents=True, exist_ok=True)
    trajectory.write_text("")
    output = trial / "grade.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(repository / "evals" / "delegation" / "grade.py"),
            "--trial",
            str(trial),
            "--trajectory",
            str(trajectory),
            "--mode",
            "denied",
            "--output",
            str(output),
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    result = json.loads(output.read_text())
    assert result["status"] == "pass"


def test_delegation_runner_enables_native_children_and_excludes_provider_keys(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).parents[2]
    observed: dict[str, object] = {}

    def fake_agent_runner(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        input: bytes,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        observed.update(command=command, cwd=cwd, env=env, input=input)
        _write_json(cwd / "work" / "synthesis.json", {
            "cross_lane": {
                "common_control": "validate-before-admit",
                "highest_severity": "critical",
                "total_impacted": 23,
            },
            "lane_findings": {
                "authority": {
                    "impacted": 7,
                    "owner": "runtime",
                    "severity": "critical",
                },
                "evidence": {
                    "impacted": 11,
                    "owner": "verification",
                    "severity": "high",
                },
                "privacy": {
                    "impacted": 5,
                    "owner": "distro",
                    "severity": "high",
                },
            },
            "schema_version": 1,
        })
        _write_json(cwd / "work" / "delegation-summary.json", {
            "child_count": 3,
            "depth": 1,
            "mode": "native-read-parallel",
            "parent_integrated": True,
            "schema_version": 1,
            "tasks": ["authority", "evidence", "privacy"],
        })
        trajectory = "\n".join(
            json.dumps({
                "arguments": {"task_name": task},
                "tool": "spawn_agent",
                "type": "collaboration_tool_call",
            })
            for task in ("authority", "evidence", "privacy")
        )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(trajectory + "\n").encode(),
            stderr=b"",
        )

    trial, returncode = run_trial(
        repository,
        mode="native-read-parallel",
        runs_root=tmp_path / "runs",
        host_environment={
            "CODEX_HOME": "/safe/codex-home",
            "HOME": "/safe/home",
            "OPENAI_API_KEY": "must-not-cross",
            "PATH": "/safe/bin",
        },
        agent_runner=fake_agent_runner,
        codex_executable="/safe/bin/codex",
        codex_version="codex-test",
    )

    assert returncode == 0
    assert "--enable" in observed["command"]
    assert "multi_agent" in observed["command"]
    assert b"native-read-parallel" in observed["input"]
    assert "OPENAI_API_KEY" not in observed["env"]
    metrics = json.loads((trial / "metrics.json").read_text())
    assert metrics["declared_child_count"] == 3
    assert metrics["observed_spawn_count"] == 3


def test_delegation_grader_accepts_codex_exec_collab_event_shape(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).parents[2]
    trial = tmp_path / "trial"
    _write_json(trial / "work" / "synthesis.json", {
        "cross_lane": {
            "common_control": "validate-before-admit",
            "highest_severity": "critical",
            "total_impacted": 23,
        },
        "lane_findings": {
            "authority": {
                "impacted": 7,
                "owner": "runtime",
                "severity": "critical",
            },
            "evidence": {
                "impacted": 11,
                "owner": "verification",
                "severity": "high",
            },
            "privacy": {
                "impacted": 5,
                "owner": "distro",
                "severity": "high",
            },
        },
        "schema_version": 1,
    })
    _write_json(trial / "work" / "delegation-summary.json", {
        "child_count": 3,
        "depth": 1,
        "mode": "native-read-parallel",
        "parent_integrated": True,
        "schema_version": 1,
        "tasks": ["authority", "evidence", "privacy"],
    })
    trajectory = trial / "trajectory.jsonl"
    trajectory.write_text(
        "\n".join(
            json.dumps({
                "type": "item.completed",
                "item": {
                    "agents_states": {"child-thread": {"status": "completed"}},
                    "id": f"item_{index}",
                    "prompt": f"Lane task: {task}. Read only its matching input.",
                    "receiver_thread_ids": [f"child-{index}"],
                    "sender_thread_id": "parent",
                    "status": "completed",
                    "tool": "spawn_agent",
                    "type": "collab_tool_call",
                },
            })
            for index, task in enumerate(("authority", "evidence", "privacy"))
        )
        + "\n"
    )
    output = trial / "grade.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(repository / "evals" / "delegation" / "grade.py"),
            "--trial",
            str(trial),
            "--trajectory",
            str(trajectory),
            "--mode",
            "native-read-parallel",
            "--output",
            str(output),
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_delegation_grader_marks_hidden_spawn_telemetry_as_indirect(
    tmp_path: Path,
) -> None:
    trial = tmp_path / "trial"
    _write_valid_native_outputs(trial)
    trajectory = trial / "trajectory.jsonl"
    trajectory.write_text(json.dumps({
        "type": "item.completed",
        "item": {
            "agents_states": {},
            "id": "item_1",
            "prompt": None,
            "receiver_thread_ids": [],
            "sender_thread_id": "parent",
            "status": "completed",
            "tool": "wait",
            "type": "collab_tool_call",
        },
    }) + "\n")

    result = grade(trial, trajectory, "native-read-parallel")

    assert result["status"] == "pass"
    assert result["telemetry"]["delegation_evidence"] == "indirect"
