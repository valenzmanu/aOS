from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from modules.journal import (
    EventRecord,
    ExpectedRevision,
    GateRejection,
    InitializationRequest,
    InvalidOperation,
    Invoker,
    OperationOutcome,
    OperationRequest,
    _run_governed,
    append_observation,
    initialize_root,
    read_events,
    retire_historical_liaison_authority,
)
from modules.protocol import RESERVED_EVENT_KINDS

FIXED_TIME = datetime(2026, 7, 23, 16, 0, tzinfo=UTC)
DECISION_SOURCE_HASH = "a" * 64


@dataclass(frozen=True)
class InitializedRoot:
    path: Path
    initialization_event_id: int


@pytest.fixture
def initialized_root(tmp_path: Path) -> InitializedRoot:
    root = tmp_path / "state"
    outcome = initialize_root(
        root,
        InitializationRequest(
            operation_id="OP-INIT-JOURNAL-CORE",
            invoker_id="maintenance:journal-core-tests",
            expected_schema=0,
            expected_root_state="empty",
        ),
        clock=lambda: FIXED_TIME,
    )
    return InitializedRoot(root, outcome.initialization_event_id)


def invoker(*, relayed_human: bool = False) -> Invoker:
    return Invoker(
        kind="eval_fixture",
        id="eval:journal-core",
        on_behalf_of="human:maintainer" if relayed_human else None,
        decision_source_hash=DECISION_SOURCE_HASH if relayed_human else None,
    )


def observation_request(
    operation_id: str,
    payload: dict[str, object],
    *,
    kind: str = "fixture.observed",
) -> OperationRequest:
    return OperationRequest(
        operation_id=operation_id,
        action="journal.append_observation",
        invoker=invoker(),
        expected_revisions=(),
        payload={
            "kind": kind,
            "aggregate_type": "fixture",
            "aggregate_id": "fixture-one",
            "payload": payload,
        },
    )


def governed_request(
    operation_id: str,
    *,
    expected_revision: int,
    relayed_human: bool = False,
) -> OperationRequest:
    return OperationRequest(
        operation_id=operation_id,
        action="mission.create",
        invoker=invoker(relayed_human=relayed_human),
        expected_revisions=(ExpectedRevision("mission", "M-JOURNAL-CORE", expected_revision),),
        payload={"mission_id": "M-JOURNAL-CORE", "objective": "exercise kernel"},
    )


def install_fixture_projection(root: Path) -> None:
    with sqlite3.connect(root / "aos.db") as db:
        db.execute(
            """
            CREATE TABLE fixture_projection (
                id TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                source_event_id INTEGER NOT NULL REFERENCES events(id)
            )
            """
        )


def create_fixture_projector(context: object) -> None:
    event = context.append_event(  # type: ignore[attr-defined]
        "mission.created",
        "mission",
        "M-JOURNAL-CORE",
        {"state": "draft"},
    )
    context.db.execute(  # type: ignore[attr-defined]
        """
        INSERT INTO fixture_projection(id, value, source_event_id)
        VALUES ('M-JOURNAL-CORE', 'draft', ?)
        """,
        (event.event_id,),
    )


def test_observation_request_hash_is_canonical_and_replay_safe(
    initialized_root: InitializedRoot,
) -> None:
    first_request = observation_request(
        "OP-CANONICAL-OBSERVATION",
        {"alpha": 1, "nested": {"left": 2, "right": 3}},
    )
    reordered_request = observation_request(
        "OP-CANONICAL-OBSERVATION",
        {"nested": {"right": 3, "left": 2}, "alpha": 1},
    )

    first = append_observation(
        initialized_root.path,
        first_request,
        clock=lambda: FIXED_TIME,
    )
    replay = append_observation(
        initialized_root.path,
        reordered_request,
        clock=lambda: FIXED_TIME,
    )

    assert isinstance(first, OperationOutcome)
    assert first.status == "completed"
    assert replay.status == "replayed"
    assert replay.admission_event_id == first.admission_event_id
    assert replay.domain_event_ids == first.domain_event_ids
    events = read_events(
        initialized_root.path,
        after_event_id=initialized_root.initialization_event_id,
    )
    assert all(isinstance(event, EventRecord) for event in events)
    assert [event.kind for event in events] == ["gate.admitted", "fixture.observed"]


