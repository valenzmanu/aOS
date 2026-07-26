from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from modules.journal import InvalidOperation, read_events
from modules.lease import read_authority_epoch
from modules.mission_controls import (
    authorize_mission,
    contract_evaluation_errors,
    dispatch_mission,
    finish_mission,
    initialize_controls,
    intake_mission,
    mission_status,
    propose_contract,
    replace_worker,
    start_mission,
)
from modules.mission_controls import (
    main as mission_main,
)

LIAISON_SESSION_ID = "LS-TEST-LANDING"
RAW_REQUEST = (
    "Create a landing page for aOS. Search the best aesthetics, colors, etc for this. "
    "Your time budget is 15 minutes. Choose the best harness/model to accomplish this.\n"
)

EVALUABLE_CONTRACT = """# Proposed contract

## Outcome

Ship a static aOS landing page under `aos-site/`.

## Acceptance

- [ ] `aos-site/index.html` opens without a build step
- [ ] Page visibly names `aOS`
- [ ] Page includes one short product sentence and one CTA

## Constraints

- Worker wall-clock budget: 15 minutes
- Liaison does not implement the page
- No secrets or private state paths in the site

## Out of scope

- Multi-page marketing site
- Backend or auth

## Open questions

None.
"""


def _write_decision(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "decision": "authorized",
                "decision_source_hash": "a" * 64,
                "human_principal": "human:learning",
                "schema_version": 1,
            }
        )
    )


def _worker_receipt(
    *,
    authority_epoch_id: str = "AE-M-LANDING-0001",
    worker_session_id: str = "S-M-LANDING-0001",
) -> dict[str, object]:
    return {
        "attempt_id": "A-M-LANDING-0001",
        "authority_epoch_id": authority_epoch_id,
        "mission_id": "M-LANDING",
        "process_id": "P-M-LANDING-0001",
        "schema_version": 1,
        "status": "completed",
        "work_order_id": "WO-M-LANDING-0001",
        "worker_session_id": worker_session_id,
    }


def _dispatch_fixture(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "state"
    missions = tmp_path / "missions"
    request = tmp_path / "human-request.md"
    contract = tmp_path / "contract.md"
    decision = tmp_path / "authorize.json"
    work_order = tmp_path / "work-order.md"
    request.write_text(RAW_REQUEST)
    contract.write_text(EVALUABLE_CONTRACT)
    work_order.write_text("# Work order\n\nEvolve `tyr-site/` in place.\n")
    _write_decision(decision)

    initialize_controls(root, "OP-DF-INIT")
    intake_mission(
        root,
        missions,
        mission_id="M-LANDING",
        human_request_path=request,
        operation_id="OP-DF-INTAKE",
        liaison_session_id=LIAISON_SESSION_ID,
    )
    propose_contract(
        root,
        missions,
        "M-LANDING",
        contract_path=contract,
        operation_id="OP-DF-PROPOSE",
        expected_revision=1,
        liaison_session_id=LIAISON_SESSION_ID,
    )
    authorize_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-AUTHORIZE",
        expected_revision=1,
        liaison_session_id=LIAISON_SESSION_ID,
        decision_path=decision,
    )
    dispatch_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-DISPATCH",
        expected_revision=2,
        liaison_session_id=LIAISON_SESSION_ID,
        work_order_path=work_order,
        backend="t3-code",
        model="gpt-5.6-sol",
        workspace="repo-root/tyr-site",
        worker_token="worker-capability-material-" + ("x" * 32),
        budget_seconds=900,
    )
    return root, missions


def test_contract_evaluation_requires_sections_and_resolved_questions() -> None:
    assert "missing_section:Outcome" in contract_evaluation_errors("# nope\n")
    assert contract_evaluation_errors(EVALUABLE_CONTRACT) == ()
    unresolved = EVALUABLE_CONTRACT.replace("None.", "- [ ] Which domain hosts the page?")
    assert "open_questions_unresolved" in contract_evaluation_errors(unresolved)


def test_intake_preserves_raw_request_and_stays_draft(tmp_path: Path) -> None:
    root = tmp_path / "state"
    missions = tmp_path / "missions"
    request = tmp_path / "human-request.md"
    request.write_text(RAW_REQUEST)

    initialize_controls(root, "OP-DF-INIT")
    outcome = intake_mission(
        root,
        missions,
        mission_id="M-LANDING",
        human_request_path=request,
        operation_id="OP-DF-INTAKE",
        liaison_session_id=LIAISON_SESSION_ID,
    )

    assert outcome.status == "completed"
    preserved = (missions / "M-LANDING" / "intake" / "human-request.md").read_text()
    assert preserved == RAW_REQUEST
    status = mission_status(root, missions, "M-LANDING")
    assert status["state"] == "draft"
    assert status["revision"] == 1


