#!/usr/bin/env python3
"""Grade the deterministic E2E-LAUNCH-01 cross-harness contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

EXPECTED_PAIRS = [
    ["codex", "codex"],
    ["codex", "cursor"],
    ["cursor", "codex"],
    ["cursor", "cursor"],
]
EXPECTED_LIFECYCLE = [
    "admitted_route",
    "adapter_launch",
    "normalized_observation",
]
EXPECTED_OBSERVATION_FIELDS = [
    "billing_channel",
    "billing_channel_source",
    "exit_status",
    "harness",
    "model",
    "reasoning_effort",
    "schema_version",
    "session_handle",
    "usage",
    "version",
]


def grade(matrix: Path) -> dict[str, object]:
    manifest = json.loads(matrix.read_text())
    cells = manifest.get("cells", [])
    observed_pairs = sorted(
        [cell.get("liaison_harness"), cell.get("worker_harness")]
        for cell in cells
        if isinstance(cell, dict)
    )
    checks = [
        {
            "name": "complete_cross_product",
            "passed": observed_pairs == EXPECTED_PAIRS,
        },
        {
            "name": "worker_selects_adapter",
            "passed": all(
                cell.get("adapter_skill") == f"launch-{cell.get('worker_harness')}-worker"
                for cell in cells
            ),
        },
        {
            "name": "shared_lifecycle",
            "passed": all(cell.get("lifecycle") == EXPECTED_LIFECYCLE for cell in cells),
        },
        {
            "name": "worker_route_owns_billing",
            "passed": all(cell.get("billing_owner") == "worker_route" for cell in cells),
        },
        {
            "name": "route_identity_is_observable",
            "passed": manifest.get("normalized_observation_fields")
            == EXPECTED_OBSERVATION_FIELDS,
        },
    ]
    return {
        "checks": checks,
        "observed_pairs": observed_pairs,
        "scenario_id": "E2E-LAUNCH-01-CONTRACT",
        "status": "pass" if all(check["passed"] for check in checks) else "fail",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    result = grade(args.matrix)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