def test_nested_secret_rejection_persists_no_secret_bytes(
    initialized_root: InitializedRoot,
) -> None:
    sentinel = "sk-" + "journal-core-secret-sentinel-123456789"
    outcome = append_observation(
        initialized_root.path,
        observation_request(
            "OP-SECRET-OBSERVATION",
            {"metadata": {"provider": {"apiKey": sentinel}}},
        ),
        clock=lambda: FIXED_TIME,
    )

    assert outcome.status == "rejected"
    assert outcome.rejection_event_id is not None
    assert outcome.domain_event_ids == ()
    for path in initialized_root.path.rglob("*"):
        if path.is_file():
            assert sentinel.encode() not in path.read_bytes()


def test_same_operation_different_request_is_durably_deduplicated(
    initialized_root: InitializedRoot,
) -> None:
    original = observation_request(
        "OP-CONFLICT-OBSERVATION",
        {"sequence": 1},
    )
    conflict = observation_request(
        "OP-CONFLICT-OBSERVATION",
        {"sequence": 2},
    )
    append_observation(initialized_root.path, original, clock=lambda: FIXED_TIME)

    first_conflict = append_observation(
        initialized_root.path,
        conflict,
        clock=lambda: FIXED_TIME,
    )
    repeated_conflict = append_observation(
        initialized_root.path,
        conflict,
        clock=lambda: FIXED_TIME,
    )

    assert first_conflict.status == "rejected"
    assert repeated_conflict.status == "rejected"
    assert repeated_conflict.rejection_event_id == first_conflict.rejection_event_id
    with sqlite3.connect(initialized_root.path / "aos.db") as db:
        conflicts = db.execute(
            """
            SELECT conflicting_request_hash, rejection_event_id
            FROM operation_conflicts
            WHERE operation_id='OP-CONFLICT-OBSERVATION'
            """
        ).fetchall()
        assert len(conflicts) == 1
        assert len(str(conflicts[0][0])) == 64


def test_governed_operation_atomically_admits_projects_events_and_revises(
    initialized_root: InitializedRoot,
) -> None:
    install_fixture_projection(initialized_root.path)

    outcome = _run_governed(
        initialized_root.path,
        governed_request("OP-CREATE-MISSION", expected_revision=0),
        validator=lambda _context: None,
        projector=create_fixture_projector,
        clock=lambda: FIXED_TIME,
    )

    assert outcome.status == "completed"
    assert outcome.admission_event_id is not None
    assert len(outcome.domain_event_ids) == 1
    domain_event_id = outcome.domain_event_ids[0]
    assert outcome.admission_event_id < domain_event_id
    with sqlite3.connect(initialized_root.path / "aos.db") as db:
        assert db.execute(
            """
            SELECT value, source_event_id
            FROM fixture_projection
            WHERE id='M-JOURNAL-CORE'
            """
        ).fetchone() == ("draft", domain_event_id)
        assert db.execute(
            """
            SELECT revision, genesis_event_id, source_event_id
            FROM aggregate_revisions
            WHERE aggregate_type='mission' AND aggregate_id='M-JOURNAL-CORE'
            """
        ).fetchone() == (1, domain_event_id, domain_event_id)
        assert db.execute(
            """
            SELECT expected_revision, observed_revision, role
            FROM operation_targets
            WHERE operation_id='OP-CREATE-MISSION'
              AND aggregate_type='mission'
              AND aggregate_id='M-JOURNAL-CORE'
            """
        ).fetchone() == (0, 0, "write")


def test_stale_revision_rejection_commits_and_leaves_target_unchanged(
    initialized_root: InitializedRoot,
) -> None:
    install_fixture_projection(initialized_root.path)
    _run_governed(
        initialized_root.path,
        governed_request("OP-CREATE-BEFORE-STALE", expected_revision=0),
        validator=lambda _context: None,
        projector=create_fixture_projector,
        clock=lambda: FIXED_TIME,
    )
    projector_called = False

    def must_not_project(_context: object) -> None:
        nonlocal projector_called
        projector_called = True

    rejected = _run_governed(
        initialized_root.path,
        governed_request("OP-STALE-MISSION", expected_revision=0),
        validator=lambda _context: None,
        projector=must_not_project,
        clock=lambda: FIXED_TIME,
    )

    assert rejected.status == "rejected"
    assert rejected.rejection_event_id is not None
    assert rejected.domain_event_ids == ()
    assert not projector_called
    with sqlite3.connect(initialized_root.path / "aos.db") as db:
        assert db.execute(
            """
            SELECT value FROM fixture_projection
            WHERE id='M-JOURNAL-CORE'
            """
        ).fetchone() == ("draft",)
        assert db.execute(
            """
            SELECT revision FROM aggregate_revisions
            WHERE aggregate_type='mission' AND aggregate_id='M-JOURNAL-CORE'
            """
        ).fetchone() == (1,)
        assert db.execute(
            """
            SELECT expected_revision, observed_revision
            FROM operation_targets
            WHERE operation_id='OP-STALE-MISSION'
              AND aggregate_type='mission'
              AND aggregate_id='M-JOURNAL-CORE'
            """
        ).fetchone() == (0, 1)


