"""Provisional mission controls learned from one disposable mission.

Humans may speak vaguely. This module preserves the raw request, lets the liaison
refine an evaluable contract in the State Tree (interviewing when needed), and
admits authorization only when that contract is evaluable. It also gates worker
authority, lifecycle transitions, and bound receipts. It never launches, sequences,
supervises, or evaluates a worker; agents remain the runtime. Verification returns
only after a general evidence interface earns its place.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
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
    _outcome_from_record,
    _run_governed,
    append_observation,
    content_digest,
    initialize_root,
    utc_now,
)
from modules.lease import capability_digest
from modules.protocol import (
    AggregateType,
    ContractAuthorization,
    MissionState,
    ProcessRecovery,
    ProtocolViolation,
    RetryKind,
    require_transition,
)

REQUIRED_CONTRACT_HEADINGS = (
    "Outcome",
    "Acceptance",
    "Constraints",
    "Out of scope",
    "Open questions",
)
UNCHECKED_ITEM = re.compile(r"^\s*[-*]\s+\[\s\]\s+", re.MULTILINE)
CHECKED_ITEM = re.compile(r"^\s*[-*]\s+\[[xX]\]\s+", re.MULTILINE)
HEADING_LINE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
WORKER_NOTE = re.compile(r"^\s*-\s+([A-Za-z-]+):\s*(.*?)\s*$", re.MULTILINE)
SECRET_SHAPED = re.compile(
    rb"(?i)(?:sk-[a-z0-9_-]{20,}|gh[pousr]_[a-z0-9]{20,}|bearer\s+[a-z0-9._~+/=-]{20,})"
)
SECRET_ASSIGNMENT = re.compile(
    rb"""(?ix)
    ["']?
    (?:claim[_-]?token|access[_-]?token|api[_-]?key|secret|password)
    ["']?
    \s*[:=]\s*
    ["'][^"'\r\n]{8,}["']
    """
)
PRIVATE_PATH_MARKERS = (
    b"/Users/",
    b"/private/var/folders/",
    b"/var/folders/",
    b".run/missions/",
    b"missions/M-",
    b"state/liaison/",
    b"AOS_WORKER_TOKEN",
    b"AOS_LIAISON_SESSION_ID",
)


class _ValidationContext(Protocol):
    db: sqlite3.Connection
    occurred_at: datetime


class _ProjectionContext(Protocol):
    db: sqlite3.Connection
    occurred_at: str

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


@dataclass(frozen=True)
class _WorkerMembrane:
    attempt_id: str
    attempt_number: int
    attempt_state: str
    authority_epoch_id: str
    authority_expires_at: str
    authority_status: str
    backend: str
    budget_seconds: int
    model: str
    process_id: str
    process_state: str
    session_id: str
    session_sequence: int
    session_state: str
    work_order_id: str
    workspace: str


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _write_json(path: Path, value: object) -> None:
    _write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _json_file(path: Path) -> dict[str, object]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise InvalidOperation("decision file must be a JSON object")
    return payload


def _contains_unsafe_content(payload: bytes) -> bool:
    lowered = payload.lower()
    return (
        SECRET_SHAPED.search(payload) is not None
        or SECRET_ASSIGNMENT.search(payload) is not None
        or any(marker.lower() in lowered for marker in PRIVATE_PATH_MARKERS)
    )


def _human_decision(path: Path, expected_decision: str) -> tuple[str, str]:
    payload = _json_file(path)
    decision = payload.get("decision")
    principal = payload.get("human_principal")
    source_hash = payload.get("decision_source_hash")
    if decision != expected_decision:
        raise InvalidOperation(f"decision must be {expected_decision!r}")
    if not isinstance(principal, str) or not principal:
        raise InvalidOperation("human_principal is required")
    if not isinstance(source_hash, str) or len(source_hash) != 64:
        raise InvalidOperation("decision_source_hash must be a 64-char hex digest")
    return principal, source_hash


def _operation_was_completed(outcome: OperationOutcome) -> bool:
    return outcome.status == "completed" or (
        outcome.status == "replayed" and outcome.original_status == "completed"
    )


def _recorded_liaison_replay(
    root: Path,
    *,
    operation_id: str,
    action: str,
    mission_id: str,
    expected_revision: int,
    event_payload: dict[str, object] | None = None,
) -> OperationOutcome | None:
    """Return an exact recorded replay before mutable current state is reread."""

    with sqlite3.connect(Path(root).resolve() / "aos.db") as db:
        operation = db.execute(
            """
            SELECT action, status, outcome
            FROM operations
            WHERE operation_id=?
            """,
            (operation_id,),
        ).fetchone()
        if operation is None or str(operation[0]) != action:
            return None
        target = db.execute(
            """
            SELECT expected_revision
            FROM operation_targets
            WHERE operation_id=? AND aggregate_type='mission' AND aggregate_id=?
            """,
            (operation_id, mission_id),
        ).fetchone()
        if target is None or int(target[0]) != expected_revision:
            return None
        if event_payload:
            payloads = db.execute(
                """
                SELECT event.payload
                FROM events AS event
                JOIN event_envelopes AS envelope ON envelope.event_id=event.id
                WHERE envelope.operation_id=?
                """,
                (operation_id,),
            ).fetchall()
            if not any(
                all(json.loads(str(row[0])).get(key) == value for key, value in event_payload.items())
                for row in payloads
            ):
                return None
    return _outcome_from_record(operation_id, str(operation[1]), str(operation[2]))


def _recorded_replacement_replay(
    root: Path,
    *,
    operation_id: str,
    mission_id: str,
    expected_revision: int,
    worker_token: str,
    budget_seconds: int,
) -> OperationOutcome | None:
    replay = _recorded_liaison_replay(
        root,
        operation_id=operation_id,
        action="mission_controls.replace_worker",
        mission_id=mission_id,
        expected_revision=expected_revision,
    )
    if replay is None or replay.original_status != "completed":
        return replay
    with sqlite3.connect(Path(root).resolve() / "aos.db") as db:
        epoch = db.execute(
            """
            SELECT authority.subject_id, authority.scope, authority.token_hash
            FROM authority_epochs AS authority
            JOIN event_envelopes AS envelope
              ON envelope.event_id=authority.issued_event_id
            WHERE envelope.operation_id=? AND authority.kind='worker'
            """,
            (operation_id,),
        ).fetchone()
        session = db.execute(
            """
            SELECT session.metadata
            FROM worker_sessions AS session
            JOIN events AS event
              ON event.aggregate_type='worker_session'
             AND event.aggregate_id=session.id
             AND event.kind='worker_session.created'
            JOIN event_envelopes AS envelope ON envelope.event_id=event.id
            WHERE envelope.operation_id=?
            """,
            (operation_id,),
        ).fetchone()
    if epoch is None or session is None:
        return None
    metadata = json.loads(str(session[0]))
    supplied_hash = capability_digest(
        worker_token,
        kind="worker",
        subject_id=str(epoch[0]),
        scope=str(epoch[1]),
    )
    if supplied_hash != str(epoch[2]) or metadata.get("budget_seconds") != budget_seconds:
        return None
    return replay


def _section_body(markdown: str, heading: str) -> str | None:
    matches = list(HEADING_LINE.finditer(markdown))
    for index, match in enumerate(matches):
        if match.group(1) != heading:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        return markdown[start:end].strip()
    return None


def contract_evaluation_errors(markdown: str) -> tuple[str, ...]:
    """Return deterministic reasons a proposed contract is not yet evaluable."""

    errors: list[str] = []
    headings = HEADING_LINE.findall(markdown)
    for required in REQUIRED_CONTRACT_HEADINGS:
        count = sum(1 for item in headings if item == required)
        if count == 0:
            errors.append(f"missing_section:{required}")
        elif count > 1:
            errors.append(f"duplicate_section:{required}")
    if errors:
        return tuple(errors)

    acceptance = _section_body(markdown, "Acceptance") or ""
    if not CHECKED_ITEM.search(acceptance) and not UNCHECKED_ITEM.search(acceptance):
        errors.append("acceptance_needs_checkbox_items")

    open_questions = _section_body(markdown, "Open questions") or ""
    normalized = open_questions.strip().rstrip(".")
    if normalized == "None":
        return tuple(errors)
    if UNCHECKED_ITEM.search(open_questions) or not CHECKED_ITEM.search(open_questions):
        errors.append("open_questions_unresolved")
    return tuple(errors)


def unresolved_intake_questions(mission_dir: Path) -> bool:
    questions = mission_dir / "intake" / "open-questions.md"
    if not questions.is_file():
        return False
    return UNCHECKED_ITEM.search(questions.read_text(encoding="utf-8")) is not None


def initialize_controls(root: Path, operation_id: str) -> dict[str, object]:
    outcome = initialize_root(
        root,
        InitializationRequest(
            operation_id=operation_id,
            invoker_id="maintenance:mission-controls",
            expected_schema=0,
            expected_root_state="empty",
        ),
    )
    return asdict(outcome)


def _mission_revision(root: Path, mission_id: str) -> int | None:
    db_path = Path(root).resolve() / "aos.db"
    with sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True) as db:
        row = db.execute(
            """
            SELECT revision FROM aggregate_revisions
            WHERE aggregate_type='mission' AND aggregate_id=?
            """,
            (mission_id,),
        ).fetchone()
    return None if row is None else int(row[0])


def _aggregate_revision(root: Path, aggregate_type: str, aggregate_id: str) -> int:
    with sqlite3.connect(Path(root).resolve() / "aos.db") as db:
        row = db.execute(
            """
            SELECT revision FROM aggregate_revisions
            WHERE aggregate_type=? AND aggregate_id=?
            """,
            (aggregate_type, aggregate_id),
        ).fetchone()
    if row is None:
        raise InvalidOperation(f"{aggregate_type} {aggregate_id} is missing")
    return int(row[0])


def _current_worker_membrane(root: Path, mission_id: str) -> _WorkerMembrane:
    with sqlite3.connect(Path(root).resolve() / "aos.db") as db:
        processes = db.execute(
            """
            SELECT
                process.id, process.attempt, process.state, process.backend,
                process.model, process.worktree, process.session_id,
                attempt.id, attempt.state
            FROM processes AS process
            JOIN attempts AS attempt
              ON attempt.mission_id=process.mission_id
             AND attempt.number=process.attempt
            WHERE process.mission_id=?
              AND process.state IN ('created', 'running')
            ORDER BY process.created_at DESC, process.id DESC
            LIMIT 2
            """,
            (mission_id,),
        ).fetchall()
        if len(processes) != 1:
            raise InvalidOperation("mission must have exactly one current worker process")
        process = processes[0]
        session = db.execute(
            """
            SELECT state, metadata, sequence
            FROM worker_sessions
            WHERE id=? AND process_id=?
            """,
            (str(process[6]), str(process[0])),
        ).fetchone()
        epochs = db.execute(
            """
            SELECT epoch_id, status, expires_at
            FROM authority_epochs
            WHERE kind='worker' AND subject_id=? AND status='active'
            """,
            (str(process[0]),),
        ).fetchall()
        work_order = db.execute(
            """
            SELECT id
            FROM work_orders
            WHERE process_id=?
            ORDER BY revision DESC
            LIMIT 1
            """,
            (str(process[0]),),
        ).fetchone()
    if session is None or len(epochs) != 1 or work_order is None:
        raise InvalidOperation("current worker membrane is incomplete")
    metadata = json.loads(str(session[1]))
    return _WorkerMembrane(
        attempt_id=str(process[7]),
        attempt_number=int(process[1]),
        attempt_state=str(process[8]),
        authority_epoch_id=str(epochs[0][0]),
        authority_expires_at=str(epochs[0][2]),
        authority_status=str(epochs[0][1]),
        backend=str(process[3]),
        budget_seconds=int(metadata["budget_seconds"]),
        model=str(process[4]),
        process_id=str(process[0]),
        process_state=str(process[2]),
        session_id=str(process[6]),
        session_sequence=int(session[2]),
        session_state=str(session[0]),
        work_order_id=str(work_order[0]),
        workspace=str(process[5]),
    )


def intake_mission(
    root: Path,
    missions_root: Path,
    *,
    mission_id: str,
    human_request_path: Path,
    operation_id: str,
    liaison_session_id: str,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    request_text = Path(human_request_path).read_text(encoding="utf-8")
    if not request_text.strip():
        raise InvalidOperation("human request is empty")
    request_digest = content_digest(request_text)

    def validator(context: _ValidationContext) -> GateRejection | None:
        if context.db.execute("SELECT 1 FROM missions WHERE id=?", (mission_id,)).fetchone():
            return GateRejection("mission_exists", {"mission_id": mission_id})
        return None

    def projector(context: _ProjectionContext) -> None:
        event = context.append_event(
            "mission.created",
            "mission",
            mission_id,
            {
                "intake": "human_request_preserved",
                "mission_id": mission_id,
                "objective_digest": request_digest,
            },
        )
        context.db.execute(
            """
            INSERT INTO missions(
                id, project, objective, state, allowed_backends, allowed_models,
                allow_anthropic, verify_command, created_at, updated_at
            ) VALUES (?, '.', ?, 'draft', '[]', '[]', 0, 'pending', ?, ?)
            """,
            (mission_id, request_text.strip(), context.occurred_at, context.occurred_at),
        )
        if getattr(event, "event_id", None) is None:
            raise InvalidOperation("mission creation did not produce an event")

    outcome = _liaison_operation(
        root,
        operation_id=operation_id,
        action="mission_controls.intake",
        liaison_session_id=liaison_session_id,
        expected_revisions=(ExpectedRevision("mission", mission_id, 0),),
        payload={"mission_id": mission_id, "objective_digest": request_digest},
        validator=validator,
        projector=projector,
        clock=clock,
    )
    if _operation_was_completed(outcome):
        mission_dir = Path(missions_root).resolve() / mission_id
        _write_text(mission_dir / "intake" / "human-request.md", request_text)
        _write_text(
            mission_dir / "intake" / "status.md",
            "# Intake status\n\nPhase: awaiting_evaluable_contract\n",
        )
        _write_text(
            mission_dir / "README.md",
            (
                f"# Mission {mission_id}\n\n"
                "Raw human request is under `intake/human-request.md`.\n"
                "Propose an evaluable contract before authorization.\n"
            ),
        )
    return outcome


def propose_contract(
    root: Path,
    missions_root: Path,
    mission_id: str,
    *,
    contract_path: Path,
    operation_id: str,
    expected_revision: int,
    liaison_session_id: str,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    """Validate and publish a proposed contract without leaving draft.

    Proposal success writes State Tree files and a nonreserved observation. It does
    not bump the mission revision; authorization still expects the post-intake
    revision. Failed evaluation is a journaled gate rejection.
    """

    contract_text = Path(contract_path).read_text(encoding="utf-8")
    errors = list(contract_evaluation_errors(contract_text))
    mission_dir = Path(missions_root).resolve() / mission_id
    if unresolved_intake_questions(mission_dir):
        errors.append("intake_open_questions_unresolved")
    contract_hash = _sha256_bytes(contract_text.encode("utf-8"))
    observed_revision = _mission_revision(root, mission_id)
    if observed_revision is None:
        raise InvalidOperation("mission missing")
    if observed_revision != expected_revision:
        raise InvalidOperation(
            f"mission revision mismatch: expected {expected_revision}, observed {observed_revision}"
        )

    request = OperationRequest(
        operation_id=operation_id,
        action="mission_controls.propose_contract",
        invoker=Invoker(kind="liaison", id=liaison_session_id),
        expected_revisions=(ExpectedRevision("mission", mission_id, expected_revision),),
        payload={"contract_hash": contract_hash, "mission_id": mission_id},
    )

    def validator(context: _ValidationContext) -> GateRejection | None:
        row = context.db.execute(
            "SELECT state FROM missions WHERE id=?",
            (mission_id,),
        ).fetchone()
        if row is None:
            return GateRejection("mission_missing", {"mission_id": mission_id})
        if str(row[0]) != MissionState.DRAFT:
            return GateRejection(
                "mission_state_mismatch",
                {
                    "expected": str(MissionState.DRAFT),
                    "mission_id": mission_id,
                    "observed": str(row[0]),
                },
            )
        if errors:
            return GateRejection(
                "contract_not_evaluable",
                {"errors": errors, "mission_id": mission_id},
            )
        return None

    if errors:

        def noop_projector(_context: _ProjectionContext) -> None:
            return None

        return _run_governed(
            root,
            request,
            validator=validator,
            projector=noop_projector,
            clock=clock,
        )

    # Success: confirm draft, write State Tree, journal a nonreserved observation.
    with sqlite3.connect(Path(root).resolve() / "aos.db") as db:
        row = db.execute("SELECT state FROM missions WHERE id=?", (mission_id,)).fetchone()
        if row is None or str(row[0]) != MissionState.DRAFT:
            raise InvalidOperation("mission is not draft")

    _write_text(mission_dir / "contract" / "proposed.md", contract_text)
    _write_text(
        mission_dir / "intake" / "status.md",
        "# Intake status\n\nPhase: contract_proposed\n"
        f"Contract-hash: {contract_hash}\n",
    )
    return append_observation(
        root,
        OperationRequest(
            operation_id=operation_id,
            action="journal.append_observation",
            invoker=Invoker(kind="liaison", id=liaison_session_id),
            expected_revisions=(),
            payload={
                "aggregate_id": mission_id,
                "aggregate_type": "mission",
                "kind": "mission.contract_proposed",
                "payload": {
                    "contract_hash": contract_hash,
                    "mission_id": mission_id,
                },
                "severity": "info",
            },
        ),
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
    mission_dir = Path(missions_root).resolve() / mission_id
    proposed_path = mission_dir / "contract" / "proposed.md"
    if not proposed_path.is_file():
        raise InvalidOperation("proposed contract is missing; propose-contract first")
    contract_text = proposed_path.read_text(encoding="utf-8")
    errors = list(contract_evaluation_errors(contract_text))
    if unresolved_intake_questions(mission_dir):
        errors.append("intake_open_questions_unresolved")
    contract_hash = _sha256_bytes(contract_text.encode("utf-8"))
    contract_id = f"CR-{mission_id}"
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

    def validator(context: _ValidationContext) -> GateRejection | None:
        if errors:
            return GateRejection(
                "contract_not_evaluable",
                {"errors": errors, "mission_id": mission_id},
            )
        rejection = _mission_state_rejection(
            context,
            mission_id=mission_id,
            source=MissionState.DRAFT,
            target=MissionState.AUTHORIZED,
            protocol_context=protocol_context,
        )
        if rejection is not None:
            return rejection
        existing = context.db.execute(
            "SELECT 1 FROM contract_revisions WHERE id=?",
            (contract_id,),
        ).fetchone()
        if existing is not None:
            return GateRejection("contract_exists", {"contract_id": contract_id})
        return None

    def projector(context: _ProjectionContext) -> None:
        contract_event = context.append_event(
            "contract.authorized",
            "contract_revision",
            contract_id,
            {
                "contract_hash": contract_hash,
                "contract_revision_id": contract_id,
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
                contract_id,
                mission_id,
                contract_text,
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
        action="mission_controls.authorize",
        liaison_session_id=liaison_session_id,
        expected_revisions=(
            ExpectedRevision("mission", mission_id, expected_revision),
            ExpectedRevision("contract_revision", contract_id, 0),
        ),
        payload={
            "contract_hash": contract_hash,
            "decision_source_hash": decision_hash,
            "mission_id": mission_id,
        },
        validator=validator,
        projector=projector,
        decision=(principal, decision_hash),
        clock=clock,
    )
    if _operation_was_completed(outcome):
        authorized = contract_text if contract_text.endswith("\n") else f"{contract_text}\n"
        _write_text(mission_dir / "contract" / "authorized.md", authorized)
        _write_text(
            mission_dir / "intake" / "status.md",
            "# Intake status\n\nPhase: authorized\n"
            f"Contract-hash: {contract_hash}\n",
        )
    return outcome


def dispatch_mission(
    root: Path,
    missions_root: Path,
    mission_id: str,
    *,
    operation_id: str,
    expected_revision: int,
    liaison_session_id: str,
    work_order_path: Path,
    backend: str,
    model: str,
    workspace: str,
    worker_token: str,
    budget_seconds: int,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    """Admit one authored work order and a cooperative first-attempt membrane.

    This command records dispatch. It deliberately does not launch or supervise a
    worker; a harness adapter or resident liaison performs that external action
    only after this operation is admitted. The learning slice records the existing
    working tree honestly; physical worktree isolation remains later M2 work.
    """

    for label, value in (
        ("backend", backend),
        ("model", model),
        ("workspace", workspace),
    ):
        if not value or value != value.strip():
            raise InvalidOperation(f"{label} is required")
    workspace_path = Path(workspace)
    if workspace_path.is_absolute() or ".." in workspace_path.parts:
        raise InvalidOperation("workspace must be a logical relative locator")
    if not isinstance(budget_seconds, int) or not 1 <= budget_seconds <= 86_400:
        raise InvalidOperation("budget_seconds must be between 1 and 86400")

    work_order_text = Path(work_order_path).read_text(encoding="utf-8")
    if not work_order_text.strip():
        raise InvalidOperation("work order is empty")
    if _contains_unsafe_content(work_order_text.encode("utf-8")):
        raise InvalidOperation("work order contains unsafe content")
    work_order_hash = _sha256_bytes(work_order_text.encode("utf-8"))

    attempt_number = 1
    attempt_id = f"A-{mission_id}-{attempt_number:04d}"
    process_id = f"P-{mission_id}-{attempt_number:04d}"
    work_order_id = f"WO-{mission_id}-{attempt_number:04d}"
    worker_session_id = f"S-{mission_id}-{attempt_number:04d}"
    authority_epoch_id = f"AE-{mission_id}-{attempt_number:04d}"
    scope = (
        f"mission:{mission_id}:attempt:{attempt_id}:"
        f"process:{process_id}"
    )
    token_hash = capability_digest(
        worker_token,
        kind="worker",
        subject_id=process_id,
        scope=scope,
    )

    with sqlite3.connect(Path(root).resolve() / "aos.db") as db:
        contract = db.execute(
            """
            SELECT id, content_hash, attempt_ceiling
            FROM contract_revisions
            WHERE mission_id=?
            ORDER BY revision DESC
            LIMIT 1
            """,
            (mission_id,),
        ).fetchone()
    if contract is None:
        raise InvalidOperation("authorized contract is missing")

    expected_revisions = (
        ExpectedRevision("mission", mission_id, expected_revision),
        ExpectedRevision("contract_revision", str(contract[0]), 1, role="read"),
        ExpectedRevision("attempt", attempt_id, 0),
        ExpectedRevision("authority_epoch", authority_epoch_id, 0),
        ExpectedRevision("process", process_id, 0),
        ExpectedRevision("work_order", work_order_id, 0),
        ExpectedRevision("worker_session", worker_session_id, 0),
    )

    def validator(context: _ValidationContext) -> GateRejection | None:
        rejection = _mission_state_rejection(
            context,
            mission_id=mission_id,
            source=MissionState.AUTHORIZED,
            target=MissionState.DISPATCHED,
        )
        if rejection is not None:
            return rejection
        attempts_created = int(
            context.db.execute(
                "SELECT COUNT(*) FROM attempts WHERE mission_id=?",
                (mission_id,),
            ).fetchone()[0]
        )
        if attempts_created >= int(contract[2]):
            return GateRejection(
                "attempt_ceiling_exhausted",
                {
                    "attempt_ceiling": int(contract[2]),
                    "attempts_created": attempts_created,
                    "mission_id": mission_id,
                },
            )
        return None

    def projector(context: _ProjectionContext) -> None:
        expires_at = (
            datetime.fromisoformat(context.occurred_at) + timedelta(seconds=budget_seconds)
        ).isoformat()
        context.append_event(
            "attempt.created",
            "attempt",
            attempt_id,
            {
                "attempt_id": attempt_id,
                "isolation": "cooperative_existing_working_tree",
                "mission_id": mission_id,
                "number": attempt_number,
                "workspace": workspace,
            },
        )
        context.db.execute(
            """
            INSERT INTO attempts(
                id, mission_id, number, state, worktree, branch, created_at, updated_at
            ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (
                attempt_id,
                mission_id,
                attempt_number,
                workspace,
                "existing-working-tree",
                context.occurred_at,
                context.occurred_at,
            ),
        )

        authority_event = context.append_event(
            "authority.claimed",
            "authority_epoch",
            authority_epoch_id,
            {
                "epoch_id": authority_epoch_id,
                "expires_at": expires_at,
                "kind": "worker",
                "scope": scope,
                "subject_id": process_id,
            },
        )
        context.db.execute(
            """
            INSERT INTO authority_epochs(
                epoch_id, kind, subject_id, scope, token_hash, issued_at,
                expires_at, status, issued_event_id
            ) VALUES (?, 'worker', ?, ?, ?, ?, ?, 'active', ?)
            """,
            (
                authority_epoch_id,
                process_id,
                scope,
                token_hash,
                context.occurred_at,
                expires_at,
                authority_event.event_id,  # type: ignore[attr-defined]
            ),
        )

        context.append_event(
            "process.created",
            "process",
            process_id,
            {
                "attempt_id": attempt_id,
                "backend": backend,
                "mission_id": mission_id,
                "model": model,
                "process_id": process_id,
            },
        )
        context.db.execute(
            """
            INSERT INTO processes(
                id, mission_id, attempt, backend, model, state, token_hash,
                token_expires_at, worktree, session_id, checkpoint,
                routing_reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'created', ?, ?, ?, ?, '{}', ?, ?, ?)
            """,
            (
                process_id,
                mission_id,
                attempt_number,
                backend,
                model,
                token_hash,
                expires_at,
                workspace,
                worker_session_id,
                (
                    f"liaison selected {backend}/{model} for the authorized "
                    f"{budget_seconds}-second learning-mission slice"
                ),
                context.occurred_at,
                context.occurred_at,
            ),
        )

        context.append_event(
            "work_order.created",
            "work_order",
            work_order_id,
            {
                "contract_hash": str(contract[1]),
                "contract_revision_id": str(contract[0]),
                "content_hash": work_order_hash,
                "mission_id": mission_id,
                "process_id": process_id,
                "work_order_id": work_order_id,
            },
        )
        context.db.execute(
            """
            INSERT INTO work_orders(
                id, mission_id, process_id, revision, content, content_hash, created_at
            ) VALUES (?, ?, ?, 1, ?, ?, ?)
            """,
            (
                work_order_id,
                mission_id,
                process_id,
                work_order_text,
                work_order_hash,
                context.occurred_at,
            ),
        )

        context.append_event(
            "worker_session.created",
            "worker_session",
            worker_session_id,
            {
                "process_id": process_id,
                "sequence": 1,
                "session_id": worker_session_id,
            },
        )
        context.db.execute(
            """
            INSERT INTO worker_sessions(
                id, process_id, sequence, state, backend_handle,
                metadata, started_at, ended_at
            ) VALUES (?, ?, 1, 'created', NULL, ?, ?, NULL)
            """,
            (
                worker_session_id,
                process_id,
                json.dumps(
                    {
                        "backend": backend,
                        "budget_seconds": budget_seconds,
                        "model": model,
                        "workspace": workspace,
                    },
                    sort_keys=True,
                ),
                context.occurred_at,
            ),
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
        action="mission_controls.dispatch",
        liaison_session_id=liaison_session_id,
        expected_revisions=expected_revisions,
        payload={
            "authority_epoch_id": authority_epoch_id,
            "backend": backend,
            "budget_seconds": budget_seconds,
            "contract_hash": str(contract[1]),
            "mission_id": mission_id,
            "model": model,
            "token_hash": token_hash,
            "workspace": workspace,
            "work_order_hash": work_order_hash,
        },
        validator=validator,
        projector=projector,
        clock=clock,
    )
    if _operation_was_completed(outcome):
        attempt_dir = (
            Path(missions_root).resolve()
            / mission_id
            / "attempts"
            / f"{attempt_number:04d}"
        )
        _write_json(
            attempt_dir / "attempt.json",
            {
                "attempt_id": attempt_id,
                "authority_epoch_id": authority_epoch_id,
                "budget_seconds": budget_seconds,
                "isolation": "cooperative_existing_working_tree",
                "mission_id": mission_id,
                "number": attempt_number,
                "process_id": process_id,
                "worker_session_id": worker_session_id,
                "workspace": workspace,
            },
        )
        _write_text(
            attempt_dir / "route.md",
            (
                f"# Admitted route — {attempt_id}\n\n"
                f"- Harness: {backend}\n"
                f"- Model: {model}\n"
                f"- Budget-seconds: {budget_seconds}\n"
                f"- Contract: {contract[0]} ({contract[1]})\n"
                "- Isolation: cooperative existing working tree\n"
                f"- Workspace: {workspace}\n"
                f"- Work-order-hash: {work_order_hash}\n"
            ),
        )
        notes_path = attempt_dir / "notes.md"
        if not notes_path.exists():
            _write_text(
                notes_path,
                (
                    f"# Worker notes — {attempt_id}\n\n"
                    f"- Harness: {backend}\n"
                    f"- Model: {model}\n"
                    "- Elapsed-seconds: pending\n"
                    "- Outcome: pending\n"
                ),
            )
        _write_text(
            attempt_dir
            / "processes"
            / process_id
            / "work-orders"
            / "0001.md",
            work_order_text,
        )
        _write_json(
            attempt_dir
            / "processes"
            / process_id
            / "sessions"
            / worker_session_id
            / "session.json",
            {
                "process_id": process_id,
                "sequence": 1,
                "state": "created",
                "worker_session_id": worker_session_id,
            },
        )
    return outcome


def replace_worker(
    root: Path,
    missions_root: Path,
    mission_id: str,
    *,
    operation_id: str,
    expected_revision: int,
    liaison_session_id: str,
    worker_token: str,
    budget_seconds: int,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    """Rotate a stalled worker's session and authority without changing its work."""

    if not isinstance(budget_seconds, int) or not 1 <= budget_seconds <= 86_400:
        raise InvalidOperation("budget_seconds must be between 1 and 86400")
    replay = _recorded_replacement_replay(
        root,
        operation_id=operation_id,
        mission_id=mission_id,
        expected_revision=expected_revision,
        worker_token=worker_token,
        budget_seconds=budget_seconds,
    )
    if replay is not None:
        return replay

    previous = _current_worker_membrane(root, mission_id)
    with sqlite3.connect(Path(root).resolve() / "aos.db") as db:
        next_sequence = int(
            db.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) + 1
                FROM worker_sessions
                WHERE process_id=?
                """,
                (previous.process_id,),
            ).fetchone()[0]
        )
    worker_session_id = f"S-{mission_id}-{next_sequence:04d}"
    authority_epoch_id = f"AE-{mission_id}-{next_sequence:04d}"
    scope = (
        f"mission:{mission_id}:attempt:{previous.attempt_id}:"
        f"process:{previous.process_id}"
    )
    token_hash = capability_digest(
        worker_token,
        kind="worker",
        subject_id=previous.process_id,
        scope=scope,
    )
    expected_revisions = (
        ExpectedRevision("mission", mission_id, expected_revision, role="read"),
        ExpectedRevision(
            "attempt",
            previous.attempt_id,
            _aggregate_revision(root, "attempt", previous.attempt_id),
            role="read",
        ),
        ExpectedRevision(
            "process",
            previous.process_id,
            _aggregate_revision(root, "process", previous.process_id),
        ),
        ExpectedRevision(
            "work_order",
            previous.work_order_id,
            _aggregate_revision(root, "work_order", previous.work_order_id),
            role="read",
        ),
        ExpectedRevision(
            "worker_session",
            previous.session_id,
            _aggregate_revision(root, "worker_session", previous.session_id),
        ),
        ExpectedRevision(
            "authority_epoch",
            previous.authority_epoch_id,
            _aggregate_revision(root, "authority_epoch", previous.authority_epoch_id),
        ),
        ExpectedRevision("worker_session", worker_session_id, 0),
        ExpectedRevision("authority_epoch", authority_epoch_id, 0),
    )
    recovery = ProcessRecovery(
        retry_kind=RetryKind.SESSION,
        previous_authority_epoch_id=previous.authority_epoch_id,
        new_authority_epoch_id=authority_epoch_id,
        same_process=True,
        same_attempt=True,
        same_worktree=True,
        creates_new_worker_session=True,
    )

    def validator(context: _ValidationContext) -> GateRejection | None:
        mission = context.db.execute(
            "SELECT state FROM missions WHERE id=?",
            (mission_id,),
        ).fetchone()
        attempt = context.db.execute(
            "SELECT state FROM attempts WHERE id=?",
            (previous.attempt_id,),
        ).fetchone()
        process = context.db.execute(
            "SELECT state, session_id FROM processes WHERE id=?",
            (previous.process_id,),
        ).fetchone()
        session = context.db.execute(
            "SELECT state FROM worker_sessions WHERE id=?",
            (previous.session_id,),
        ).fetchone()
        epoch = context.db.execute(
            "SELECT status FROM authority_epochs WHERE epoch_id=?",
            (previous.authority_epoch_id,),
        ).fetchone()
        if None in (mission, attempt, process, session, epoch):
            return GateRejection("worker_membrane_missing", {"mission_id": mission_id})
        if budget_seconds > previous.budget_seconds:
            return GateRejection(
                "replacement_budget_expanded",
                {
                    "admitted_budget_seconds": previous.budget_seconds,
                    "requested_budget_seconds": budget_seconds,
                },
            )
        if (
            str(mission[0]) != str(MissionState.IMPLEMENTING)
            or str(attempt[0]) != "active"
            or str(process[0]) != "running"
            or str(process[1]) != previous.session_id
            or str(session[0]) != "running"
            or str(epoch[0]) != "active"
        ):
            return GateRejection(
                "worker_membrane_not_replaceable",
                {"mission_id": mission_id, "process_id": previous.process_id},
            )
        try:
            require_transition(
                AggregateType.WORKER_SESSION,
                "running",
                "orphaned",
            )
            require_transition(
                AggregateType.PROCESS,
                "running",
                "created",
                context=recovery,
            )
        except ProtocolViolation:
            return GateRejection("protocol_rejected", {"phase": "replace_worker"})
        return None

    def projector(context: _ProjectionContext) -> None:
        context.append_event(
            "worker_session.transitioned",
            "worker_session",
            previous.session_id,
            {
                "from_state": "running",
                "reason": "stalled_worker_replaced",
                "replacement_session_id": worker_session_id,
                "to_state": "orphaned",
            },
        )
        context.db.execute(
            """
            UPDATE worker_sessions
            SET state='orphaned', ended_at=?
            WHERE id=?
            """,
            (context.occurred_at, previous.session_id),
        )

        expired = datetime.fromisoformat(
            previous.authority_expires_at.replace("Z", "+00:00")
        ) <= datetime.fromisoformat(context.occurred_at)
        authority_status = "expired" if expired else "revoked"
        authority_kind = "authority.expired" if expired else "authority.revoked"
        closed = context.append_event(
            authority_kind,
            "authority_epoch",
            previous.authority_epoch_id,
            {
                "epoch_id": previous.authority_epoch_id,
                "reason": "stalled_worker_replaced",
                "replacement_epoch_id": authority_epoch_id,
                "subject_id": previous.process_id,
            },
        )
        context.db.execute(
            """
            UPDATE authority_epochs
            SET status=?, closed_at=?, closed_event_id=?, close_reason=?
            WHERE epoch_id=?
            """,
            (
                authority_status,
                context.occurred_at,
                closed.event_id,  # type: ignore[attr-defined]
                "stalled_worker_replaced",
                previous.authority_epoch_id,
            ),
        )

        expires_at = (
            datetime.fromisoformat(context.occurred_at)
            + timedelta(seconds=budget_seconds)
        ).isoformat()
        claimed = context.append_event(
            "authority.claimed",
            "authority_epoch",
            authority_epoch_id,
            {
                "epoch_id": authority_epoch_id,
                "expires_at": expires_at,
                "kind": "worker",
                "scope": scope,
                "subject_id": previous.process_id,
            },
        )
        context.db.execute(
            """
            INSERT INTO authority_epochs(
                epoch_id, kind, subject_id, scope, token_hash, issued_at,
                expires_at, status, issued_event_id
            ) VALUES (?, 'worker', ?, ?, ?, ?, ?, 'active', ?)
            """,
            (
                authority_epoch_id,
                previous.process_id,
                scope,
                token_hash,
                context.occurred_at,
                expires_at,
                claimed.event_id,  # type: ignore[attr-defined]
            ),
        )
        context.append_event(
            "worker_session.created",
            "worker_session",
            worker_session_id,
            {
                "process_id": previous.process_id,
                "replaces_session_id": previous.session_id,
                "sequence": next_sequence,
                "session_id": worker_session_id,
            },
        )
        context.db.execute(
            """
            INSERT INTO worker_sessions(
                id, process_id, sequence, state, backend_handle,
                metadata, started_at, ended_at
            ) VALUES (?, ?, ?, 'created', NULL, ?, ?, NULL)
            """,
            (
                worker_session_id,
                previous.process_id,
                next_sequence,
                json.dumps(
                    {
                        "backend": previous.backend,
                        "budget_seconds": budget_seconds,
                        "model": previous.model,
                        "replaces_session_id": previous.session_id,
                        "workspace": previous.workspace,
                    },
                    sort_keys=True,
                ),
                context.occurred_at,
            ),
        )
        context.append_event(
            "process.transitioned",
            "process",
            previous.process_id,
            {
                "from_state": "running",
                "new_authority_epoch_id": authority_epoch_id,
                "new_session_id": worker_session_id,
                "previous_authority_epoch_id": previous.authority_epoch_id,
                "reason": "session_recovery",
                "to_state": "created",
            },
        )
        context.db.execute(
            """
            UPDATE processes
            SET state='created', session_id=?, token_hash=?,
                token_expires_at=?, updated_at=?
            WHERE id=?
            """,
            (
                worker_session_id,
                token_hash,
                expires_at,
                context.occurred_at,
                previous.process_id,
            ),
        )

    outcome = _liaison_operation(
        root,
        operation_id=operation_id,
        action="mission_controls.replace_worker",
        liaison_session_id=liaison_session_id,
        expected_revisions=expected_revisions,
        payload={
            "budget_seconds": budget_seconds,
            "mission_id": mission_id,
            "new_authority_epoch_id": authority_epoch_id,
            "new_session_id": worker_session_id,
            "previous_authority_epoch_id": previous.authority_epoch_id,
            "previous_session_id": previous.session_id,
            "process_id": previous.process_id,
            "token_hash": token_hash,
        },
        validator=validator,
        projector=projector,
        clock=clock,
    )
    if outcome.status == "completed":
        attempt_dir = (
            Path(missions_root).resolve()
            / mission_id
            / "attempts"
            / f"{previous.attempt_number:04d}"
        )
        session_root = (
            attempt_dir / "processes" / previous.process_id / "sessions"
        )
        _write_json(
            session_root / previous.session_id / "session.json",
            {
                "process_id": previous.process_id,
                "reason": "stalled_worker_replaced",
                "replacement_session_id": worker_session_id,
                "sequence": previous.session_sequence,
                "state": "orphaned",
                "worker_session_id": previous.session_id,
            },
        )
        _write_json(
            session_root / worker_session_id / "session.json",
            {
                "process_id": previous.process_id,
                "replaces_session_id": previous.session_id,
                "sequence": next_sequence,
                "state": "created",
                "worker_session_id": worker_session_id,
            },
        )
        attempt_metadata_path = attempt_dir / "attempt.json"
        attempt_metadata = _json_file(attempt_metadata_path)
        attempt_metadata.update(
            {
                "authority_epoch_id": authority_epoch_id,
                "recovery_count": int(attempt_metadata.get("recovery_count", 0)) + 1,
                "worker_session_id": worker_session_id,
            }
        )
        _write_json(attempt_metadata_path, attempt_metadata)
    return outcome

def start_mission(
    root: Path,
    missions_root: Path,
    mission_id: str,
    *,
    operation_id: str,
    expected_revision: int,
    liaison_session_id: str,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    """Mark the current admitted worker session running before external execution."""

    replay = _recorded_liaison_replay(
        root,
        operation_id=operation_id,
        action="mission_controls.start",
        mission_id=mission_id,
        expected_revision=expected_revision,
    )
    if replay is not None:
        return replay
    membrane = _current_worker_membrane(root, mission_id)
    with sqlite3.connect(Path(root).resolve() / "aos.db") as db:
        mission = db.execute(
            "SELECT state FROM missions WHERE id=?",
            (mission_id,),
        ).fetchone()
    if mission is None:
        raise InvalidOperation(f"mission {mission_id} is missing")
    mission_state = str(mission[0])
    if mission_state not in {
        str(MissionState.DISPATCHED),
        str(MissionState.IMPLEMENTING),
    }:
        raise InvalidOperation("mission is not ready to start a worker")
    initial_start = mission_state == str(MissionState.DISPATCHED)
    expected_revisions = (
        ExpectedRevision(
            "mission",
            mission_id,
            expected_revision,
            role="write" if initial_start else "read",
        ),
        ExpectedRevision(
            "process",
            membrane.process_id,
            _aggregate_revision(root, "process", membrane.process_id),
        ),
        ExpectedRevision(
            "worker_session",
            membrane.session_id,
            _aggregate_revision(root, "worker_session", membrane.session_id),
        ),
        ExpectedRevision(
            "attempt",
            membrane.attempt_id,
            _aggregate_revision(root, "attempt", membrane.attempt_id),
            role="read",
        ),
        ExpectedRevision(
            "work_order",
            membrane.work_order_id,
            _aggregate_revision(root, "work_order", membrane.work_order_id),
            role="read",
        ),
        ExpectedRevision(
            "authority_epoch",
            membrane.authority_epoch_id,
            _aggregate_revision(root, "authority_epoch", membrane.authority_epoch_id),
            role="read",
        ),
    )

    def validator(context: _ValidationContext) -> GateRejection | None:
        observed_mission = context.db.execute(
            "SELECT state FROM missions WHERE id=?",
            (mission_id,),
        ).fetchone()
        if observed_mission is None or str(observed_mission[0]) != mission_state:
            return GateRejection(
                "mission_state_mismatch",
                {
                    "expected": mission_state,
                    "mission_id": mission_id,
                    "observed": None if observed_mission is None else str(observed_mission[0]),
                },
            )
        if initial_start:
            rejection = _mission_state_rejection(
                context,
                mission_id=mission_id,
                source=MissionState.DISPATCHED,
                target=MissionState.IMPLEMENTING,
            )
            if rejection is not None:
                return rejection
        process = context.db.execute(
            "SELECT state, session_id FROM processes WHERE id=?",
            (membrane.process_id,),
        ).fetchone()
        session = context.db.execute(
            "SELECT state FROM worker_sessions WHERE id=?",
            (membrane.session_id,),
        ).fetchone()
        epoch = context.db.execute(
            "SELECT status, expires_at FROM authority_epochs WHERE epoch_id=?",
            (membrane.authority_epoch_id,),
        ).fetchone()
        if process is None or session is None or epoch is None:
            return GateRejection("worker_membrane_missing", {"mission_id": mission_id})
        if str(process[1]) != membrane.session_id:
            return GateRejection("worker_membrane_changed", {"mission_id": mission_id})
        if str(epoch[0]) != "active":
            return GateRejection(
                "worker_authority_inactive",
                {"authority_epoch_id": membrane.authority_epoch_id},
            )
        if datetime.fromisoformat(str(epoch[1])) <= context.occurred_at:
            return GateRejection(
                "worker_authority_expired",
                {"authority_epoch_id": membrane.authority_epoch_id},
            )
        try:
            require_transition(AggregateType.PROCESS, str(process[0]), "running")
            require_transition(
                AggregateType.WORKER_SESSION,
                str(session[0]),
                "running",
            )
        except ProtocolViolation:
            return GateRejection("protocol_rejected", {"phase": "start"})
        return None

    def projector(context: _ProjectionContext) -> None:
        context.append_event(
            "process.transitioned",
            "process",
            membrane.process_id,
            {"from_state": "created", "to_state": "running"},
        )
        context.db.execute(
            "UPDATE processes SET state='running', updated_at=? WHERE id=?",
            (context.occurred_at, membrane.process_id),
        )
        context.append_event(
            "worker_session.transitioned",
            "worker_session",
            membrane.session_id,
            {"from_state": "created", "to_state": "running"},
        )
        context.db.execute(
            """
            UPDATE worker_sessions
            SET state='running', started_at=?
            WHERE id=?
            """,
            (context.occurred_at, membrane.session_id),
        )
        if initial_start:
            context.append_event(
                "mission.transitioned",
                "mission",
                mission_id,
                {"from_state": "dispatched", "to_state": "implementing"},
            )
            context.db.execute(
                "UPDATE missions SET state='implementing', updated_at=? WHERE id=?",
                (context.occurred_at, mission_id),
            )

    outcome = _liaison_operation(
        root,
        operation_id=operation_id,
        action="mission_controls.start",
        liaison_session_id=liaison_session_id,
        expected_revisions=expected_revisions,
        payload={
            "authority_epoch_id": membrane.authority_epoch_id,
            "mission_id": mission_id,
            "process_id": membrane.process_id,
            "worker_session_id": membrane.session_id,
        },
        validator=validator,
        projector=projector,
        clock=clock,
    )
    if outcome.status == "completed":
        _write_json(
            Path(missions_root).resolve()
            / mission_id
            / "attempts"
            / f"{membrane.attempt_number:04d}"
            / "processes"
            / membrane.process_id
            / "sessions"
            / membrane.session_id
            / "session.json",
            {
                "process_id": membrane.process_id,
                "sequence": membrane.session_sequence,
                "state": "running",
                "worker_session_id": membrane.session_id,
            },
        )
    return outcome

def finish_mission(
    root: Path,
    missions_root: Path,
    mission_id: str,
    *,
    operation_id: str,
    expected_revision: int,
    liaison_session_id: str,
    receipt_path: Path,
    clock: Callable[[], datetime] = utc_now,
) -> OperationOutcome:
    """Admit a bound worker receipt and close the running attempt for verification."""

    receipt_file = Path(receipt_path)
    receipt_bytes = receipt_file.read_bytes()
    receipt_digest = _sha256_bytes(receipt_bytes)
    replay = _recorded_liaison_replay(
        root,
        operation_id=operation_id,
        action="mission_controls.finish",
        mission_id=mission_id,
        expected_revision=expected_revision,
        event_payload={"receipt_digest": receipt_digest},
    )
    if replay is not None:
        return replay
    membrane = _current_worker_membrane(root, mission_id)
    attempt_id = membrane.attempt_id
    process_id = membrane.process_id
    work_order_id = membrane.work_order_id
    worker_session_id = membrane.session_id
    authority_epoch_id = membrane.authority_epoch_id
    try:
        receipt = json.loads(receipt_bytes)
    except json.JSONDecodeError as exc:
        raise InvalidOperation("worker receipt must be valid JSON") from exc
    if not isinstance(receipt, dict):
        raise InvalidOperation("worker receipt must be a JSON object")
    expected_receipt = {
        "attempt_id": attempt_id,
        "authority_epoch_id": authority_epoch_id,
        "mission_id": mission_id,
        "process_id": process_id,
        "schema_version": 1,
        "status": "completed",
        "work_order_id": work_order_id,
        "worker_session_id": worker_session_id,
    }
    if set(receipt) != set(expected_receipt) or any(
        receipt.get(key) != value for key, value in expected_receipt.items()
    ):
        raise InvalidOperation("worker receipt does not match the dispatched membrane")

    attempt_dir = (
        Path(missions_root).resolve()
        / mission_id
        / "attempts"
        / f"{membrane.attempt_number:04d}"
    )
    notes_path = attempt_dir / "notes.md"
    if not notes_path.is_file():
        raise InvalidOperation("worker notes are missing")
    notes_text = notes_path.read_text(encoding="utf-8")
    notes = {key: value for key, value in WORKER_NOTE.findall(notes_text)}
    required_notes = {"Harness", "Model", "Elapsed-seconds", "Outcome"}
    if not required_notes.issubset(notes):
        raise InvalidOperation("worker notes are incomplete")
    try:
        elapsed_seconds = int(notes["Elapsed-seconds"])
    except ValueError as exc:
        raise InvalidOperation("worker elapsed seconds must be an integer") from exc
    if elapsed_seconds < 0 or notes["Outcome"] != "completed":
        raise InvalidOperation("worker notes do not report completed work")

    with sqlite3.connect(Path(root).resolve() / "aos.db") as db:
        route = db.execute(
            """
            SELECT backend, model
            FROM processes
            WHERE id=? AND mission_id=?
            """,
            (process_id, mission_id),
        ).fetchone()
        session_metadata = db.execute(
            "SELECT metadata FROM worker_sessions WHERE id=?",
            (worker_session_id,),
        ).fetchone()
    if route is None or session_metadata is None:
        raise InvalidOperation("dispatched membrane is missing")
    if notes["Harness"] != str(route[0]) or notes["Model"] != str(route[1]):
        raise InvalidOperation("worker notes do not match the admitted route")
    metadata = json.loads(str(session_metadata[0]))
    budget_seconds = int(metadata["budget_seconds"])
    notes_digest = _sha256_bytes(notes_text.encode("utf-8"))

    expected_revisions = (
        ExpectedRevision("mission", mission_id, expected_revision),
        ExpectedRevision(
            "attempt",
            attempt_id,
            _aggregate_revision(root, "attempt", attempt_id),
        ),
        ExpectedRevision(
            "process",
            process_id,
            _aggregate_revision(root, "process", process_id),
        ),
        ExpectedRevision(
            "worker_session",
            worker_session_id,
            _aggregate_revision(root, "worker_session", worker_session_id),
        ),
        ExpectedRevision(
            "authority_epoch",
            authority_epoch_id,
            _aggregate_revision(root, "authority_epoch", authority_epoch_id),
        ),
        ExpectedRevision(
            "work_order",
            work_order_id,
            _aggregate_revision(root, "work_order", work_order_id),
            role="read",
        ),
    )

    def validator(context: _ValidationContext) -> GateRejection | None:
        rejection = _mission_state_rejection(
            context,
            mission_id=mission_id,
            source=MissionState.IMPLEMENTING,
            target=MissionState.VERIFYING,
        )
        if rejection is not None:
            return rejection
        process = context.db.execute(
            "SELECT state FROM processes WHERE id=?",
            (process_id,),
        ).fetchone()
        session = context.db.execute(
            "SELECT state FROM worker_sessions WHERE id=?",
            (worker_session_id,),
        ).fetchone()
        attempt = context.db.execute(
            "SELECT state FROM attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        epoch = context.db.execute(
            "SELECT status, expires_at FROM authority_epochs WHERE epoch_id=?",
            (authority_epoch_id,),
        ).fetchone()
        if process is None or session is None or attempt is None or epoch is None:
            return GateRejection(
                "worker_membrane_missing",
                {"mission_id": mission_id},
            )
        if str(epoch[0]) != "active":
            return GateRejection(
                "worker_authority_inactive",
                {"authority_epoch_id": authority_epoch_id},
            )
        try:
            expires_at = datetime.fromisoformat(
                str(epoch[1]).replace("Z", "+00:00")
            )
        except ValueError:
            return GateRejection(
                "worker_authority_invalid",
                {"authority_epoch_id": authority_epoch_id},
            )
        if context.occurred_at >= expires_at:
            return GateRejection(
                "worker_authority_expired",
                {"authority_epoch_id": authority_epoch_id},
            )
        try:
            require_transition(
                AggregateType.PROCESS,
                str(process[0]),
                "completed",
            )
            require_transition(
                AggregateType.WORKER_SESSION,
                str(session[0]),
                "completed",
            )
            require_transition(
                AggregateType.ATTEMPT,
                str(attempt[0]),
                "completed",
            )
        except ProtocolViolation:
            return GateRejection(
                "protocol_rejected",
                {"phase": "finish"},
            )
        return None

    def projector(context: _ProjectionContext) -> None:
        completion = {
            "authority_epoch_id": authority_epoch_id,
            "elapsed_seconds": elapsed_seconds,
            "notes_digest": notes_digest,
            "receipt_digest": receipt_digest,
            "within_budget": elapsed_seconds <= budget_seconds,
            "work_order_id": work_order_id,
        }
        context.append_event(
            "process.transitioned",
            "process",
            process_id,
            {"from_state": "running", "to_state": "completed", **completion},
        )
        context.db.execute(
            "UPDATE processes SET state='completed', updated_at=? WHERE id=?",
            (context.occurred_at, process_id),
        )
        context.append_event(
            "worker_session.transitioned",
            "worker_session",
            worker_session_id,
            {"from_state": "running", "to_state": "completed", **completion},
        )
        context.db.execute(
            """
            UPDATE worker_sessions
            SET state='completed', ended_at=?
            WHERE id=?
            """,
            (context.occurred_at, worker_session_id),
        )
        context.append_event(
            "attempt.transitioned",
            "attempt",
            attempt_id,
            {"from_state": "active", "to_state": "completed", **completion},
        )
        context.db.execute(
            "UPDATE attempts SET state='completed', updated_at=? WHERE id=?",
            (context.occurred_at, attempt_id),
        )
        release_event = context.append_event(
            "authority.released",
            "authority_epoch",
            authority_epoch_id,
            {
                "epoch_id": authority_epoch_id,
                "reason": "worker_completion_admitted",
                "subject_id": process_id,
            },
        )
        context.db.execute(
            """
            UPDATE authority_epochs
            SET status='released', closed_at=?, closed_event_id=?, close_reason=?
            WHERE epoch_id=?
            """,
            (
                context.occurred_at,
                release_event.event_id,  # type: ignore[attr-defined]
                "worker_completion_admitted",
                authority_epoch_id,
            ),
        )
        context.append_event(
            "mission.transitioned",
            "mission",
            mission_id,
            {
                "from_state": "implementing",
                "to_state": "verifying",
                **completion,
            },
        )
        context.db.execute(
            "UPDATE missions SET state='verifying', updated_at=? WHERE id=?",
            (context.occurred_at, mission_id),
        )

    outcome = _liaison_operation(
        root,
        operation_id=operation_id,
        action="mission_controls.finish",
        liaison_session_id=liaison_session_id,
        expected_revisions=expected_revisions,
        payload={
            "authority_epoch_id": authority_epoch_id,
            "elapsed_seconds": elapsed_seconds,
            "mission_id": mission_id,
            "notes_digest": notes_digest,
            "receipt_digest": receipt_digest,
            "within_budget": elapsed_seconds <= budget_seconds,
            "work_order_id": work_order_id,
        },
        validator=validator,
        projector=projector,
        clock=clock,
    )
    if _operation_was_completed(outcome):
        _write_text(attempt_dir / "receipt.json", receipt_bytes.decode("utf-8"))
        attempt_metadata_path = attempt_dir / "attempt.json"
        attempt_metadata = _json_file(attempt_metadata_path)
        attempt_metadata.update(
            {
                "elapsed_seconds": elapsed_seconds,
                "state": "completed",
                "within_budget": elapsed_seconds <= budget_seconds,
            }
        )
        _write_json(attempt_metadata_path, attempt_metadata)
        _write_json(
            attempt_dir
            / "processes"
            / process_id
            / "sessions"
            / worker_session_id
            / "session.json",
            {
                "elapsed_seconds": elapsed_seconds,
                "process_id": process_id,
                "sequence": membrane.session_sequence,
                "state": "completed",
                "worker_session_id": worker_session_id,
                "within_budget": elapsed_seconds <= budget_seconds,
            },
        )
    return outcome


def mission_status(root: Path, missions_root: Path, mission_id: str) -> dict[str, object]:
    db_path = Path(root).resolve() / "aos.db"
    with sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True) as db:
        row = db.execute(
            """
            SELECT mission.state, revision.revision
            FROM missions AS mission
            JOIN aggregate_revisions AS revision
              ON revision.aggregate_type='mission'
             AND revision.aggregate_id=mission.id
            WHERE mission.id=?
            """,
            (mission_id,),
        ).fetchone()
        attempt = db.execute(
            """
            SELECT id, number, state
            FROM attempts
            WHERE mission_id=?
            ORDER BY number DESC
            LIMIT 1
            """,
            (mission_id,),
        ).fetchone()
        process = db.execute(
            """
            SELECT id, state, session_id
            FROM processes
            WHERE mission_id=?
            ORDER BY attempt DESC
            LIMIT 1
            """,
            (mission_id,),
        ).fetchone()
        worker_session = (
            None
            if process is None
            else db.execute(
                "SELECT state FROM worker_sessions WHERE id=?",
                (str(process[2]),),
            ).fetchone()
        )
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
    mission_dir = Path(missions_root).resolve() / mission_id
    proposed = mission_dir / "contract" / "proposed.md"
    return {
        "attempt_id": None if attempt is None else str(attempt[0]),
        "attempt_state": None if attempt is None else str(attempt[2]),
        "evidence_digest": None if evidence is None else str(evidence[0]),
        "evidence_id": (
            None if evidence is None else f"EV-{mission_id}-0001"
        ),
        "has_proposed_contract": proposed.is_file(),
        "intake_open_questions": unresolved_intake_questions(mission_dir),
        "mission_id": mission_id,
        "process_id": None if process is None else str(process[0]),
        "process_state": None if process is None else str(process[1]),
        "proposed_contract_errors": (
            list(contract_evaluation_errors(proposed.read_text(encoding="utf-8")))
            if proposed.is_file()
            else ["missing_proposed_contract"]
        ),
        "revision": None if row is None else int(row[1]),
        "state": None if row is None else str(row[0]),
        "worker_session_id": None if process is None else str(process[2]),
        "worker_session_state": (
            None if worker_session is None else str(worker_session[0])
        ),
    }


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


def _root_from_env(value: str | None, environment_name: str) -> Path:
    selected = value or os.environ.get(environment_name)
    if not selected:
        raise InvalidOperation(f"{environment_name} is required")
    return Path(selected)


def _liaison_session_from_env(value: str | None) -> str:
    session_id = value or os.environ.get("AOS_LIAISON_SESSION_ID")
    if not session_id:
        raise InvalidOperation("AOS_LIAISON_SESSION_ID is required")
    return session_id


def _worker_token_from_env() -> str:
    worker_token = os.environ.get("AOS_WORKER_TOKEN")
    if not worker_token:
        raise InvalidOperation("AOS_WORKER_TOKEN is required")
    return worker_token


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aos-mission")
    parser.add_argument("--state-root")
    parser.add_argument("--missions-root")
    parser.add_argument("--liaison-session")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init")
    init.add_argument("--op", default="OP-MC-INIT")

    intake = subparsers.add_parser("intake")
    intake.add_argument("mission_id")
    intake.add_argument("--request", required=True)
    intake.add_argument("--op", required=True)

    propose = subparsers.add_parser("propose-contract")
    propose.add_argument("mission_id")
    propose.add_argument("--contract", required=True)
    propose.add_argument("--op", required=True)
    propose.add_argument("--expect", required=True, type=int)

    authorize = subparsers.add_parser("authorize")
    authorize.add_argument("mission_id")
    authorize.add_argument("--decision", required=True)
    authorize.add_argument("--op", required=True)
    authorize.add_argument("--expect", required=True, type=int)

    dispatch = subparsers.add_parser("dispatch")
    dispatch.add_argument("mission_id")
    dispatch.add_argument("--work-order", required=True)
    dispatch.add_argument("--backend", required=True)
    dispatch.add_argument("--model", required=True)
    dispatch.add_argument("--workspace", required=True)
    dispatch.add_argument("--budget-seconds", required=True, type=int)
    dispatch.add_argument("--op", required=True)
    dispatch.add_argument("--expect", required=True, type=int)

    start = subparsers.add_parser("start")
    start.add_argument("mission_id")
    start.add_argument("--op", required=True)
    start.add_argument("--expect", required=True, type=int)

    replace = subparsers.add_parser("replace-worker")
    replace.add_argument("mission_id")
    replace.add_argument("--budget-seconds", required=True, type=int)
    replace.add_argument("--op", required=True)
    replace.add_argument("--expect", required=True, type=int)

    finish = subparsers.add_parser("finish")
    finish.add_argument("mission_id")
    finish.add_argument("--receipt", required=True)
    finish.add_argument("--op", required=True)
    finish.add_argument("--expect", required=True, type=int)

    status = subparsers.add_parser("status")
    status.add_argument("mission_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        root = _root_from_env(args.state_root, "AOS_STATE_ROOT")
        missions_root = _root_from_env(args.missions_root, "AOS_MISSIONS_ROOT")
        if args.command == "init":
            result = initialize_controls(root, args.op)
        elif args.command == "intake":
            session_id = _liaison_session_from_env(args.liaison_session)
            result = _outcome_json(
                intake_mission(
                    root,
                    missions_root,
                    mission_id=args.mission_id,
                    human_request_path=Path(args.request),
                    operation_id=args.op,
                    liaison_session_id=session_id,
                )
            )
            if not _result_is_rejected(result):
                result.update(mission_status(root, missions_root, args.mission_id))
        elif args.command == "propose-contract":
            session_id = _liaison_session_from_env(args.liaison_session)
            result = _outcome_json(
                propose_contract(
                    root,
                    missions_root,
                    args.mission_id,
                    contract_path=Path(args.contract),
                    operation_id=args.op,
                    expected_revision=args.expect,
                    liaison_session_id=session_id,
                )
            )
            if not _result_is_rejected(result):
                result.update(mission_status(root, missions_root, args.mission_id))
        elif args.command == "authorize":
            session_id = _liaison_session_from_env(args.liaison_session)
            result = _outcome_json(
                authorize_mission(
                    root,
                    missions_root,
                    args.mission_id,
                    operation_id=args.op,
                    expected_revision=args.expect,
                    liaison_session_id=session_id,
                    decision_path=Path(args.decision),
                )
            )
            if not _result_is_rejected(result):
                result.update(mission_status(root, missions_root, args.mission_id))
        elif args.command == "dispatch":
            session_id = _liaison_session_from_env(args.liaison_session)
            result = _outcome_json(
                dispatch_mission(
                    root,
                    missions_root,
                    args.mission_id,
                    operation_id=args.op,
                    expected_revision=args.expect,
                    liaison_session_id=session_id,
                    work_order_path=Path(args.work_order),
                    backend=args.backend,
                    model=args.model,
                    workspace=args.workspace,
                    worker_token=_worker_token_from_env(),
                    budget_seconds=args.budget_seconds,
                )
            )
            if not _result_is_rejected(result):
                result.update(mission_status(root, missions_root, args.mission_id))
        elif args.command == "start":
            session_id = _liaison_session_from_env(args.liaison_session)
            result = _outcome_json(
                start_mission(
                    root,
                    missions_root,
                    args.mission_id,
                    operation_id=args.op,
                    expected_revision=args.expect,
                    liaison_session_id=session_id,
                )
            )
            if not _result_is_rejected(result):
                result.update(mission_status(root, missions_root, args.mission_id))
        elif args.command == "replace-worker":
            session_id = _liaison_session_from_env(args.liaison_session)
            result = _outcome_json(
                replace_worker(
                    root,
                    missions_root,
                    args.mission_id,
                    operation_id=args.op,
                    expected_revision=args.expect,
                    liaison_session_id=session_id,
                    worker_token=_worker_token_from_env(),
                    budget_seconds=args.budget_seconds,
                )
            )
            if not _result_is_rejected(result):
                result.update(mission_status(root, missions_root, args.mission_id))
        elif args.command == "finish":
            session_id = _liaison_session_from_env(args.liaison_session)
            result = _outcome_json(
                finish_mission(
                    root,
                    missions_root,
                    args.mission_id,
                    operation_id=args.op,
                    expected_revision=args.expect,
                    liaison_session_id=session_id,
                    receipt_path=Path(args.receipt),
                )
            )
            if not _result_is_rejected(result):
                result.update(mission_status(root, missions_root, args.mission_id))
        else:
            result = mission_status(root, missions_root, args.mission_id)
    except InvalidOperation as exc:
        print(json.dumps({"reason": str(exc), "status": "error"}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    if args.command == "status":
        return 0 if result.get("state") is not None else 1
    if result.get("status") in {"completed", "replayed"} and not _result_is_rejected(result):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