def test_propose_rejects_unevaluable_contract(tmp_path: Path) -> None:
    root = tmp_path / "state"
    missions = tmp_path / "missions"
    request = tmp_path / "human-request.md"
    bad = tmp_path / "bad.md"
    request.write_text(RAW_REQUEST)
    bad.write_text("# vague\n\nMake it pretty.\n")

    initialize_controls(root, "OP-DF-INIT")
    intake_mission(
        root,
        missions,
        mission_id="M-LANDING",
        human_request_path=request,
        operation_id="OP-DF-INTAKE",
        liaison_session_id=LIAISON_SESSION_ID,
    )
    outcome = propose_contract(
        root,
        missions,
        "M-LANDING",
        contract_path=bad,
        operation_id="OP-DF-PROPOSE-BAD",
        expected_revision=1,
        liaison_session_id=LIAISON_SESSION_ID,
    )

    assert outcome.status == "rejected"
    assert outcome.reason == "contract_not_evaluable"
    assert not (missions / "M-LANDING" / "contract" / "proposed.md").exists()


def test_open_intake_questions_block_propose_and_authorize(tmp_path: Path) -> None:
    root = tmp_path / "state"
    missions = tmp_path / "missions"
    request = tmp_path / "human-request.md"
    contract = tmp_path / "contract.md"
    decision = tmp_path / "authorize.json"
    request.write_text(RAW_REQUEST)
    contract.write_text(EVALUABLE_CONTRACT)
    _write_decision(decision)

    initialize_controls(root, "OP-DF-INIT")
    intake_mission(
        root,
        missions,
        mission_id="M-LANDING",
        human_request_path=request,
        operation_id="OP-DF-INTAKE",
        liaison_session_id=LIAISON_SESSION_ID,
    )
    questions = missions / "M-LANDING" / "intake" / "open-questions.md"
    questions.write_text("# Open questions\n\n- [ ] Single page or multi-page?\n")

    proposed = propose_contract(
        root,
        missions,
        "M-LANDING",
        contract_path=contract,
        operation_id="OP-DF-PROPOSE",
        expected_revision=1,
        liaison_session_id=LIAISON_SESSION_ID,
    )
    assert proposed.status == "rejected"
    assert proposed.reason == "contract_not_evaluable"

    questions.write_text("# Open questions\n\n- [x] Single page\n")
    proposed_ok = propose_contract(
        root,
        missions,
        "M-LANDING",
        contract_path=contract,
        operation_id="OP-DF-PROPOSE-2",
        expected_revision=1,
        liaison_session_id=LIAISON_SESSION_ID,
    )
    assert proposed_ok.status == "completed"

    authorized = authorize_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-AUTHORIZE",
        expected_revision=1,
        liaison_session_id=LIAISON_SESSION_ID,
        decision_path=decision,
    )
    assert authorized.status == "completed"
    status = mission_status(root, missions, "M-LANDING")
    assert status["state"] == "authorized"
    assert (missions / "M-LANDING" / "contract" / "authorized.md").is_file()