def test_gate_rejection_is_committed_before_the_kernel_returns(
    initialized_root: InitializedRoot,
) -> None:
    projector_called = False

    def reject(_context: object) -> GateRejection:
        return GateRejection(
            code="fixture.policy_denied",
            details={"reason": "fixture policy denied the operation"},
        )

    def must_not_project(_context: object) -> None:
        nonlocal projector_called
        projector_called = True

    outcome = _run_governed(
        initialized_root.path,
        OperationRequest(
            operation_id="OP-VALIDATOR-REJECTION",
            action="fixture.reject",
            invoker=invoker(),
            expected_revisions=(),
            payload={"safe": "input"},
        ),
        validator=reject,
        projector=must_not_project,
        clock=lambda: FIXED_TIME,
    )

    assert outcome.status == "rejected"
    assert outcome.rejection_event_id is not None
    assert not projector_called
    with sqlite3.connect(initialized_root.path / "aos.db") as db:
        assert db.execute(
            """
            SELECT status, rejection_event_id
            FROM operations
            WHERE operation_id='OP-VALIDATOR-REJECTION'
            """
        ).fetchone() == ("rejected", outcome.rejection_event_id)
        assert db.execute(
            "SELECT kind FROM events WHERE id=?",
            (outcome.rejection_event_id,),
        ).fetchone() == ("gate.rejected",)


def test_projector_fault_rolls_back_admission_domain_event_and_projection(
    initialized_root: InitializedRoot,
) -> None:
    install_fixture_projection(initialized_root.path)
    before = read_events(initialized_root.path)

    def faulty_projector(context: object) -> None:
        create_fixture_projector(context)
        raise RuntimeError("injected projector fault")

    request = governed_request("OP-FAULTED-MISSION", expected_revision=0)
    with pytest.raises(RuntimeError, match="injected projector fault"):
        _run_governed(
            initialized_root.path,
            request,
            validator=lambda _context: None,
            projector=faulty_projector,
            clock=lambda: FIXED_TIME,
        )

    assert read_events(initialized_root.path) == before
    with sqlite3.connect(initialized_root.path / "aos.db") as db:
        assert db.execute("SELECT * FROM fixture_projection").fetchall() == []
        assert (
            db.execute(
                "SELECT 1 FROM operations WHERE operation_id='OP-FAULTED-MISSION'"
            ).fetchone()
            is None
        )
        assert (
            db.execute(
                """
            SELECT 1 FROM aggregate_revisions
            WHERE aggregate_type='mission' AND aggregate_id='M-JOURNAL-CORE'
            """
            ).fetchone()
            is None
        )

    retry = _run_governed(
        initialized_root.path,
        request,
        validator=lambda _context: None,
        projector=create_fixture_projector,
        clock=lambda: FIXED_TIME,
    )
    assert retry.status == "completed"
    assert len(retry.domain_event_ids) == 1


@pytest.mark.parametrize("kind", sorted(RESERVED_EVENT_KINDS))
def test_generic_observation_cannot_forge_any_reserved_event(
    initialized_root: InitializedRoot,
    kind: str,
) -> None:
    outcome = append_observation(
        initialized_root.path,
        observation_request(
            f"OP-RESERVED-{sorted(RESERVED_EVENT_KINDS).index(kind):03d}",
            {"safe": "payload"},
            kind=kind,
        ),
        clock=lambda: FIXED_TIME,
    )

    assert outcome.status == "rejected"
    assert outcome.rejection_event_id is not None
    assert outcome.domain_event_ids == ()


