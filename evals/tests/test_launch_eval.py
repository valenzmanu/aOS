from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from evals.launch.run import run_live_trial


def test_launch_matrix_keeps_liaison_and_worker_harness_independent(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).parents[2]
    matrix = repository / "evals" / "launch" / "fixtures" / "matrix.json"
    output = tmp_path / "grade.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(repository / "evals" / "launch" / "grade.py"),
            "--matrix",
            str(matrix),
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
    assert result["observed_pairs"] == [
        ["codex", "codex"],
        ["codex", "cursor"],
        ["cursor", "codex"],
        ["cursor", "cursor"],
    ]


def test_live_launch_runner_preserves_admitted_route_identity(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).parents[2]
    observed_command: list[str] = []

    def fake_adapter_runner(
        command: list[str],
        *,
        cwd: Path,
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        observed_command.extend(command)
        workspace = Path(command[command.index("--workspace") + 1])
        output_dir = Path(command[command.index("--output-dir") + 1])
        output_dir.mkdir(parents=True)
        (workspace / "launch-result.json").write_text(
            '{"scenario_id":"E2E-LAUNCH-01","status":"complete"}\n'
        )
        (output_dir / "observation.json").write_text(json.dumps({
            "billing_channel": "cursor-subscription",
            "billing_channel_source": "admitted_route_argument",
            "exit_status": 0,
            "harness": "cursor",
            "model": "gpt-5.6-terra-medium",
            "reasoning_effort": "medium",
            "schema_version": 1,
            "session_handle": "cursor-test",
            "usage": {},
            "version": "cursor-test",
        }))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    trial, returncode = run_live_trial(
        repository,
        worker_harness="cursor",
        model="gpt-5.6-terra-medium",
        reasoning_effort="medium",
        billing_channel="cursor-subscription",
        runs_root=tmp_path / "runs",
        adapter_runner=fake_adapter_runner,
    )

    assert returncode == 0
    assert "launch-cursor-worker" in " ".join(observed_command)
    assert json.loads((trial / "grade.json").read_text())["status"] == "pass"