def test_dispatch_creates_a_scoped_worker_membrane_without_leaking_token(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    missions = tmp_path / "missions"
    request = tmp_path / "human-request.md"
    contract = tmp_path / "contract.md"
    decision = tmp_path / "authorize.json"
    work_order = tmp_path / "work-order.md"
    request.write_text(RAW_REQUEST)
    contract.write_text(EVALUABLE_CONTRACT)
    work_order.write_text("# Work order\n\nEvolve `tyr-site/` in place.\n")
    _write_decision(decision)

    initialize_controls(root, "OP-DF-INIT")
    intake_mission(
        root,
        missions,
        mission_id="M-LANDING",
        human_request_path=request,
        operation_id="OP-DF-INTAKE",
        liaison_session_id=LIAISON_SESSION_ID,
    )
    propose_contract(
        root,
        missions,
        "M-LANDING",
        contract_path=contract,
        operation_id="OP-DF-PROPOSE",
        expected_revision=1,
        liaison_session_id=LIAISON_SESSION_ID,
    )
    authorize_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-AUTHORIZE",
        expected_revision=1,
        liaison_session_id=LIAISON_SESSION_ID,
        decision_path=decision,
    )

    worker_token = "worker-capability-material-" + ("x" * 32)
    outcome = dispatch_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-DISPATCH",
        expected_revision=2,
        liaison_session_id=LIAISON_SESSION_ID,
        work_order_path=work_order,
        backend="t3-code",
        model="gpt-5.6-sol",
        workspace="repo-root/tyr-site",
        worker_token=worker_token,
        budget_seconds=900,
    )

    assert outcome.status == "completed"
    status = mission_status(root, missions, "M-LANDING")
    assert status["state"] == "dispatched"
    assert status["revision"] == 3
    assert status["attempt_id"] == "A-M-LANDING-0001"
    assert status["process_id"] == "P-M-LANDING-0001"
    assert status["worker_session_id"] == "S-M-LANDING-0001"

    attempt_dir = missions / "M-LANDING" / "attempts" / "0001"
    admitted_work_order = (
        attempt_dir
        / "processes"
        / "P-M-LANDING-0001"
        / "work-orders"
        / "0001.md"
    ).read_text()
    assert admitted_work_order == work_order.read_text()
    route = (attempt_dir / "route.md").read_text()
    assert "Harness: t3-code" in route
    assert "Model: gpt-5.6-sol" in route
    assert "Budget-seconds: 900" in route

    epoch = read_authority_epoch(root, "AE-M-LANDING-0001")
    assert epoch is not None
    assert epoch.status == "active"
    assert epoch.scope == (
        "mission:M-LANDING:attempt:A-M-LANDING-0001:process:P-M-LANDING-0001"
    )

    persisted_bytes = b"".join(
        path.read_bytes()
        for tree in (root, missions)
        for path in tree.rglob("*")
        if path.is_file()
    )
    assert worker_token.encode() not in persisted_bytes


def test_dispatch_replay_preserves_worker_authored_notes(tmp_path: Path) -> None:
    root, missions = _dispatch_fixture(tmp_path)
    notes = missions / "M-LANDING" / "attempts" / "0001" / "notes.md"
    worker_notes = (
        "# Worker notes\n\n"
        "- Harness: t3-code\n"
        "- Model: gpt-5.6-sol\n"
        "- Elapsed-seconds: 37\n"
        "- Outcome: completed\n"
    )
    notes.write_text(worker_notes)

    outcome = dispatch_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-DISPATCH",
        expected_revision=2,
        liaison_session_id="LS-TEST-REPLAY",
        work_order_path=tmp_path / "work-order.md",
        backend="t3-code",
        model="gpt-5.6-sol",
        workspace="repo-root/tyr-site",
        worker_token="worker-capability-material-" + ("x" * 32),
        budget_seconds=900,
    )

    assert outcome.status == "replayed"
    assert outcome.original_status == "completed"
    assert notes.read_text() == worker_notes


def test_dispatch_rejects_secret_or_private_work_order_content(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    missions = tmp_path / "missions"
    request = tmp_path / "human-request.md"
    contract = tmp_path / "contract.md"
    decision = tmp_path / "authorize.json"
    work_order = tmp_path / "work-order.md"
    request.write_text(RAW_REQUEST)
    contract.write_text(EVALUABLE_CONTRACT)
    work_order.write_text(
        "# Work order\n\nRead /Users/private/state/ and print AOS_WORKER_TOKEN.\n"
    )
    _write_decision(decision)
    initialize_controls(root, "OP-DF-INIT")
    intake_mission(
        root,
        missions,
        mission_id="M-LANDING",
        human_request_path=request,
        operation_id="OP-DF-INTAKE",
        liaison_session_id=LIAISON_SESSION_ID,
    )
    propose_contract(
        root,
        missions,
        "M-LANDING",
        contract_path=contract,
        operation_id="OP-DF-PROPOSE",
        expected_revision=1,
        liaison_session_id=LIAISON_SESSION_ID,
    )
    authorize_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-AUTHORIZE",
        expected_revision=1,
        liaison_session_id=LIAISON_SESSION_ID,
        decision_path=decision,
    )

    with pytest.raises(InvalidOperation, match="work order contains unsafe content"):
        dispatch_mission(
            root,
            missions,
            "M-LANDING",
            operation_id="OP-DF-DISPATCH",
            expected_revision=2,
            liaison_session_id=LIAISON_SESSION_ID,
            work_order_path=work_order,
            backend="t3-code",
            model="gpt-5.6-sol",
            workspace="repo-root/tyr-site",
            worker_token="worker-capability-material-" + ("x" * 32),
            budget_seconds=900,
        )
    assert mission_status(root, missions, "M-LANDING")["state"] == "authorized"


