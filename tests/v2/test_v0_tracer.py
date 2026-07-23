from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from modules.v0_tracer import (
    V0_EXPECTED_RESULT_DIGEST,
    _outcome_json,
    _parser,
    _result_is_rejected,
    _transition_runtime,
    authorize_mission,
    claim_v0,
    decide_mission,
    dispatch_mission,
    initialize_v0,
    mission_status,
    seed_mission,
    verify_mission,
)

EXPECTED_RESULT = b"TYR v0 end-to-end mission completed.\n"
TOKEN = "v0-test-liaison-capability-" + ("x" * 40)


def _write_decision(path: Path, decision: str, source_hash: str) -> None:
    path.write_text(
        json.dumps(
            {
                "decision": decision,
                "decision_source_hash": source_hash,
                "human_principal": "human:v0",
                "schema_version": 1,
            }
        )
    )


def _prepare_verified_mission(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "state"
    missions_root = tmp_path / "missions"
    objective = tmp_path / "objective.md"
    authorize = tmp_path / "authorize.json"
    approve = tmp_path / "approve.json"
    objective.write_text("Complete the deterministic v0 mission.\n")
    _write_decision(authorize, "authorized", "a" * 64)
    _write_decision(approve, "approved", "b" * 64)

    initialize_v0(root, "OP-V0-INIT")
    assert (
        seed_mission(
            root,
            missions_root,
            mission_id="M-V0",
            objective_path=objective,
            operation_id="OP-V0-SETUP",
        ).status
        == "completed"
    )
    assert (
        claim_v0(
            root,
            operation_id="OP-V0-CLAIM",
            epoch_id="AE-V0",
            token=TOKEN,
        )["status"]
        == "completed"
    )
    assert (
        authorize_mission(
            root,
            missions_root,
            "M-V0",
            operation_id="OP-V0-AUTHORIZE",
            expected_revision=1,
            epoch_id="AE-V0",
            token=TOKEN,
            decision_path=authorize,
        ).status
        == "completed"
    )
    assert (
        dispatch_mission(
            root,
            missions_root,
            "M-V0",
            operation_id="OP-V0-DISPATCH",
            expected_revision=2,
            epoch_id="AE-V0",
            token=TOKEN,
        ).status
        == "completed"
    )
    assert (
        _transition_runtime(
            root,
            missions_root,
            "M-V0",
            operation_id="OP-V0-START",
            expected_revision=3,
            epoch_id="AE-V0",
            token=TOKEN,
            phase="start",
        ).status
        == "completed"
    )

    output_dir = missions_root / "M-V0" / "attempts" / "0001" / "worker-output"
    output_dir.mkdir(parents=True)
    result = output_dir / "result.txt"
    result.write_bytes(EXPECTED_RESULT)
    receipt = output_dir / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "attempt_id": "A-V0",
                "mission_id": "M-V0",
                "process_id": "P-V0",
                "result_path": "result.txt",
                "result_sha256": hashlib.sha256(EXPECTED_RESULT).hexdigest(),
                "schema_version": 1,
                "status": "completed",
                "worker_session_id": "S-V0",
            }
        )
    )
    assert (
        _transition_runtime(
            root,
            missions_root,
            "M-V0",
            operation_id="OP-V0-FINISH",
            expected_revision=4,
            epoch_id="AE-V0",
            token=TOKEN,
            phase="finish",
            receipt_path=receipt,
        ).status
        == "completed"
    )
    assert (
        verify_mission(
            root,
            missions_root,
            "M-V0",
            operation_id="OP-V0-VERIFY",
            expected_revision=5,
            epoch_id="AE-V0",
            token=TOKEN,
        ).status
        == "completed"
    )
    return root, missions_root, approve, result


def test_verifier_is_pinned_and_has_no_caller_oracle() -> None:
    assert hashlib.sha256(EXPECTED_RESULT).hexdigest() == V0_EXPECTED_RESULT_DIGEST
    with pytest.raises(SystemExit):
        _parser().parse_args(
            [
                "verify",
                "M-V0",
                "--op",
                "OP-V0-VERIFY",
                "--expect",
                "5",
                "--expected",
                "caller-controlled.txt",
            ]
        )


def test_approval_rejects_mutated_output_and_replay_stays_rejected(tmp_path: Path) -> None:
    root, missions_root, approve, result = _prepare_verified_mission(tmp_path)
    result.write_text("mutated after verification\n")

    rejected = decide_mission(
        root,
        missions_root,
        "M-V0",
        operation_id="OP-V0-DECIDE-MUTATED",
        expected_revision=6,
        epoch_id="AE-V0",
        token=TOKEN,
        decision_path=approve,
    )
    assert rejected.status == "rejected"
    assert rejected.reason == "evidence_stale"

    replayed = decide_mission(
        root,
        missions_root,
        "M-V0",
        operation_id="OP-V0-DECIDE-MUTATED",
        expected_revision=6,
        epoch_id="AE-V0",
        token=TOKEN,
        decision_path=approve,
    )
    replay_json = _outcome_json(replayed)
    assert replay_json["status"] == "replayed"
    assert replay_json["original_status"] == "rejected"
    assert _result_is_rejected(replay_json)
    assert mission_status(root, "M-V0")["state"] == "awaiting_approval"

    result.write_bytes(EXPECTED_RESULT)
    approved = decide_mission(
        root,
        missions_root,
        "M-V0",
        operation_id="OP-V0-DECIDE-CLEAN",
        expected_revision=6,
        epoch_id="AE-V0",
        token=TOKEN,
        decision_path=approve,
    )
    assert approved.status == "completed"
    assert mission_status(root, "M-V0")["state"] == "delivered"