def test_database_guards_keep_events_and_envelopes_immutable(
    initialized_root: InitializedRoot,
) -> None:
    outcome = append_observation(
        initialized_root.path,
        observation_request("OP-IMMUTABLE-EVENT", {"safe": "payload"}),
        clock=lambda: FIXED_TIME,
    )
    event_id = outcome.domain_event_ids[0]

    with sqlite3.connect(initialized_root.path / "aos.db") as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE events SET kind='fixture.changed' WHERE id=?", (event_id,))
        db.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM events WHERE id=?", (event_id,))
        db.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(
                """
                UPDATE event_envelopes
                SET payload_schema_version=2
                WHERE event_id=?
                """,
                (event_id,),
            )
        db.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM event_envelopes WHERE event_id=?", (event_id,))


def test_event_envelope_preserves_invoker_human_and_operation_attribution(
    initialized_root: InitializedRoot,
) -> None:
    install_fixture_projection(initialized_root.path)
    outcome = _run_governed(
        initialized_root.path,
        governed_request(
            "OP-ATTRIBUTED-MISSION",
            expected_revision=0,
            relayed_human=True,
        ),
        validator=lambda _context: None,
        projector=create_fixture_projector,
        clock=lambda: FIXED_TIME,
    )
    domain_event_id = outcome.domain_event_ids[0]

    with sqlite3.connect(initialized_root.path / "aos.db") as db:
        envelope = db.execute(
            """
            SELECT envelope_version, payload_schema_version,
                   invoker_kind, invoker_id, authority_epoch_id,
                   on_behalf_of, decision_source_hash,
                   operation_id, request_key, request_hash,
                   causation_id, admission_id
            FROM event_envelopes
            WHERE event_id=?
            """,
            (domain_event_id,),
        ).fetchone()
        assert envelope is not None
        assert envelope[:4] == (1, 1, "eval_fixture", "eval:journal-core")
        assert envelope[4] is None
        assert envelope[5:7] == ("human:maintainer", DECISION_SOURCE_HASH)
        assert envelope[7:9] == (
            "OP-ATTRIBUTED-MISSION",
            "OP-ATTRIBUTED-MISSION",
        )
        assert len(str(envelope[9])) == 64
        assert envelope[10] == "OP-ATTRIBUTED-MISSION"
        assert envelope[11] == outcome.admission_event_id

        targets = db.execute(
            """
            SELECT aggregate_type, aggregate_id, role
            FROM event_targets
            WHERE event_id=?
            ORDER BY role, aggregate_type, aggregate_id
            """,
            (domain_event_id,),
        ).fetchall()
        assert targets == [("mission", "M-JOURNAL-CORE", "target")]


def test_resident_liaison_session_needs_no_authority_epoch(
    initialized_root: InitializedRoot,
) -> None:
    outcome = _run_governed(
        initialized_root.path,
        OperationRequest(
            operation_id="OP-RESIDENT-LIAISON",
            action="fixture.liaison",
            invoker=Invoker(kind="liaison", id="LS-CODEX-A8F3"),
            expected_revisions=(),
            payload={"focus": "M-JOURNAL-CORE"},
        ),
        validator=lambda _context: None,
        projector=lambda _context: None,
        clock=lambda: FIXED_TIME,
    )

    assert outcome.status == "completed"
    events = read_events(
        initialized_root.path,
        after_event_id=initialized_root.initialization_event_id,
    )
    assert events[0].kind == "gate.admitted"
    assert events[0].envelope is not None
    assert events[0].envelope["invoker_kind"] == "liaison"
    assert events[0].envelope["invoker_id"] == "LS-CODEX-A8F3"
    assert events[0].envelope["authority_epoch_id"] is None


def test_liaison_session_rejects_authority_epoch(
    initialized_root: InitializedRoot,
) -> None:
    with pytest.raises(InvalidOperation, match="must not carry an authority epoch"):
        _run_governed(
            initialized_root.path,
            OperationRequest(
                operation_id="OP-LIAISON-WITH-EPOCH",
                action="fixture.liaison",
                invoker=Invoker(
                    kind="liaison",
                    id="LS-CODEX-A8F3",
                    authority_epoch_id="AE-OBSOLETE-LIAISON",
                ),
                expected_revisions=(),
                payload={},
            ),
            validator=lambda _context: None,
            projector=lambda _context: None,
            clock=lambda: FIXED_TIME,
        )


