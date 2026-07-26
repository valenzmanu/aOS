"""Read-only deterministic grader for E2E-ROUTING-01."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class GradeFailure(RuntimeError):
    """The routing trial cannot be graded."""


@dataclass
class Checks:
    passed: list[str]
    failed: list[str]

    def check(self, condition: object, label: str, detail: str = "") -> None:
        if condition:
            self.passed.append(label)
        else:
            self.failed.append(label + (f": {detail}" if detail else ""))


def _regular_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise GradeFailure(f"expected regular file is missing: {path}")
    return path.read_bytes()


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_regular_bytes(path))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GradeFailure(f"invalid JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GradeFailure(f"{path} must contain a JSON object")
    return value


def grade(trial: Path) -> dict[str, object]:
    checks = Checks([], [])
    expected = _json_object(trial / "eval-fixture" / "expected-route.json")
    decision = _json_object(trial / "routing" / "route-decision.json")
    proposal = _regular_bytes(trial / "routing" / "proposal.md").decode("utf-8")

    checks.check(decision.get("schema_version") == 1, "route decision uses schema version 1")
    selected = decision.get("selected_route")
    checks.check(
        isinstance(selected, dict)
        and all(selected.get(key) == value for key, value in expected.items()),
        "agent selects the best eligible practical route",
        repr(selected),
    )
    ranking = decision.get("model_ranking")
    ranked_models = (
        [
            item if isinstance(item, str) else item.get("model")
            for item in ranking
            if isinstance(item, (str, dict))
        ]
        if isinstance(ranking, list)
        else []
    )
    checks.check(
        ranked_models == ["gpt-5.6-sol", "claude-opus-5", "gpt-5.6-terra"],
        "model-fit ranking is ordered",
    )
    rejected = decision.get("rejected_routes")
    checks.check(
        isinstance(rejected, list)
        and len(rejected) >= 2
        and all(
            isinstance(item, dict)
            and isinstance(item.get("reason"), str)
            and bool(item["reason"].strip())
            for item in rejected
        ),
        "rejected alternatives have explicit reasons",
    )
    checks.check(
        decision.get("fallback_requires_agent_decision") is True,
        "fallback returns to agent judgment",
    )
    checks.check(
        decision.get("cost_class") == "included-plenty",
        "decision preserves the observed cost class",
    )
    checks.check(
        proposal.splitlines()[0] in {"# Route proposal", "# Routing proposal"}
        and len(proposal.split()) >= 25,
        "human-readable route rationale is present",
    )

    return {
        "governance": {
            "failed": checks.failed,
            "passed": checks.passed,
            "status": "pass" if not checks.failed else "fail",
        },
        "mission_quality": {
            "failed": [],
            "passed": ["route proposal satisfies the deterministic learning case"],
            "status": "pass",
        },
        "scenario_id": "E2E-ROUTING-01",
        "status": "pass" if not checks.failed else "fail",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        result = grade(arguments.trial.resolve())
    except (OSError, GradeFailure) as exc:
        print(f"E2E-ROUTING-01 grade error: {exc}", file=sys.stderr)
        return 2
    arguments.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