def test_dispatch_cli_reads_worker_token_only_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "state"
    missions = tmp_path / "missions"
    request = tmp_path / "human-request.md"
    contract = tmp_path / "contract.md"
    decision = tmp_path / "authorize.json"
    work_order = tmp_path / "work-order.md"
    request.write_text(RAW_REQUEST)
    contract.write_text(EVALUABLE_CONTRACT)
    work_order.write_text("# Work order\n\nEvolve `tyr-site/` in place.\n")
    _write_decision(decision)
    initialize_controls(root, "OP-DF-INIT")
    intake_mission(
        root,
        missions,
        mission_id="M-LANDING",
        human_request_path=request,
        operation_id="OP-DF-INTAKE",
        liaison_session_id=LIAISON_SESSION_ID,
    )
    propose_contract(
        root,
        missions,
        "M-LANDING",
        contract_path=contract,
        operation_id="OP-DF-PROPOSE",
        expected_revision=1,
        liaison_session_id=LIAISON_SESSION_ID,
    )
    authorize_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-AUTHORIZE",
        expected_revision=1,
        liaison_session_id=LIAISON_SESSION_ID,
        decision_path=decision,
    )
    token = "cli-worker-capability-" + ("z" * 32)
    monkeypatch.setenv("AOS_STATE_ROOT", str(root))
    monkeypatch.setenv("AOS_MISSIONS_ROOT", str(missions))
    monkeypatch.setenv("AOS_LIAISON_SESSION_ID", LIAISON_SESSION_ID)
    monkeypatch.setenv("AOS_WORKER_TOKEN", token)

    exit_code = mission_main(
        [
            "dispatch",
            "M-LANDING",
            "--work-order",
            str(work_order),
            "--backend",
            "t3-code",
            "--model",
            "gpt-5.6-sol",
            "--workspace",
            "repo-root/tyr-site",
            "--budget-seconds",
            "900",
            "--op",
            "OP-DF-DISPATCH",
            "--expect",
            "2",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert json.loads(output)["state"] == "dispatched"
    assert token not in output


def test_start_marks_the_dispatched_membrane_as_running(tmp_path: Path) -> None:
    root, missions = _dispatch_fixture(tmp_path)

    outcome = start_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-START",
        expected_revision=3,
        liaison_session_id=LIAISON_SESSION_ID,
    )

    assert outcome.status == "completed"
    status = mission_status(root, missions, "M-LANDING")
    assert status["state"] == "implementing"
    assert status["revision"] == 4
    assert status["process_state"] == "running"
    assert status["worker_session_state"] == "running"
    session = (
        missions
        / "M-LANDING"
        / "attempts"
        / "0001"
        / "processes"
        / "P-M-LANDING-0001"
        / "sessions"
        / "S-M-LANDING-0001"
        / "session.json"
    )
    assert json.loads(session.read_text())["state"] == "running"


def test_finish_accepts_a_bound_worker_receipt_and_closes_the_attempt(
    tmp_path: Path,
) -> None:
    root, missions = _dispatch_fixture(tmp_path)
    start_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-START",
        expected_revision=3,
        liaison_session_id=LIAISON_SESSION_ID,
    )
    attempt_dir = missions / "M-LANDING" / "attempts" / "0001"
    (attempt_dir / "notes.md").write_text(
        "# Worker notes\n\n"
        "- Harness: t3-code\n"
        "- Model: gpt-5.6-sol\n"
        "- Elapsed-seconds: 420\n"
        "- Outcome: completed\n"
    )
    receipt = attempt_dir / "receipt.json"
    receipt.write_text(json.dumps(_worker_receipt()))

    outcome = finish_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-FINISH",
        expected_revision=4,
        liaison_session_id=LIAISON_SESSION_ID,
        receipt_path=receipt,
    )

    assert outcome.status == "completed"
    status = mission_status(root, missions, "M-LANDING")
    assert status["state"] == "verifying"
    assert status["revision"] == 5
    assert status["attempt_state"] == "completed"
    assert status["process_state"] == "completed"
    assert status["worker_session_state"] == "completed"
    epoch = read_authority_epoch(root, "AE-M-LANDING-0001")
    assert epoch is not None
    assert epoch.status == "released"


