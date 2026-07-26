from __future__ import annotations

import json
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
    start_mission,
    verify_mission,
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


def _worker_receipt() -> dict[str, object]:
    return {
        "attempt_id": "A-M-LANDING-0001",
        "authority_epoch_id": "AE-M-LANDING-0001",
        "mission_id": "M-LANDING",
        "process_id": "P-M-LANDING-0001",
        "schema_version": 1,
        "status": "completed",
        "work_order_id": "WO-M-LANDING-0001",
        "worker_session_id": "S-M-LANDING-0001",
    }


def _write_passing_site(site: Path) -> None:
    site.mkdir()
    (site / "index.html").write_text(
        """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>aOS</title></head>
<body>
  <main>
    <h1 data-primary-brand>aOS</h1>
    <p data-product-sentence>
      aOS is a sparse agent OS that keeps mission state durable and delivery evidence-bound.
    </p>
    <a data-primary-cta href="https://github.com/valenzmanu/aOS">View on GitHub</a>
  </main>
</body>
</html>
"""
    )


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


def _finished_fixture(tmp_path: Path) -> tuple[Path, Path]:
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
    finish_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-FINISH",
        expected_revision=4,
        liaison_session_id=LIAISON_SESSION_ID,
        receipt_path=receipt,
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


def test_verify_records_passing_acceptance_evidence(tmp_path: Path) -> None:
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
    finish_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-FINISH",
        expected_revision=4,
        liaison_session_id=LIAISON_SESSION_ID,
        receipt_path=receipt,
    )
    site = tmp_path / "tyr-site"
    site.mkdir()
    (site / "index.html").write_text(
        """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>aOS</title></head>
<body>
  <main>
    <h1 data-primary-brand>aOS</h1>
    <p data-product-sentence>
      aOS is a sparse agent OS that keeps mission state durable and delivery evidence-bound.
    </p>
    <a data-primary-cta href="https://github.com/valenzmanu/aOS">View on GitHub</a>
  </main>
</body>
</html>
"""
    )

    outcome = verify_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-VERIFY",
        expected_revision=5,
        liaison_session_id=LIAISON_SESSION_ID,
        site_root=site,
    )

    assert outcome.status == "completed"
    status = mission_status(root, missions, "M-LANDING")
    assert status["state"] == "awaiting_approval"
    assert status["revision"] == 6
    assert status["evidence_id"] == "EV-M-LANDING-0001"
    assert isinstance(status["evidence_digest"], str)
    report_path = (
        attempt_dir
        / "processes"
        / "P-M-LANDING-0001"
        / "evidence"
        / "EV-M-LANDING-0001"
        / "verification.json"
    )
    report = json.loads(report_path.read_text())
    assert report["passed"] is True
    assert [check["name"] for check in report["checks"]] == [
        "single_static_page",
        "primary_brand_aos",
        "product_sentence",
        "github_primary_cta",
        "privacy_scan",
        "worker_attempt_notes",
    ]
    assert all(check["passed"] for check in report["checks"])
    evidence_event = next(
        event for event in read_events(root) if event.kind == "evidence.recorded"
    )
    assert evidence_event.payload["verification_report"] == report


def test_verify_replay_uses_recorded_report_after_site_changes(
    tmp_path: Path,
) -> None:
    root, missions = _finished_fixture(tmp_path)
    site = tmp_path / "tyr-site"
    _write_passing_site(site)
    operation_id = "OP-DF-VERIFY-REPLAY"

    initial = verify_mission(
        root,
        missions,
        "M-LANDING",
        operation_id=operation_id,
        expected_revision=5,
        liaison_session_id=LIAISON_SESSION_ID,
        site_root=site,
    )
    assert initial.status == "completed"
    evidence_dir = (
        missions
        / "M-LANDING"
        / "attempts"
        / "0001"
        / "processes"
        / "P-M-LANDING-0001"
        / "evidence"
        / "EV-M-LANDING-0001"
    )
    expected_report = json.loads((evidence_dir / "verification.json").read_text())
    event_ids = [event.event_id for event in read_events(root)]

    (site / "index.html").write_text("<h1>changed after verification</h1>\n")
    (evidence_dir / "verification.json").unlink()
    (evidence_dir / "evidence.json").unlink()

    replay = verify_mission(
        root,
        missions,
        "M-LANDING",
        operation_id=operation_id,
        expected_revision=5,
        liaison_session_id=LIAISON_SESSION_ID,
        site_root=site,
    )

    assert replay.status == "replayed"
    assert replay.original_status == "completed"
    assert json.loads((evidence_dir / "verification.json").read_text()) == expected_report
    assert [event.event_id for event in read_events(root)] == event_ids


def test_verify_records_failed_evidence_and_blocks_approval(tmp_path: Path) -> None:
    root, missions = _finished_fixture(tmp_path)
    site = tmp_path / "tyr-site"
    site.mkdir()
    (site / "index.html").write_text(
        """<!doctype html>
<html lang="en">
<body>
  <main>
    <h1 data-primary-brand>TYR</h1>
    <p data-product-sentence>TYR is a generic landing page with stale branding.</p>
    <a data-primary-cta href="https://github.com/valenzmanu/aOS">View on GitHub</a>
  </main>
</body>
</html>
"""
    )

    outcome = verify_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-VERIFY-FAIL",
        expected_revision=5,
        liaison_session_id=LIAISON_SESSION_ID,
        site_root=site,
    )

    assert outcome.status == "completed"
    status = mission_status(root, missions, "M-LANDING")
    assert status["state"] == "failed"
    assert status["revision"] == 6
    assert status["evidence_id"] == "EV-M-LANDING-0001"
    report_path = (
        missions
        / "M-LANDING"
        / "attempts"
        / "0001"
        / "processes"
        / "P-M-LANDING-0001"
        / "evidence"
        / "EV-M-LANDING-0001"
        / "verification.json"
    )
    report = json.loads(report_path.read_text())
    assert report["passed"] is False
    failed_checks = {
        check["name"] for check in report["checks"] if not check["passed"]
    }
    assert "primary_brand_aos" in failed_checks