def test_liaison_invoker_requires_session_shaped_identifier(
    initialized_root: InitializedRoot,
) -> None:
    with pytest.raises(InvalidOperation, match="liaison invoker id"):
        _run_governed(
            initialized_root.path,
            OperationRequest(
                operation_id="OP-LIAISON-WITHOUT-SESSION",
                action="fixture.liaison",
                invoker=Invoker(kind="liaison", id="liaison:codex"),
                expected_revisions=(),
                payload={},
            ),
            validator=lambda _context: None,
            projector=lambda _context: None,
            clock=lambda: FIXED_TIME,
        )


def test_worker_still_requires_delegated_authority_epoch(
    initialized_root: InitializedRoot,
) -> None:
    outcome = _run_governed(
        initialized_root.path,
        OperationRequest(
            operation_id="OP-WORKER-WITHOUT-EPOCH",
            action="fixture.worker",
            invoker=Invoker(kind="worker", id="worker:one"),
            expected_revisions=(),
            payload={},
        ),
        validator=lambda _context: None,
        projector=lambda _context: None,
        clock=lambda: FIXED_TIME,
    )

    assert outcome.status == "rejected"
    assert outcome.reason == "authority_missing"


def test_stale_session_record_never_blocks_another_resident_session(
    initialized_root: InitializedRoot,
) -> None:
    stale = (
        initialized_root.path
        / "liaison"
        / "sessions"
        / "LS-ORPHANED"
        / "session.md"
    )
    stale.parent.mkdir(parents=True)
    stale.write_text("# Liaison session LS-ORPHANED\n\nStatus: active\n")

    outcome = _run_governed(
        initialized_root.path,
        OperationRequest(
            operation_id="OP-AFTER-ORPHANED-SESSION",
            action="fixture.liaison",
            invoker=Invoker(kind="liaison", id="LS-CURSOR-NEW"),
            expected_revisions=(),
            payload={},
        ),
        validator=lambda _context: None,
        projector=lambda _context: None,
        clock=lambda: FIXED_TIME,
    )

    assert outcome.status == "completed"


def test_historical_active_liaison_epoch_is_inert_after_cutover(
    initialized_root: InitializedRoot,
) -> None:
    with sqlite3.connect(initialized_root.path / "aos.db") as db:
        db.execute(
            """
            INSERT INTO aggregate_revisions(
                aggregate_type, aggregate_id, revision,
                genesis_event_id, source_event_id
            ) VALUES ('authority_epoch', 'AE-HISTORICAL-LIAISON', 1, ?, ?)
            """,
            (
                initialized_root.initialization_event_id,
                initialized_root.initialization_event_id,
            ),
        )
        db.execute(
            """
            INSERT INTO authority_epochs(
                epoch_id, kind, subject_id, scope, token_hash,
                issued_at, expires_at, status, issued_event_id
            ) VALUES (
                'AE-HISTORICAL-LIAISON', 'liaison', 'liaison:old',
                'liaison:root', ?, ?, ?, 'active', ?
            )
            """,
            (
                "a" * 64,
                "2026-07-23T15:00:00.000+00:00",
                "2099-07-23T15:00:00.000+00:00",
                initialized_root.initialization_event_id,
            ),
        )

    outcome = _run_governed(
        initialized_root.path,
        OperationRequest(
            operation_id="OP-AFTER-HISTORICAL-LIAISON",
            action="fixture.liaison",
            invoker=Invoker(kind="liaison", id="LS-CODEX-CUTOVER"),
            expected_revisions=(),
            payload={},
        ),
        validator=lambda _context: None,
        projector=lambda _context: None,
        clock=lambda: FIXED_TIME,
    )

    assert outcome.status == "completed"

    cutover_request = OperationRequest(
        operation_id="OP-RETIRE-HISTORICAL-LIAISON",
        action="journal.retire_liaison_authority",
        invoker=Invoker(
            kind="maintenance_operator",
            id="maintenance:resident-liaison-cutover",
        ),
        expected_revisions=(
            ExpectedRevision("authority_epoch", "AE-HISTORICAL-LIAISON", 1),
        ),
        payload={"epoch_ids": ["AE-HISTORICAL-LIAISON"]},
    )
    cutover = retire_historical_liaison_authority(
        initialized_root.path,
        cutover_request,
        clock=lambda: FIXED_TIME,
    )
    replay = retire_historical_liaison_authority(
        initialized_root.path,
        cutover_request,
        clock=lambda: FIXED_TIME,
    )

    assert cutover.status == "completed"
    assert replay.status == "replayed"
    with sqlite3.connect(initialized_root.path / "aos.db") as db:
        assert db.execute(
            """
            SELECT status, close_reason
            FROM authority_epochs
            WHERE epoch_id='AE-HISTORICAL-LIAISON'
            """
        ).fetchone() == ("closed", "resident_liaison_cutover")