def test_finish_rejects_receipt_without_epoch_and_work_order_binding(
    tmp_path: Path,
) -> None:
    root, missions = _dispatch_fixture(tmp_path)
    start_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-START",
        expected_revision=3,
        liaison_session_id=LIAISON_SESSION_ID,
    )
    attempt_dir = missions / "M-LANDING" / "attempts" / "0001"
    (attempt_dir / "notes.md").write_text(
        "# Worker notes\n\n"
        "- Harness: t3-code\n"
        "- Model: gpt-5.6-sol\n"
        "- Elapsed-seconds: 98\n"
        "- Outcome: completed\n"
    )
    unbound = _worker_receipt()
    del unbound["authority_epoch_id"]
    del unbound["work_order_id"]
    receipt = attempt_dir / "receipt.json"
    receipt.write_text(json.dumps(unbound))

    with pytest.raises(
        InvalidOperation,
        match="receipt does not match the dispatched membrane",
    ):
        finish_mission(
            root,
            missions,
            "M-LANDING",
            operation_id="OP-DF-FINISH-UNBOUND",
            expected_revision=4,
            liaison_session_id=LIAISON_SESSION_ID,
            receipt_path=receipt,
        )


def test_finish_rejects_receipt_relayed_after_worker_authority_expiry(
    tmp_path: Path,
) -> None:
    root, missions = _dispatch_fixture(tmp_path)
    start_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-START",
        expected_revision=3,
        liaison_session_id=LIAISON_SESSION_ID,
    )
    attempt_dir = missions / "M-LANDING" / "attempts" / "0001"
    (attempt_dir / "notes.md").write_text(
        "# Worker notes\n\n"
        "- Harness: t3-code\n"
        "- Model: gpt-5.6-sol\n"
        "- Elapsed-seconds: 98\n"
        "- Outcome: completed\n"
    )
    receipt = attempt_dir / "receipt.json"
    receipt.write_text(json.dumps(_worker_receipt()))
    epoch = read_authority_epoch(root, "AE-M-LANDING-0001")
    assert epoch is not None
    expired_at = datetime.fromisoformat(epoch.expires_at) + timedelta(seconds=1)

    outcome = finish_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-FINISH-LATE",
        expected_revision=4,
        liaison_session_id=LIAISON_SESSION_ID,
        receipt_path=receipt,
        clock=lambda: expired_at,
    )

    assert outcome.status == "rejected"
    assert outcome.reason == "worker_authority_expired"
    assert mission_status(root, missions, "M-LANDING")["state"] == "implementing"
    still_open = read_authority_epoch(root, "AE-M-LANDING-0001")
    assert still_open is not None
    assert still_open.status == "active"