def test_failed_verifier_result_is_completed_operation_not_gate_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, missions = _finished_fixture(tmp_path)
    site = tmp_path / "tyr-site"
    site.mkdir()
    (site / "index.html").write_text("<h1>incomplete</h1>\n")
    monkeypatch.setenv("AOS_STATE_ROOT", str(root))
    monkeypatch.setenv("AOS_MISSIONS_ROOT", str(missions))
    monkeypatch.setenv("AOS_LIAISON_SESSION_ID", LIAISON_SESSION_ID)

    exit_code = mission_main(
        [
            "verify",
            "M-LANDING",
            "--site-root",
            str(site),
            "--op",
            "OP-DF-VERIFY-FAIL",
            "--expect",
            "5",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["status"] == "completed"
    assert result["state"] == "failed"
    assert result["rejection_event_id"] is None


def test_verify_rejects_secret_keyed_site_metadata(tmp_path: Path) -> None:
    root, missions = _finished_fixture(tmp_path)
    site = tmp_path / "tyr-site"
    site.mkdir()
    (site / "index.html").write_text(
        """<!doctype html>
<html lang="en">
<body>
  <main>
    <h1 data-primary-brand>aOS</h1>
    <p data-product-sentence>
      aOS is a sparse agent OS that keeps mission state durable and delivery evidence-bound.
    </p>
    <a data-primary-cta href="https://github.com/valenzmanu/aOS">View on GitHub</a>
  </main>
</body>
</html>
"""
    )
    (site / ".deployment.json").write_text(
        json.dumps({"claim" + "Token": "placeholder-" + ("x" * 32)})
    )

    verify_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-VERIFY-SECRET",
        expected_revision=5,
        liaison_session_id=LIAISON_SESSION_ID,
        site_root=site,
    )

    status = mission_status(root, missions, "M-LANDING")
    assert status["state"] == "failed"
    report_path = (
        missions
        / "M-LANDING"
        / "attempts"
        / "0001"
        / "processes"
        / "P-M-LANDING-0001"
        / "evidence"
        / "EV-M-LANDING-0001"
        / "verification.json"
    )
    report = json.loads(report_path.read_text())
    privacy = next(
        check for check in report["checks"] if check["name"] == "privacy_scan"
    )
    assert privacy["passed"] is False


def test_verify_rejects_symlinks_without_recording_their_targets(
    tmp_path: Path,
) -> None:
    root, missions = _finished_fixture(tmp_path)
    site = tmp_path / "tyr-site"
    _write_passing_site(site)
    private_target = "/" + "Users" + "/private/" + "state/aos.db"
    (site / "legacy-state").symlink_to(private_target)

    verify_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-VERIFY-SYMLINK",
        expected_revision=5,
        liaison_session_id=LIAISON_SESSION_ID,
        site_root=site,
    )

    assert mission_status(root, missions, "M-LANDING")["state"] == "failed"
    evidence_event = next(
        event for event in read_events(root) if event.kind == "evidence.recorded"
    )
    serialized_event = json.dumps(evidence_event.payload, sort_keys=True)
    assert private_target not in serialized_event
    report = evidence_event.payload["verification_report"]
    assert isinstance(report, dict)
    privacy = next(
        check for check in report["checks"] if check["name"] == "privacy_scan"
    )
    assert privacy["passed"] is False
    assert privacy["details"]["symlink_count"] == 1


def test_verify_rejects_a_symlinked_site_root(tmp_path: Path) -> None:
    root, missions = _finished_fixture(tmp_path)
    passing_site = tmp_path / "passing-site"
    _write_passing_site(passing_site)
    site_link = tmp_path / "site-link"
    site_link.symlink_to(passing_site, target_is_directory=True)

    with pytest.raises(InvalidOperation, match="site root must not be a symlink"):
        verify_mission(
            root,
            missions,
            "M-LANDING",
            operation_id="OP-DF-VERIFY-ROOT-SYMLINK",
            expected_revision=5,
            liaison_session_id=LIAISON_SESSION_ID,
            site_root=site_link,
        )

    assert mission_status(root, missions, "M-LANDING")["state"] == "verifying"
    assert not any(event.kind == "evidence.recorded" for event in read_events(root))


def test_verify_report_never_records_attacker_controlled_site_values(
    tmp_path: Path,
) -> None:
    root, missions = _finished_fixture(tmp_path)
    site = tmp_path / "tyr-site"
    site.mkdir()
    private_value = "/" + "Users" + "/private/" + "state/aos.db"
    token_value = "Bearer " + ("x" * 28)
    (site / "index.html").write_text(
        f"""<!doctype html>
<html lang="en">
<body>
  <main>
    <h1 data-primary-brand>{private_value}</h1>
    <p data-product-sentence>
      aOS is a sparse agent OS that keeps mission state durable and delivery evidence-bound.
    </p>
    <a data-primary-cta href="{token_value}">View on GitHub</a>
  </main>
</body>
</html>
"""
    )

    verify_mission(
        root,
        missions,
        "M-LANDING",
        operation_id="OP-DF-VERIFY-SANITIZED",
        expected_revision=5,
        liaison_session_id=LIAISON_SESSION_ID,
        site_root=site,
    )

    evidence_event = next(
        event for event in read_events(root) if event.kind == "evidence.recorded"
    )
    serialized_event = json.dumps(evidence_event.payload, sort_keys=True)
    assert private_value not in serialized_event
    assert token_value not in serialized_event