def test_resident_sessions_advance_unrelated_aggregates_concurrently(
    initialized_root: InitializedRoot,
) -> None:
    def request(operation_id: str, session_id: str, mission_id: str) -> OperationRequest:
        return OperationRequest(
            operation_id=operation_id,
            action="mission.create",
            invoker=Invoker(kind="liaison", id=session_id),
            expected_revisions=(ExpectedRevision("mission", mission_id, 0),),
            payload={"mission_id": mission_id},
        )

    def projector(mission_id: str) -> Callable[[object], None]:
        def project(context: object) -> None:
            context.append_event(  # type: ignore[attr-defined]
                "mission.created",
                "mission",
                mission_id,
                {"state": "draft"},
            )

        return project

    first = _run_governed(
        initialized_root.path,
        request("OP-SESSION-A-MISSION", "LS-CODEX-A", "M-SESSION-A"),
        validator=lambda _context: None,
        projector=projector("M-SESSION-A"),
        clock=lambda: FIXED_TIME,
    )
    second = _run_governed(
        initialized_root.path,
        request("OP-SESSION-B-MISSION", "LS-CURSOR-B", "M-SESSION-B"),
        validator=lambda _context: None,
        projector=projector("M-SESSION-B"),
        clock=lambda: FIXED_TIME,
    )

    assert first.status == second.status == "completed"
    assert first.revisions[0].revision == second.revisions[0].revision == 1


def test_resident_sessions_resolve_same_revision_conflict_at_the_aggregate(
    initialized_root: InitializedRoot,
) -> None:
    mission_id = "M-SESSION-RACE"

    def request(operation_id: str, session_id: str) -> OperationRequest:
        return OperationRequest(
            operation_id=operation_id,
            action="mission.create",
            invoker=Invoker(kind="liaison", id=session_id),
            expected_revisions=(ExpectedRevision("mission", mission_id, 0),),
            payload={"mission_id": mission_id},
        )

    def create(context: object) -> None:
        context.append_event(  # type: ignore[attr-defined]
            "mission.created",
            "mission",
            mission_id,
            {"state": "draft"},
        )

    first = _run_governed(
        initialized_root.path,
        request("OP-SESSION-RACE-A", "LS-CODEX-RACE"),
        validator=lambda _context: None,
        projector=create,
        clock=lambda: FIXED_TIME,
    )
    second = _run_governed(
        initialized_root.path,
        request("OP-SESSION-RACE-B", "LS-CURSOR-RACE"),
        validator=lambda _context: None,
        projector=create,
        clock=lambda: FIXED_TIME,
    )

    assert first.status == "completed"
    assert second.status == "rejected"
    assert second.reason == "revision_mismatch"
    assert second.revisions[0].revision == 1


def test_identical_liaison_operation_replays_across_harness_sessions(
    initialized_root: InitializedRoot,
) -> None:
    def request(session_id: str) -> OperationRequest:
        return OperationRequest(
            operation_id="OP-CROSS-SESSION-REPLAY",
            action="fixture.liaison",
            invoker=Invoker(kind="liaison", id=session_id),
            expected_revisions=(),
            payload={"effect": "dispatch-once"},
        )

    first = _run_governed(
        initialized_root.path,
        request("LS-CODEX-ORIGINAL"),
        validator=lambda _context: None,
        projector=lambda _context: None,
        clock=lambda: FIXED_TIME,
    )
    replay = _run_governed(
        initialized_root.path,
        request("LS-CURSOR-RECOVERY"),
        validator=lambda _context: None,
        projector=lambda _context: pytest.fail("replay must not project a second effect"),
        clock=lambda: FIXED_TIME,
    )

    assert first.status == "completed"
    assert replay.status == "replayed"
    assert replay.original_status == "completed"
    assert replay.admission_event_id == first.admission_event_id
    with sqlite3.connect(initialized_root.path / "aos.db") as db:
        assert db.execute(
            """
            SELECT invoker_id
            FROM operations
            WHERE operation_id='OP-CROSS-SESSION-REPLAY'
            """
        ).fetchone() == ("LS-CODEX-ORIGINAL",)