def test_replace_worker_recovers_the_same_work_without_overlapping_authority(
    tmp_path: Path,
) -> None:
    root, missions = _dispatch_fixture(tmp_path)
    first_token = "worker-capability-material-" + ("x" * 32)
    replacement_token = "replacement-capability-material-" + ("y" * 32)
    recovery_session_id = "LS-TEST-RECOVERY"
    partial_artifact = tmp_path / "shared-worktree" / "artifact.txt"
    partial_artifact.parent.mkdir()
    partial_artifact.write_text("preserved partial work\n")
    start_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-START",
        expected_revision=3,
        liaison_session_id=LIAISON_SESSION_ID,
    )
    expanded = replace_worker(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-REPLACE-EXPANDED",
        expected_revision=4,
        liaison_session_id=recovery_session_id,
        worker_token=replacement_token,
        budget_seconds=901,
    )
    assert expanded.status == "rejected"
    assert expanded.reason == "replacement_budget_expanded"

    outcome = replace_worker(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-REPLACE",
        expected_revision=4,
        liaison_session_id=recovery_session_id,
        worker_token=replacement_token,
        budget_seconds=600,
    )

    assert outcome.status == "completed"
    assert partial_artifact.read_text() == "preserved partial work\n"
    replay = replace_worker(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-REPLACE",
        expected_revision=4,
        liaison_session_id="LS-TEST-RECOVERY-REPLAY",
        worker_token=replacement_token,
        budget_seconds=600,
    )
    assert replay.status == "replayed"
    assert replay.original_status == "completed"
    stale = replace_worker(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-REPLACE-STALE",
        expected_revision=4,
        liaison_session_id=recovery_session_id,
        worker_token="stale-replacement-capability-" + ("z" * 32),
        budget_seconds=500,
    )
    assert stale.status == "rejected"
    assert stale.reason == "worker_membrane_not_replaceable"
    status = mission_status(root, missions, "M-LANDING")
    assert status["state"] == "implementing"
    assert status["revision"] == 4
    assert status["attempt_id"] == "A-M-LANDING-0001"
    assert status["process_id"] == "P-M-LANDING-0001"
    assert status["process_state"] == "created"
    assert status["worker_session_id"] == "S-M-LANDING-0002"
    assert status["worker_session_state"] == "created"

    events = read_events(root)
    process_reset = [
        (
            event.kind,
            event.payload.get("from_state"),
            event.payload.get("to_state"),
        )
        for event in events
        if event.aggregate_id == "P-M-LANDING-0001"
        and event.kind == "process.transitioned"
    ]
    assert ("process.transitioned", "running", "created") in process_reset
    revoked = next(
        event
        for event in events
        if event.kind == "authority.revoked"
        and event.aggregate_id == "AE-M-LANDING-0001"
    )
    claimed = next(
        event
        for event in events
        if event.kind == "authority.claimed"
        and event.aggregate_id == "AE-M-LANDING-0002"
    )
    assert revoked.event_id < claimed.event_id
    assert revoked.envelope is not None
    assert claimed.envelope is not None
    assert revoked.envelope["operation_id"] == "OP-DF-REPLACE"
    assert claimed.envelope["operation_id"] == "OP-DF-REPLACE"

    restart = start_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-RESTART",
        expected_revision=4,
        liaison_session_id=recovery_session_id,
    )
    assert restart.status == "completed"
    restart_replay = start_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-RESTART",
        expected_revision=4,
        liaison_session_id="LS-TEST-RECOVERY-REPLAY",
    )
    assert restart_replay.status == "replayed"
    status = mission_status(root, missions, "M-LANDING")
    assert status["state"] == "implementing"
    assert status["revision"] == 4
    assert status["process_state"] == "running"
    assert status["worker_session_id"] == "S-M-LANDING-0002"
    assert status["worker_session_state"] == "running"

    attempt_dir = missions / "M-LANDING" / "attempts" / "0001"
    (attempt_dir / "notes.md").write_text(
        "# Worker notes\n\n"
        "- Harness: t3-code\n"
        "- Model: gpt-5.6-sol\n"
        "- Elapsed-seconds: 120\n"
        "- Outcome: completed\n"
    )
    receipt = attempt_dir / "receipt.json"
    receipt.write_text(json.dumps(_worker_receipt()))
    with pytest.raises(InvalidOperation, match="receipt does not match"):
        finish_mission(
            root,
            missions,
            "M-LANDING",
            operation_id="OP-DF-FINISH-STALE",
            expected_revision=4,
            liaison_session_id=recovery_session_id,
            receipt_path=receipt,
        )

    receipt.write_text(
        json.dumps(
            _worker_receipt(
                authority_epoch_id="AE-M-LANDING-0002",
                worker_session_id="S-M-LANDING-0002",
            )
        )
    )
    finish = finish_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-FINISH-REPLACEMENT",
        expected_revision=4,
        liaison_session_id=recovery_session_id,
        receipt_path=receipt,
    )

    assert finish.status == "completed"
    finish_replay = finish_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-FINISH-REPLACEMENT",
        expected_revision=4,
        liaison_session_id="LS-TEST-RECOVERY-REPLAY",
        receipt_path=receipt,
    )
    assert finish_replay.status == "replayed"
    status = mission_status(root, missions, "M-LANDING")
    assert status["state"] == "verifying"
    assert status["revision"] == 5
    assert status["attempt_state"] == "completed"
    assert status["process_state"] == "completed"
    assert status["worker_session_state"] == "completed"
    with sqlite3.connect(root / "aos.db") as db:
        assert db.execute(
            "SELECT COUNT(*) FROM attempts WHERE mission_id='M-LANDING'"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM processes WHERE mission_id='M-LANDING'"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM work_orders WHERE mission_id='M-LANDING'"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT state FROM worker_sessions WHERE id='S-M-LANDING-0001'"
        ).fetchone()[0] == "orphaned"
    first_epoch = read_authority_epoch(root, "AE-M-LANDING-0001")
    replacement_epoch = read_authority_epoch(root, "AE-M-LANDING-0002")
    assert first_epoch is not None
    assert replacement_epoch is not None
    assert first_epoch.status == "revoked"
    assert replacement_epoch.status == "released"
    for directory in (root, missions):
        for path in directory.rglob("*"):
            if path.is_file():
                contents = path.read_bytes()
                assert first_token.encode() not in contents
                assert replacement_token.encode() not in contents
