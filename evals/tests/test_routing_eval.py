from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from evals.routing.run import run_trial


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _valid_trial(trial: Path) -> None:
    expected_route = {
        "billing_channel": "chatgpt-plan",
        "execution_mode": "headless",
        "harness": "codex",
        "isolation": "workspace-write",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
    }
    _write_json(trial / "eval-fixture" / "expected-route.json", expected_route)
    _write_json(
        trial / "routing" / "route-decision.json",
        {
            "cost_class": "included-plenty",
            "fallback_requires_agent_decision": True,
            "model_ranking": [
                {"model": "gpt-5.6-sol", "rank": 1},
                {"model": "claude-opus-5", "rank": 2},
                {"model": "gpt-5.6-terra", "rank": 3},
            ],
            "rejected_routes": [
                {
                    "harness": "cursor",
                    "model": "gpt-5.6-sol",
                    "reason": "higher expected cost to an accepted outcome",
                },
                {
                    "harness": "cursor",
                    "model": "claude-opus-5",
                    "reason": "privacy boundary excludes this offer",
                },
            ],
            "schema_version": 1,
            "selected_route": expected_route,
        },
    )
    proposal = trial / "routing" / "proposal.md"
    proposal.parent.mkdir(parents=True, exist_ok=True)
    proposal.write_text(
        "# Route proposal\n\n"
        "GPT-5.6 Sol is the strongest task fit. Codex is selected because both Sol offers "
        "clear the quality threshold while Codex has plentiful included capacity. The Cursor "
        "Claude offer is outside the authorized privacy boundary. Any fallback requires a new "
        "liaison decision.\n"
    )


def _run_grader(repository: Path, trial: Path) -> subprocess.CompletedProcess[str]:
    output = trial / "grade.json"
    return subprocess.run(
        [
            sys.executable,
            str(repository / "evals" / "routing" / "grade.py"),
            "--trial",
            str(trial),
            "--output",
            str(output),
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )


def test_routing_grader_accepts_the_best_eligible_agent_authored_route(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).parents[2]
    trial = tmp_path / "trial"
    _valid_trial(trial)

    completed = _run_grader(repository, trial)

    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert json.loads((trial / "grade.json").read_text())["status"] == "pass"


def test_routing_grader_rejects_a_different_practical_route(tmp_path: Path) -> None:
    repository = Path(__file__).parents[2]
    trial = tmp_path / "trial"
    _valid_trial(trial)
    decision_path = trial / "routing" / "route-decision.json"
    decision = json.loads(decision_path.read_text())
    decision["selected_route"]["harness"] = "cursor"
    decision["selected_route"]["billing_channel"] = "cursor-subscription"
    _write_json(decision_path, decision)

    completed = _run_grader(repository, trial)

    assert completed.returncode == 1
    result = json.loads((trial / "grade.json").read_text())
    assert result["governance"]["status"] == "fail"


def test_routing_grader_accepts_equivalent_explanatory_shapes(tmp_path: Path) -> None:
    repository = Path(__file__).parents[2]
    trial = tmp_path / "trial"
    _valid_trial(trial)
    decision_path = trial / "routing" / "route-decision.json"
    decision = json.loads(decision_path.read_text())
    decision["model_ranking"] = [
        "gpt-5.6-sol",
        "claude-opus-5",
        "gpt-5.6-terra",
    ]
    decision["selected_route"]["status"] = "healthy"
    _write_json(decision_path, decision)
    proposal_path = trial / "routing" / "proposal.md"
    proposal_path.write_text(
        "# Routing proposal\n\n"
        "GPT-5.6 Sol ranks first for the architectural task. Both Sol offers clear the "
        "quality threshold, and Codex wins because its included capacity lowers expected "
        "cost to acceptance. Cursor Claude violates the privacy boundary. Every fallback "
        "requires a fresh liaison decision rather than an automatic provider switch.\n"
    )

    completed = _run_grader(repository, trial)

    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_routing_runner_launches_one_agent_with_a_credential_free_environment(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).parents[2]
    observed: dict[str, object] = {}

    def fake_agent_runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        trial = Path(str(kwargs["cwd"]))
        observed["environment"] = kwargs["env"]
        expected = json.loads(
            (trial / "eval-fixture" / "expected-route.json").read_text()
        )
        _write_json(
            trial / "routing" / "route-decision.json",
            {
                "cost_class": "included-plenty",
                "fallback_requires_agent_decision": True,
                "model_ranking": [
                    {"model": "gpt-5.6-sol", "rank": 1},
                    {"model": "claude-opus-5", "rank": 2},
                    {"model": "gpt-5.6-terra", "rank": 3},
                ],
                "rejected_routes": [
                    {
                        "harness": "cursor",
                        "model": "gpt-5.6-sol",
                        "reason": "higher expected cost to an accepted outcome",
                    },
                    {
                        "harness": "cursor",
                        "model": "claude-opus-5",
                        "reason": "privacy boundary excludes this offer",
                    },
                ],
                "schema_version": 1,
                "selected_route": expected,
            },
        )
        proposal = trial / "routing" / "proposal.md"
        proposal.parent.mkdir(parents=True, exist_ok=True)
        proposal.write_text(
            "# Route proposal\n\n"
            "GPT-5.6 Sol ranks first. The Codex offer clears the quality threshold and "
            "uses plentiful included capacity. Cursor Sol has a higher expected cost and "
            "Cursor Claude violates the privacy boundary. Fallback requires liaison judgment.\n"
        )
        final_path = Path(command[command.index("--output-last-message") + 1])
        final_path.write_text("E2E-ROUTING-01 selected codex/gpt-5.6-sol.\n")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=b'{"type":"turn.completed"}\n',
            stderr=b"",
        )

    trial, returncode = run_trial(
        repository,
        runs_root=tmp_path / "runs",
        host_environment={
            "HOME": str(tmp_path / "home"),
            "PATH": "/usr/bin",
            "OPENAI_API_KEY": "must-not-cross",
            "CURSOR_API_KEY": "must-not-cross",
        },
        agent_runner=fake_agent_runner,
        codex_executable="/usr/bin/codex",
        codex_version="codex-cli test",
    )

    assert returncode == 0
    assert json.loads((trial / "grade.json").read_text())["status"] == "pass"
    environment = observed["environment"]
    assert isinstance(environment, dict)
    assert "OPENAI_API_KEY" not in environment
    assert "CURSOR_API_KEY" not in environment
