#!/usr/bin/env python3
"""Grade bounded native-delegation behavior and the parent's synthesis."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

TASKS = ("authority", "evidence", "privacy")

EXPECTED_SYNTHESIS = {
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
}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _spawn_tasks(trajectory: Path) -> list[str]:
    tasks: list[str] = []
    for line in trajectory.read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("tool") == "spawn_agent":
            arguments = event.get("arguments", {})
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            task = arguments.get("task_name")
            if isinstance(task, str):
                tasks.append(task)
            continue
        item = event.get("item")
        if (
            event.get("type") != "item.completed"
            or not isinstance(item, dict)
            or item.get("type") != "collab_tool_call"
            or item.get("tool") != "spawn_agent"
            or item.get("status") != "completed"
        ):
            continue
        prompt = item.get("prompt")
        if not isinstance(prompt, str):
            continue
        matching_tasks = [
            task for task in TASKS if f"lane task: {task}." in prompt.lower()
        ]
        if len(matching_tasks) == 1:
            tasks.append(matching_tasks[0])
    return tasks


def _parent_read_lanes(trajectory: Path) -> list[str]:
    observed: list[str] = []
    for line in trajectory.read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        command = item.get("command")
        if not isinstance(command, str):
            continue
        observed.extend(
            task for task in TASKS if f"inputs/{task}.md" in command
        )
    return sorted(set(observed))


def _completed_collaboration_waits(trajectory: Path) -> int:
    count = 0
    for line in trajectory.read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "collab_tool_call"
            and item.get("tool") == "wait"
            and item.get("status") == "completed"
        ):
            count += 1
    return count


def _work_files(trial: Path) -> list[str]:
    work = trial / "work"
    if not work.is_dir():
        return []
    return sorted(
        path.relative_to(work).as_posix()
        for path in work.rglob("*")
        if path.is_file()
    )


def _check(name: str, passed: bool, detail: str) -> dict[str, object]:
    return {"detail": detail, "name": name, "passed": passed}


def grade(trial: Path, trajectory: Path, mode: str) -> dict[str, object]:
    synthesis = _read_json(trial / "work" / "synthesis.json")
    summary = _read_json(trial / "work" / "delegation-summary.json")
    spawn_tasks = _spawn_tasks(trajectory)
    expected_count = 3 if mode == "native-read-parallel" else 0
    expected_depth = 1 if expected_count else 0
    expected_tasks = list(TASKS) if expected_count else []
    parent_reads = _parent_read_lanes(trajectory)
    collaboration_waits = _completed_collaboration_waits(trajectory)
    work_files = _work_files(trial)
    direct_child_evidence = spawn_tasks == expected_tasks
    indirect_child_evidence = (
        expected_count == 3
        and not spawn_tasks
        and collaboration_waits > 0
        and not parent_reads
    )
    bounded_evidence = (
        direct_child_evidence or indirect_child_evidence
        if expected_count
        else not spawn_tasks and collaboration_waits == 0
    )
    evidence_level = (
        "direct"
        if direct_child_evidence and expected_count
        else "indirect"
        if indirect_child_evidence
        else "none"
    )

    governance = [
        _check(
            "bounded_native_delegation_evidence",
            bounded_evidence,
            (
                f"spawn tasks={spawn_tasks!r}; completed collaboration waits="
                f"{collaboration_waits}; evidence={evidence_level}"
            ),
        ),
        _check(
            "declared_delegation",
            summary
            == {
                "child_count": expected_count,
                "depth": expected_depth,
                "mode": mode,
                "parent_integrated": True,
                "schema_version": 1,
                "tasks": expected_tasks,
            },
            "delegation-summary.json must exactly describe the bounded execution",
        ),
        _check(
            "native_parent_context_isolation",
            not parent_reads if expected_count else True,
            f"parent directly read native child lanes: {parent_reads!r}",
        ),
        _check(
            "parent_owned_durable_output",
            work_files == ["delegation-summary.json", "synthesis.json"],
            f"durable work files: {work_files!r}",
        ),
    ]
    mission_quality = [
        _check(
            "parent_synthesis",
            synthesis == EXPECTED_SYNTHESIS,
            "synthesis.json must exactly integrate the three lane findings",
        )
    ]
    checks = governance + mission_quality
    return {
        "governance": governance,
        "mission_quality": mission_quality,
        "mode": mode,
        "scenario_id": "E2E-DELEGATION-01",
        "status": "pass" if all(check["passed"] for check in checks) else "fail",
        "telemetry": {
            "completed_collaboration_waits": collaboration_waits,
            "delegation_evidence": evidence_level,
            "observed_spawn_tasks": spawn_tasks,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trial", type=Path, required=True)
    parser.add_argument("--trajectory", type=Path, required=True)
    parser.add_argument(
        "--mode",
        choices=("denied", "native-read-parallel"),
        required=True,
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    result = grade(args.trial, args.trajectory, args.mode)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
