"""EXPERIMENTAL permanent tracer for the first agent-driven aOS vertical slice.

Each command performs one requested operation.  This module deliberately does
not choose, sequence, retry, or supervise work: the agent remains the runtime.
The surface is intentionally narrow; later milestones deepen the constitutional
modules while E2E-00 remains the regression baseline for this architecture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Protocol

from modules.journal import (
    ExpectedRevision,
    GateRejection,
    InitializationRequest,
    InvalidOperation,
    Invoker,
    OperationOutcome,
    OperationRequest,
    _run_governed,
    content_digest,
    initialize_root,
    utc_now,
)
from modules.protocol import (
    AggregateType,
    AttemptState,
    ContractAuthorization,
    MissionState,
    ProtocolViolation,
    require_transition,
    validate_approval_decision,
    validate_evidence_record,
)
from modules.runtime_paths import resolve_runtime_paths

V0_LIAISON_SESSION_ID = "LS-V0"
V0_ATTEMPT_ID = "A-V0"
V0_PROCESS_ID = "P-V0"
V0_WORK_ORDER_ID = "W-V0"
V0_SESSION_ID = "S-V0"
V0_EVIDENCE_ID = "EV-V0"
V0_APPROVAL_ID = "AP-V0"
V0_CONTRACT_ID = "C-V0-0001"
V0_EXPECTED_RESULT_DIGEST = "d604a6bb4ad68c78075225846d894890baf44592956ed7e47b10681c47bcd06d"
V0_VERIFIER_SPEC = (
    f"E2E-00: require the SHA-256 of worker-output/result.txt to equal {V0_EXPECTED_RESULT_DIGEST}"
)
V0_VERIFIER_SPEC_DIGEST = hashlib.sha256(V0_VERIFIER_SPEC.encode()).hexdigest()


class _ValidationContext(Protocol):
    db: sqlite3.Connection
    occurred_at: datetime


class _ProjectionContext(Protocol):
    db: sqlite3.Connection
    occurred_at: str
    admission_event_id: int

    def append_event(
        self,
        kind: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, object],
        *,
        severity: str = "info",
        payload_schema_version: int = 1,
    ) -> object: ...


def _json_file(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidOperation(f"could not read JSON input {path}") from exc
    if not isinstance(value, dict):
        raise InvalidOperation(f"{path} must contain a JSON object")
    return value


def _human_decision(path: Path, expected_decision: str) -> tuple[str, str]:
    decision = _json_file(path)
    if set(decision) != {
        "decision",
        "decision_source_hash",
        "human_principal",
        "schema_version",
    }:
        raise InvalidOperation("human decision fixture has unexpected fields")
    principal = decision["human_principal"]
    decision_hash = decision["decision_source_hash"]
    if (
        decision["schema_version"] != 1
        or decision["decision"] != expected_decision
        or not isinstance(principal, str)
        or not principal
        or not isinstance(decision_hash, str)
        or len(decision_hash) != 64
        or any(character not in "0123456789abcdef" for character in decision_hash)
    ):
        raise InvalidOperation(f"human decision must explicitly be {expected_decision}")
    return principal, decision_hash


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _operation_was_completed(outcome: OperationOutcome) -> bool:
    return outcome.status == "completed" or (
        outcome.status == "replayed" and outcome.original_status == "completed"
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _write_text(path: Path, value: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(value)
    if executable:
        temporary.chmod(0o755)
    os.replace(temporary, path)


def _mission_revision(db: sqlite3.Connection, mission_id: str) -> tuple[int, int]:
    row = db.execute(
        """
        SELECT revision, source_event_id
        FROM aggregate_revisions
        WHERE aggregate_type='mission' AND aggregate_id=?
        """,
        (mission_id,),
    ).fetchone()
    if row is None:
        raise InvalidOperation(f"unknown mission: {mission_id}")
    return int(row[0]), int(row[1])


def mission_status(root: Path, mission_id: str) -> dict[str, object]:
    db_path = Path(root).expanduser().resolve() / "aos.db"
    with sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True) as db:
        mission = db.execute(
            """
            SELECT id, objective, state
            FROM missions
            WHERE id=?
            """,
            (mission_id,),
        ).fetchone()
        if mission is None:
            raise InvalidOperation(f"unknown mission: {mission_id}")
        revision, source_event_id = _mission_revision(db, mission_id)
        evidence = db.execute(
            """
            SELECT digest
            FROM evidence_records
            WHERE mission_id=?
            ORDER BY source_event_id DESC
            LIMIT 1
            """,
            (mission_id,),
        ).fetchone()
    next_actions = {
        "draft": ["authorize"],
        "authorized": ["dispatch"],
        "dispatched": ["start"],
        "implementing": ["run the work order, then finish"],
        "verifying": ["verify"],
        "awaiting_approval": ["decide"],
        "delivered": [],
    }
    return {
        "evidence_digest": None if evidence is None else str(evidence[0]),
        "evidence_id": None if evidence is None else V0_EVIDENCE_ID,
        "mission_id": str(mission[0]),
        "next": next_actions.get(str(mission[2]), []),
        "objective": str(mission[1]),
        "revision": revision,
        "source_event_id": source_event_id,
        "state": str(mission[2]),
        "status": "ok",
    }


def _render_state_tree(root: Path, missions_root: Path, mission_id: str) -> dict[str, object]:
    root = Path(root).expanduser().resolve()
    missions_root = Path(missions_root).expanduser().resolve()
    mission_dir = missions_root / mission_id
    status = mission_status(root, mission_id)
    _write_json(mission_dir / "status.json", status)

    with sqlite3.connect(root / "aos.db") as db:
        contract = db.execute(
            """
            SELECT content
            FROM contract_revisions
            WHERE mission_id=?
            ORDER BY revision DESC
            LIMIT 1
            """,
            (mission_id,),
        ).fetchone()
        attempt = db.execute(
            """
            SELECT id, number, state, worktree, branch, created_at, updated_at
            FROM attempts
            WHERE mission_id=?
            ORDER BY number DESC
            LIMIT 1
            """,
            (mission_id,),
        ).fetchone()
        process = db.execute(
            """
            SELECT id, mission_id, attempt, backend, model, state,
                   worktree, session_id, created_at, updated_at
            FROM processes
            WHERE mission_id=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (mission_id,),
        ).fetchone()
        work_order = db.execute(
            """
            SELECT content
            FROM work_orders
            WHERE mission_id=?
            ORDER BY revision DESC
            LIMIT 1
            """,
            (mission_id,),
        ).fetchone()
        session = db.execute(
            """
            SELECT s.id, s.process_id, s.sequence, s.state,
                   s.started_at, s.ended_at
            FROM worker_sessions AS s
            JOIN processes AS p ON p.id=s.process_id
            WHERE p.mission_id=?
            ORDER BY s.sequence DESC
            LIMIT 1
            """,
            (mission_id,),
        ).fetchone()
        evidence = db.execute(
            """
            SELECT digest, mission_revision, exit_status, output_artifacts, source_event_id
            FROM evidence_records
            WHERE mission_id=?
            ORDER BY source_event_id DESC
            LIMIT 1
            """,
            (mission_id,),
        ).fetchone()
        events = db.execute(
            """
            SELECT
                e.id, e.occurred_at, e.kind, e.severity,
                e.aggregate_type, e.aggregate_id, e.payload,
                envelope.operation_id, envelope.admission_id
            FROM events AS e
            LEFT JOIN event_envelopes AS envelope ON envelope.event_id=e.id
            ORDER BY e.id
            """
        ).fetchall()

    attempt_dir = mission_dir / "attempts" / "0001"
    process_dir = attempt_dir / "processes" / V0_PROCESS_ID
    if contract is not None:
        _write_text(mission_dir / "contract" / "revisions" / "0001.md", str(contract[0]))
    if attempt is not None:
        _write_json(
            attempt_dir / "attempt.json",
            {
                "attempt_id": str(attempt[0]),
                "branch": str(attempt[4]),
                "mission_id": mission_id,
                "number": int(attempt[1]),
                "state": str(attempt[2]),
                "updated_at": str(attempt[6]),
                "worktree": str(attempt[3]),
            },
        )
        _write_json(
            attempt_dir / "worktree.json",
            {
                "attempt_id": str(attempt[0]),
                "logical_path": str(attempt[3]),
                "mission_id": mission_id,
            },
        )
    if process is not None:
        _write_json(
            process_dir / "process.json",
            {
                "backend": str(process[3]),
                "mission_id": str(process[1]),
                "model": str(process[4]),
                "process_id": str(process[0]),
                "session_id": str(process[7]),
                "state": str(process[5]),
                "updated_at": str(process[9]),
            },
        )
        _write_text(
            process_dir / "route.md",
            "# Selected route\n\nBackend: fake-worker\nModel: fixture\n",
        )
    if work_order is not None:
        _write_text(process_dir / "work-orders" / "0001.md", str(work_order[0]))
    if session is not None:
        _write_json(
            process_dir / "sessions" / str(session[0]) / "session.json",
            {
                "ended_at": session[5],
                "process_id": str(session[1]),
                "sequence": int(session[2]),
                "session_id": str(session[0]),
                "started_at": str(session[4]),
                "state": str(session[3]),
            },
        )
    if evidence is not None:
        _write_json(
            process_dir / "evidence" / f"{V0_EVIDENCE_ID}.json",
            {
                "digest": str(evidence[0]),
                "exit_status": int(evidence[2]),
                "mission_id": mission_id,
                "mission_revision": int(evidence[1]),
                "output_artifacts": json.loads(str(evidence[3])),
                "source_event_id": int(evidence[4]),
            },
        )
    rendered_events = "".join(
        json.dumps(
            {
                "admission_id": row[8],
                "aggregate_id": str(row[5]),
                "aggregate_type": str(row[4]),
                "event_id": int(row[0]),
                "kind": str(row[2]),
                "occurred_at": str(row[1]),
                "operation_id": row[7],
                "payload": json.loads(str(row[6])),
                "severity": str(row[3]),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
        for row in events
    )
    _write_text(mission_dir / "events.jsonl", rendered_events)
    return status


def initialize_v0(root: Path, operation_id: str) -> dict[str, object]:
    outcome = initialize_root(
        root,
        InitializationRequest(
            operation_id=operation_id,
            invoker_id="maintenance:v0-e2e",
            expected_schema=0,
            expected_root_state="empty",
        ),
    )
    return asdict(outcome)


def seed_mission(
    root: Path,
    missions_root: Path,
    *,
    mission_id: str,
    objective_path: Path,
    operation_id: str,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    objective = Path(objective_path).read_text().strip()
    if not objective:
        raise InvalidOperation("mission objective is empty")
    request = OperationRequest(
        operation_id=operation_id,
        action="v0.mission_setup",
        invoker=Invoker(kind="maintenance_operator", id="maintenance:v0-e2e"),
        expected_revisions=(ExpectedRevision("mission", mission_id, 0),),
        payload={"mission_id": mission_id, "objective_digest": content_digest(objective)},
    )

    def validator(context: _ValidationContext) -> GateRejection | None:
        if context.db.execute("SELECT 1 FROM missions WHERE id=?", (mission_id,)).fetchone():
            return GateRejection("mission_exists", {"mission_id": mission_id})
        return None

    def projector(context: _ProjectionContext) -> None:
        event = context.append_event(
            "mission.created",
            "mission",
            mission_id,
            {"mission_id": mission_id, "objective_digest": content_digest(objective)},
        )
        context.db.execute(
            """
            INSERT INTO missions(
                id, project, objective, state, allowed_backends, allowed_models,
                allow_anthropic, verify_command, created_at, updated_at
            ) VALUES (?, '.', ?, 'draft', '["fake-worker"]', '["fixture"]', 0,
                      'aos-v0 verify', ?, ?)
            """,
            (mission_id, objective, context.occurred_at, context.occurred_at),
        )
        if getattr(event, "event_id", None) is None:
            raise InvalidOperation("mission creation did not produce an event")

    outcome = _run_governed(
        root,
        request,
        validator=validator,
        projector=projector,
        clock=clock,
    )
    if _operation_was_completed(outcome):
        mission_dir = Path(missions_root).resolve() / mission_id
        draft_path = mission_dir / "contract" / "draft.md"
        if not draft_path.exists():
            _write_text(draft_path, objective + "\n")
        _render_state_tree(root, missions_root, mission_id)
    return outcome


def _mission_state_rejection(
    context: _ValidationContext,
    *,
    mission_id: str,
    source: str,
    target: str,
    protocol_context: object | None = None,
) -> GateRejection | None:
    row = context.db.execute("SELECT state FROM missions WHERE id=?", (mission_id,)).fetchone()
    if row is None:
        return GateRejection("mission_missing", {"mission_id": mission_id})
    if str(row[0]) != source:
        return GateRejection(
            "mission_state_mismatch",
            {"expected": source, "mission_id": mission_id, "observed": str(row[0])},
        )
    try:
        require_transition(
            AggregateType.MISSION,
            source,
            target,
            context=protocol_context,
        )
    except ProtocolViolation:
        return GateRejection("protocol_rejected", {"from": source, "to": target})
    return None


def _liaison_operation(
    root: Path,
    *,
    operation_id: str,
    action: str,
    liaison_session_id: str,
    expected_revisions: tuple[ExpectedRevision, ...],
    payload: dict[str, object],
    validator: Callable[[_ValidationContext], GateRejection | None],
    projector: Callable[[_ProjectionContext], None],
    decision: tuple[str, str] | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    on_behalf_of, decision_hash = decision if decision is not None else (None, None)
    request = OperationRequest(
        operation_id=operation_id,
        action=action,
        invoker=Invoker(
            kind="liaison",
            id=liaison_session_id,
            on_behalf_of=on_behalf_of,
            decision_source_hash=decision_hash,
        ),
        expected_revisions=expected_revisions,
        payload=payload,
    )
    return _run_governed(
        root,
        request,
        validator=validator,
        projector=projector,
        clock=clock,
    )


def authorize_mission(
    root: Path,
    missions_root: Path,
    mission_id: str,
    *,
    operation_id: str,
    expected_revision: int,
    liaison_session_id: str,
    decision_path: Path,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    principal, decision_hash = _human_decision(Path(decision_path), "authorized")
    protocol_context = ContractAuthorization(
        human_principal=principal,
        decision_source_hash=decision_hash,
        current_revision=0,
        proposed_revision=1,
        attempts_created=0,
        attempt_ceiling=1,
        invalidates_prior_routing=False,
        invalidates_prior_evidence=False,
    )
    with sqlite3.connect(Path(root).resolve() / "aos.db") as db:
        objective = str(
            db.execute("SELECT objective FROM missions WHERE id=?", (mission_id,)).fetchone()[0]
        )
    contract = f"# Authorized contract: {mission_id}\n\n{objective}\n\nAttempt ceiling: 1\n"
    contract_hash = _sha256_bytes(contract.encode())
    targets = (
        ExpectedRevision("mission", mission_id, expected_revision),
        ExpectedRevision("contract_revision", V0_CONTRACT_ID, 0),
    )

    def validator(context: _ValidationContext) -> GateRejection | None:
        return _mission_state_rejection(
            context,
            mission_id=mission_id,
            source=MissionState.DRAFT,
            target=MissionState.AUTHORIZED,
            protocol_context=protocol_context,
        )

    def projector(context: _ProjectionContext) -> None:
        contract_event = context.append_event(
            "contract.authorized",
            "contract_revision",
            V0_CONTRACT_ID,
            {
                "contract_hash": contract_hash,
                "contract_revision_id": V0_CONTRACT_ID,
                "decision_source_hash": decision_hash,
                "human_principal": principal,
                "mission_id": mission_id,
                "revision": 1,
            },
        )
        context.db.execute(
            """
            INSERT INTO contract_revisions(
                id, mission_id, revision, content, content_hash, created_at, attempt_ceiling
            ) VALUES (?, ?, 1, ?, ?, ?, 1)
            """,
            (
                V0_CONTRACT_ID,
                mission_id,
                contract,
                contract_hash,
                context.occurred_at,
            ),
        )
        mission_event = context.append_event(
            "mission.transitioned",
            "mission",
            mission_id,
            {"from_state": "draft", "to_state": "authorized"},
        )
        context.db.execute(
            "UPDATE missions SET state='authorized', updated_at=? WHERE id=?",
            (context.occurred_at, mission_id),
        )
        if (
            getattr(contract_event, "event_id", None) is None
            or getattr(mission_event, "event_id", None) is None
        ):
            raise InvalidOperation("authorization events are incomplete")

    outcome = _liaison_operation(
        root,
        operation_id=operation_id,
        action="v0.mission_authorize",
        liaison_session_id=liaison_session_id,
        expected_revisions=targets,
        payload={"decision_source_hash": decision_hash, "mission_id": mission_id},
        validator=validator,
        projector=projector,
        decision=(principal, decision_hash),
        clock=clock,
    )
    if _operation_was_completed(outcome):
        _render_state_tree(root, missions_root, mission_id)
    return outcome


def dispatch_mission(
    root: Path,
    missions_root: Path,
    mission_id: str,
    *,
    operation_id: str,
    expected_revision: int,
    liaison_session_id: str,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    with sqlite3.connect(Path(root).resolve() / "aos.db") as db:
        objective = str(
            db.execute("SELECT objective FROM missions WHERE id=?", (mission_id,)).fetchone()[0]
        )
        contract_hash = str(
            db.execute(
                "SELECT content_hash FROM contract_revisions WHERE id=?",
                (V0_CONTRACT_ID,),
            ).fetchone()[0]
        )
    work_order = (
        f"# Work order {V0_WORK_ORDER_ID}\n\n{objective}\n\n"
        "Run `sh eval-fixture/fake-worker.sh "
        f"missions/{mission_id}/attempts/0001/worker-output`.\n"
    )
    work_order_hash = _sha256_bytes(work_order.encode())
    placeholder_token = _sha256_bytes(b"v0-fake-worker-authority")
    targets = (
        ExpectedRevision("mission", mission_id, expected_revision),
        ExpectedRevision("attempt", V0_ATTEMPT_ID, 0),
        ExpectedRevision("process", V0_PROCESS_ID, 0),
        ExpectedRevision("work_order", V0_WORK_ORDER_ID, 0),
        ExpectedRevision("worker_session", V0_SESSION_ID, 0),
    )

    def validator(context: _ValidationContext) -> GateRejection | None:
        return _mission_state_rejection(
            context,
            mission_id=mission_id,
            source=MissionState.AUTHORIZED,
            target=MissionState.DISPATCHED,
        )

    def projector(context: _ProjectionContext) -> None:
        context.append_event(
            "attempt.created",
            "attempt",
            V0_ATTEMPT_ID,
            {"attempt_id": V0_ATTEMPT_ID, "mission_id": mission_id, "number": 1},
        )
        context.db.execute(
            """
            INSERT INTO attempts(
                id, mission_id, number, state, worktree, branch, created_at, updated_at
            ) VALUES (?, ?, 1, 'active', 'fixture-worktree', 'v0/e2e-00', ?, ?)
            """,
            (V0_ATTEMPT_ID, mission_id, context.occurred_at, context.occurred_at),
        )
        context.append_event(
            "process.created",
            "process",
            V0_PROCESS_ID,
            {
                "attempt_id": V0_ATTEMPT_ID,
                "mission_id": mission_id,
                "process_id": V0_PROCESS_ID,
            },
        )
        context.db.execute(
            """
            INSERT INTO processes(
                id, mission_id, attempt, backend, model, state, token_hash,
                token_expires_at, worktree, session_id, checkpoint,
                routing_reason, created_at, updated_at
            ) VALUES (?, ?, 1, 'fake-worker', 'fixture', 'created', ?, ?,
                      'fixture-worktree', ?, '{}', 'E2E-00 fixed route', ?, ?)
            """,
            (
                V0_PROCESS_ID,
                mission_id,
                placeholder_token,
                context.occurred_at,
                V0_SESSION_ID,
                context.occurred_at,
                context.occurred_at,
            ),
        )
        context.append_event(
            "work_order.created",
            "work_order",
            V0_WORK_ORDER_ID,
            {
                "content_hash": work_order_hash,
                "mission_id": mission_id,
                "process_id": V0_PROCESS_ID,
                "work_order_id": V0_WORK_ORDER_ID,
            },
        )
        context.db.execute(
            """
            INSERT INTO work_orders(
                id, mission_id, process_id, revision, content, content_hash, created_at
            ) VALUES (?, ?, ?, 1, ?, ?, ?)
            """,
            (
                V0_WORK_ORDER_ID,
                mission_id,
                V0_PROCESS_ID,
                work_order,
                work_order_hash,
                context.occurred_at,
            ),
        )
        context.append_event(
            "worker_session.created",
            "worker_session",
            V0_SESSION_ID,
            {"process_id": V0_PROCESS_ID, "sequence": 1, "session_id": V0_SESSION_ID},
        )
        context.db.execute(
            """
            INSERT INTO worker_sessions(
                id, process_id, sequence, state, backend_handle,
                metadata, started_at, ended_at
            ) VALUES (?, ?, 1, 'created', NULL, '{}', ?, NULL)
            """,
            (V0_SESSION_ID, V0_PROCESS_ID, context.occurred_at),
        )
        context.append_event(
            "mission.transitioned",
            "mission",
            mission_id,
            {"from_state": "authorized", "to_state": "dispatched"},
        )
        context.db.execute(
            "UPDATE missions SET state='dispatched', updated_at=? WHERE id=?",
            (context.occurred_at, mission_id),
        )

    outcome = _liaison_operation(
        root,
        operation_id=operation_id,
        action="v0.mission_dispatch",
        liaison_session_id=liaison_session_id,
        expected_revisions=targets,
        payload={
            "contract_hash": contract_hash,
            "mission_id": mission_id,
            "work_order_hash": work_order_hash,
        },
        validator=validator,
        projector=projector,
        clock=clock,
    )
    if _operation_was_completed(outcome):
        _render_state_tree(root, missions_root, mission_id)
    return outcome


def _transition_runtime(
    root: Path,
    missions_root: Path,
    mission_id: str,
    *,
    operation_id: str,
    expected_revision: int,
    liaison_session_id: str,
    phase: str,
    receipt_path: Path | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    if phase == "start":
        mission_edge = ("dispatched", "implementing")
        process_edge = ("created", "running")
        session_edge = ("created", "running")
        targets = (
            ExpectedRevision("mission", mission_id, expected_revision),
            ExpectedRevision("process", V0_PROCESS_ID, 1),
            ExpectedRevision("worker_session", V0_SESSION_ID, 1),
        )
        receipt_digest = None
        result_digest = None
    elif phase == "finish":
        mission_edge = ("implementing", "verifying")
        process_edge = ("running", "completed")
        session_edge = ("running", "completed")
        targets = (
            ExpectedRevision("mission", mission_id, expected_revision),
            ExpectedRevision("process", V0_PROCESS_ID, 2),
            ExpectedRevision("worker_session", V0_SESSION_ID, 2),
            ExpectedRevision("attempt", V0_ATTEMPT_ID, 1),
        )
        if receipt_path is None or not Path(receipt_path).is_file():
            raise InvalidOperation("finish requires a worker receipt")
        receipt_file = Path(receipt_path)
        receipt = _json_file(receipt_file)
        if set(receipt) != {
            "attempt_id",
            "mission_id",
            "process_id",
            "result_path",
            "result_sha256",
            "schema_version",
            "status",
            "worker_session_id",
        }:
            raise InvalidOperation("worker receipt has unexpected fields")
        expected_receipt = {
            "attempt_id": V0_ATTEMPT_ID,
            "mission_id": mission_id,
            "process_id": V0_PROCESS_ID,
            "result_path": "result.txt",
            "schema_version": 1,
            "status": "completed",
            "worker_session_id": V0_SESSION_ID,
        }
        if any(receipt.get(key) != value for key, value in expected_receipt.items()):
            raise InvalidOperation("worker receipt does not match the dispatched identities")
        result_file = receipt_file.resolve().parent / "result.txt"
        if not result_file.is_file():
            raise InvalidOperation("worker receipt names a missing result")
        result_digest = _sha256_bytes(result_file.read_bytes())
        if receipt.get("result_sha256") != result_digest:
            raise InvalidOperation("worker receipt result digest does not match the result")
        receipt_digest = _sha256_bytes(receipt_file.read_bytes())
    else:
        raise InvalidOperation(f"unknown runtime phase: {phase}")

    def validator(context: _ValidationContext) -> GateRejection | None:
        rejection = _mission_state_rejection(
            context,
            mission_id=mission_id,
            source=mission_edge[0],
            target=mission_edge[1],
        )
        if rejection is not None:
            return rejection
        try:
            require_transition(AggregateType.PROCESS, *process_edge)
            require_transition(AggregateType.WORKER_SESSION, *session_edge)
            if phase == "finish":
                require_transition(
                    AggregateType.ATTEMPT,
                    AttemptState.ACTIVE,
                    AttemptState.COMPLETED,
                )
        except ProtocolViolation:
            return GateRejection("protocol_rejected", {"phase": phase})
        return None

    def projector(context: _ProjectionContext) -> None:
        extra = (
            {}
            if receipt_digest is None
            else {
                "receipt_digest": receipt_digest,
                "result_digest": result_digest,
            }
        )
        context.append_event(
            "process.transitioned",
            "process",
            V0_PROCESS_ID,
            {"from_state": process_edge[0], "to_state": process_edge[1], **extra},
        )
        context.db.execute(
            "UPDATE processes SET state=?, updated_at=? WHERE id=?",
            (process_edge[1], context.occurred_at, V0_PROCESS_ID),
        )
        context.append_event(
            "worker_session.transitioned",
            "worker_session",
            V0_SESSION_ID,
            {"from_state": session_edge[0], "to_state": session_edge[1], **extra},
        )
        context.db.execute(
            """
            UPDATE worker_sessions
            SET state=?, ended_at=CASE WHEN ?='completed' THEN ? ELSE ended_at END
            WHERE id=?
            """,
            (session_edge[1], session_edge[1], context.occurred_at, V0_SESSION_ID),
        )
        if phase == "finish":
            context.append_event(
                "attempt.transitioned",
                "attempt",
                V0_ATTEMPT_ID,
                {"from_state": "active", "to_state": "completed", **extra},
            )
            context.db.execute(
                "UPDATE attempts SET state='completed', updated_at=? WHERE id=?",
                (context.occurred_at, V0_ATTEMPT_ID),
            )
        context.append_event(
            "mission.transitioned",
            "mission",
            mission_id,
            {"from_state": mission_edge[0], "to_state": mission_edge[1], **extra},
        )
        context.db.execute(
            "UPDATE missions SET state=?, updated_at=? WHERE id=?",
            (mission_edge[1], context.occurred_at, mission_id),
        )

    outcome = _liaison_operation(
        root,
        operation_id=operation_id,
        action=f"v0.mission_{phase}",
        liaison_session_id=liaison_session_id,
        expected_revisions=targets,
        payload={
            "mission_id": mission_id,
            "receipt_digest": receipt_digest,
            "result_digest": result_digest,
        },
        validator=validator,
        projector=projector,
        clock=clock,
    )
    if _operation_was_completed(outcome):
        _render_state_tree(root, missions_root, mission_id)
    return outcome


def verify_mission(
    root: Path,
    missions_root: Path,
    mission_id: str,
    *,
    operation_id: str,
    expected_revision: int,
    liaison_session_id: str,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    output_path = (
        Path(missions_root).resolve()
        / mission_id
        / "attempts"
        / "0001"
        / "worker-output"
        / "result.txt"
    )
    if not output_path.is_file():
        raise InvalidOperation("worker output failed the E2E-00 verifier")
    output_digest = _sha256_bytes(output_path.read_bytes())
    if output_digest != V0_EXPECTED_RESULT_DIGEST:
        raise InvalidOperation("worker output failed the E2E-00 verifier")
    with sqlite3.connect(Path(root).resolve() / "aos.db") as db:
        contract = db.execute(
            "SELECT id, content_hash FROM contract_revisions WHERE mission_id=?",
            (mission_id,),
        ).fetchone()
    if contract is None:
        raise InvalidOperation("verified mission has no authorized contract")
    evidence_record = {
        "attempt_id": V0_ATTEMPT_ID,
        "command_hash": _sha256_bytes(b"E2E-00 byte equality verifier"),
        "contract_hash": str(contract[1]),
        "contract_revision_id": str(contract[0]),
        "ended_at": "recorded-by-operation-clock",
        "exit_status": 0,
        "git_head": "0" * 40,
        "index_digest": output_digest,
        "mission_id": mission_id,
        "mission_revision": expected_revision,
        "output_artifacts": [output_digest],
        "process_id": V0_PROCESS_ID,
        "record_schema_version": 1,
        "started_at": "recorded-by-operation-clock",
        "verifier_spec_digest": V0_VERIFIER_SPEC_DIGEST,
        "worker_session_id": V0_SESSION_ID,
        "worktree_manifest_digest": output_digest,
    }
    targets = (
        ExpectedRevision("mission", mission_id, expected_revision),
        ExpectedRevision("evidence", V0_EVIDENCE_ID, 0),
    )

    def validator(context: _ValidationContext) -> GateRejection | None:
        return _mission_state_rejection(
            context,
            mission_id=mission_id,
            source=MissionState.VERIFYING,
            target=MissionState.AWAITING_APPROVAL,
        )

    def projector(context: _ProjectionContext) -> None:
        record = dict(evidence_record)
        record["started_at"] = context.occurred_at
        record["ended_at"] = context.occurred_at
        validate_evidence_record(record)
        evidence_digest = content_digest(record)
        event = context.append_event(
            "evidence.recorded",
            "evidence",
            V0_EVIDENCE_ID,
            {
                "evidence_digest": evidence_digest,
                "evidence_id": V0_EVIDENCE_ID,
                "exit_status": 0,
                "mission_id": mission_id,
                "output_digest": output_digest,
            },
        )
        context.db.execute(
            """
            INSERT INTO evidence_records(
                digest, record_schema_version, mission_id, mission_revision,
                contract_revision_id, contract_hash, attempt_id, process_id,
                worker_session_id, git_head, index_digest,
                worktree_manifest_digest, verifier_spec_digest, command_hash,
                started_at, ended_at, exit_status, output_artifacts, source_event_id
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                evidence_digest,
                mission_id,
                expected_revision,
                str(contract[0]),
                str(contract[1]),
                V0_ATTEMPT_ID,
                V0_PROCESS_ID,
                V0_SESSION_ID,
                "0" * 40,
                output_digest,
                output_digest,
                V0_VERIFIER_SPEC_DIGEST,
                str(record["command_hash"]),
                context.occurred_at,
                context.occurred_at,
                json.dumps(record["output_artifacts"], sort_keys=True),
                event.event_id,  # type: ignore[attr-defined]
            ),
        )
        context.append_event(
            "mission.transitioned",
            "mission",
            mission_id,
            {
                "evidence_digest": evidence_digest,
                "evidence_id": V0_EVIDENCE_ID,
                "from_state": "verifying",
                "to_state": "awaiting_approval",
            },
        )
        context.db.execute(
            "UPDATE missions SET state='awaiting_approval', updated_at=? WHERE id=?",
            (context.occurred_at, mission_id),
        )

    outcome = _liaison_operation(
        root,
        operation_id=operation_id,
        action="v0.mission_verify",
        liaison_session_id=liaison_session_id,
        expected_revisions=targets,
        payload={
            "mission_id": mission_id,
            "output_digest": output_digest,
            "verifier_spec_digest": V0_VERIFIER_SPEC_DIGEST,
        },
        validator=validator,
        projector=projector,
        clock=clock,
    )
    if _operation_was_completed(outcome):
        _render_state_tree(root, missions_root, mission_id)
    return outcome


def decide_mission(
    root: Path,
    missions_root: Path,
    mission_id: str,
    *,
    operation_id: str,
    expected_revision: int,
    liaison_session_id: str,
    decision_path: Path,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    principal, decision_hash = _human_decision(Path(decision_path), "approved")
    output_path = (
        Path(missions_root).resolve()
        / mission_id
        / "attempts"
        / "0001"
        / "worker-output"
        / "result.txt"
    )
    try:
        observed_output_digest = _sha256_bytes(output_path.read_bytes())
    except OSError:
        observed_output_digest = None
    with sqlite3.connect(Path(root).resolve() / "aos.db") as db:
        evidence = db.execute(
            """
            SELECT digest
            FROM evidence_records
            WHERE mission_id=?
            ORDER BY source_event_id DESC
            LIMIT 1
            """,
            (mission_id,),
        ).fetchone()
    if evidence is None:
        raise InvalidOperation("approval requires current evidence")
    evidence_digest = str(evidence[0])
    targets = (
        ExpectedRevision("mission", mission_id, expected_revision),
        ExpectedRevision("approval", V0_APPROVAL_ID, 0),
    )

    def validator(context: _ValidationContext) -> GateRejection | None:
        rejection = _mission_state_rejection(
            context,
            mission_id=mission_id,
            source=MissionState.AWAITING_APPROVAL,
            target=MissionState.DELIVERED,
        )
        if rejection is not None:
            return rejection
        current = context.db.execute(
            """
            SELECT
                digest, mission_revision, exit_status, output_artifacts,
                index_digest, worktree_manifest_digest, verifier_spec_digest
            FROM evidence_records
            WHERE mission_id=?
            ORDER BY source_event_id DESC
            LIMIT 1
            """,
            (mission_id,),
        ).fetchone()
        try:
            current_output_digest = _sha256_bytes(output_path.read_bytes())
        except OSError:
            return GateRejection("evidence_output_missing", {"mission_id": mission_id})
        try:
            output_artifacts = json.loads(str(current[3])) if current is not None else None
        except json.JSONDecodeError:
            output_artifacts = None
        if (
            current is None
            or observed_output_digest is None
            or current_output_digest != observed_output_digest
            or current_output_digest != V0_EXPECTED_RESULT_DIGEST
            or str(current[0]) != evidence_digest
            or int(current[1]) != expected_revision - 1
            or int(current[2]) != 0
            or output_artifacts != [current_output_digest]
            or str(current[4]) != current_output_digest
            or str(current[5]) != current_output_digest
            or str(current[6]) != V0_VERIFIER_SPEC_DIGEST
        ):
            return GateRejection("evidence_stale", {"mission_id": mission_id})
        return None

    def projector(context: _ProjectionContext) -> None:
        approval_record = {
            "decision": "approved",
            "decision_schema_version": 1,
            "decision_source_hash": decision_hash,
            "evidence_digest": evidence_digest,
            "expected_mission_revision": expected_revision,
            "human_principal": principal,
            "mission_id": mission_id,
        }
        validate_approval_decision(approval_record)
        event = context.append_event(
            "approval.decided",
            "approval",
            V0_APPROVAL_ID,
            {
                **approval_record,
                "evidence_id": V0_EVIDENCE_ID,
            },
        )
        context.db.execute(
            """
            INSERT INTO approval_decisions(
                decision_event_id, decision_schema_version, mission_id,
                expected_mission_revision, evidence_digest, decision,
                human_principal, decision_source_hash
            ) VALUES (?, 1, ?, ?, ?, 'approved', ?, ?)
            """,
            (
                event.event_id,  # type: ignore[attr-defined]
                mission_id,
                expected_revision,
                evidence_digest,
                principal,
                decision_hash,
            ),
        )
        context.append_event(
            "mission.transitioned",
            "mission",
            mission_id,
            {
                "approval_id": V0_APPROVAL_ID,
                "evidence_digest": evidence_digest,
                "evidence_id": V0_EVIDENCE_ID,
                "from_state": "awaiting_approval",
                "to_state": "delivered",
            },
        )
        context.db.execute(
            "UPDATE missions SET state='delivered', updated_at=? WHERE id=?",
            (context.occurred_at, mission_id),
        )

    outcome = _liaison_operation(
        root,
        operation_id=operation_id,
        action="v0.mission_decide",
        liaison_session_id=liaison_session_id,
        expected_revisions=targets,
        payload={
            "decision_source_hash": decision_hash,
            "evidence_digest": evidence_digest,
            "mission_id": mission_id,
            "observed_output_digest": observed_output_digest,
        },
        validator=validator,
        projector=projector,
        decision=(principal, decision_hash),
        clock=clock,
    )
    if _operation_was_completed(outcome):
        _render_state_tree(root, missions_root, mission_id)
    return outcome


def _liaison_session_from_env(value: str | None) -> str:
    session_id = value or os.environ.get("AOS_LIAISON_SESSION_ID")
    if not session_id:
        raise InvalidOperation("AOS_LIAISON_SESSION_ID is required")
    return session_id


def _outcome_json(outcome: OperationOutcome | dict[str, object]) -> dict[str, object]:
    if isinstance(outcome, dict):
        return outcome
    return {
        "admission_event_id": outcome.admission_event_id,
        "domain_event_ids": list(outcome.domain_event_ids),
        "operation_id": outcome.operation_id,
        "original_status": outcome.original_status,
        "reason": outcome.reason,
        "rejection_event_id": outcome.rejection_event_id,
        "revisions": [asdict(item) for item in outcome.revisions],
        "status": outcome.status,
    }


def _result_is_rejected(result: dict[str, object]) -> bool:
    return result.get("status") == "rejected" or (
        result.get("status") == "replayed" and result.get("original_status") == "rejected"
    )


def _with_mission_summary(
    result: dict[str, object],
    root: Path,
    mission_id: str,
) -> dict[str, object]:
    if _result_is_rejected(result):
        return result
    status = mission_status(root, mission_id)
    result.update(
        {
            "evidence_digest": status["evidence_digest"],
            "evidence_id": status["evidence_id"],
            "mission_id": mission_id,
            "mission_revision": status["revision"],
            "mission_state": status["state"],
        }
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aos-v0")
    parser.add_argument("--state-root")
    parser.add_argument("--missions-root")
    parser.add_argument("--liaison-session")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init")
    init.add_argument("--op", default="OP-V0-INIT")

    setup = subparsers.add_parser("setup")
    setup.add_argument("mission_id")
    setup.add_argument("--objective", required=True)
    setup.add_argument("--op", default="OP-V0-SETUP")

    status = subparsers.add_parser("status")
    status.add_argument("mission_id", nargs="?", default="M-V0")

    for name in ("authorize", "dispatch", "start", "finish", "verify", "decide"):
        command = subparsers.add_parser(name)
        command.add_argument("mission_id")
        command.add_argument("--op", required=True)
        command.add_argument("--expect", required=True, type=int)
        if name in {"authorize", "decide"}:
            command.add_argument("--decision", required=True)
        elif name == "finish":
            command.add_argument("--receipt", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        paths = resolve_runtime_paths(args.state_root, args.missions_root)
        root = paths.state_root
        missions_root = paths.missions_root
        if args.command == "init":
            result = initialize_v0(root, args.op)
        elif args.command == "setup":
            result = _with_mission_summary(
                _outcome_json(
                    seed_mission(
                        root,
                        missions_root,
                        mission_id=args.mission_id,
                        objective_path=Path(args.objective),
                        operation_id=args.op,
                    )
                ),
                root,
                args.mission_id,
            )
        elif args.command == "status":
            result = _render_state_tree(root, missions_root, args.mission_id)
        elif args.command == "authorize":
            result = _with_mission_summary(
                _outcome_json(
                    authorize_mission(
                        root,
                        missions_root,
                        args.mission_id,
                        operation_id=args.op,
                        expected_revision=args.expect,
                        liaison_session_id=_liaison_session_from_env(args.liaison_session),
                        decision_path=Path(args.decision),
                    )
                ),
                root,
                args.mission_id,
            )
        elif args.command == "dispatch":
            result = _with_mission_summary(
                _outcome_json(
                    dispatch_mission(
                        root,
                        missions_root,
                        args.mission_id,
                        operation_id=args.op,
                        expected_revision=args.expect,
                        liaison_session_id=_liaison_session_from_env(args.liaison_session),
                    )
                ),
                root,
                args.mission_id,
            )
        elif args.command in {"start", "finish"}:
            result = _with_mission_summary(
                _outcome_json(
                    _transition_runtime(
                        root,
                        missions_root,
                        args.mission_id,
                        operation_id=args.op,
                        expected_revision=args.expect,
                        liaison_session_id=_liaison_session_from_env(args.liaison_session),
                        phase=args.command,
                        receipt_path=(Path(args.receipt) if args.command == "finish" else None),
                    )
                ),
                root,
                args.mission_id,
            )
        elif args.command == "verify":
            result = _with_mission_summary(
                _outcome_json(
                    verify_mission(
                        root,
                        missions_root,
                        args.mission_id,
                        operation_id=args.op,
                        expected_revision=args.expect,
                        liaison_session_id=_liaison_session_from_env(args.liaison_session),
                    )
                ),
                root,
                args.mission_id,
            )
        else:
            result = _with_mission_summary(
                _outcome_json(
                    decide_mission(
                        root,
                        missions_root,
                        args.mission_id,
                        operation_id=args.op,
                        expected_revision=args.expect,
                        liaison_session_id=_liaison_session_from_env(args.liaison_session),
                        decision_path=Path(args.decision),
                    )
                ),
                root,
                args.mission_id,
            )
    except (InvalidOperation, OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({"error": str(exc), "status": "invalid"}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 1 if _result_is_rejected(result) else 0


if __name__ == "__main__":
    raise SystemExit(main())
