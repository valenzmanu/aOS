from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

import pytest

from modules.protocol import (
    APPROVAL_DECISION_REQUIRED_FIELDS,
    APPROVAL_DECISION_SCHEMA_VERSION,
    APPROVAL_DECISIONS,
    EVENT_ENVELOPE_VERSION,
    EVENT_PAYLOAD_SCHEMA_VERSION,
    EVIDENCE_RECORD_REQUIRED_FIELDS,
    EVIDENCE_RECORD_SCHEMA_VERSION,
    RESERVED_EVENT_KINDS,
    RESERVED_EVENT_SPECS,
    AggregateType,
    AttemptFailure,
    AttemptRecovery,
    AttemptState,
    ContractAuthorization,
    EventAggregateType,
    MissionRetry,
    MissionState,
    ProcessRecovery,
    ProcessState,
    ProtocolViolation,
    RetryKind,
    WorkerSessionState,
    allowed_targets,
    is_reserved_event_kind,
    is_terminal_state,
    require_generic_event_kind,
    require_reserved_event,
    require_transition,
    validate_approval_decision,
    validate_evidence_record,
)


def test_mission_draft_can_be_cancelled_but_not_dispatched() -> None:
    require_transition(AggregateType.MISSION, "draft", "cancelled")

    with pytest.raises(ProtocolViolation, match="illegal mission transition"):
        require_transition(AggregateType.MISSION, "draft", "dispatched")


EXPECTED_ORDINARY_EDGES = {
    AggregateType.MISSION: {
        ("draft", "cancelled"),
        ("authorized", "dispatched"),
        ("authorized", "cancelled"),
        ("dispatched", "implementing"),
        ("dispatched", "failed"),
        ("dispatched", "cancelled"),
        ("implementing", "verifying"),
        ("implementing", "failed"),
        ("implementing", "cancelled"),
        ("verifying", "awaiting_approval"),
        ("verifying", "failed"),
        ("verifying", "cancelled"),
        ("awaiting_approval", "delivered"),
        ("awaiting_approval", "failed"),
        ("awaiting_approval", "cancelled"),
        ("failed", "cancelled"),
        ("non_convergent", "cancelled"),
    },
    AggregateType.PROCESS: {
        ("created", "running"),
        ("created", "failed"),
        ("created", "cancelled"),
        ("created", "orphaned"),
        ("running", "paused"),
        ("running", "blocked"),
        ("running", "completed"),
        ("running", "failed"),
        ("running", "cancelled"),
        ("running", "orphaned"),
        ("paused", "running"),
        ("paused", "cancelled"),
        ("paused", "orphaned"),
        ("blocked", "running"),
        ("blocked", "failed"),
        ("blocked", "cancelled"),
        ("blocked", "orphaned"),
    },
    AggregateType.WORKER_SESSION: {
        ("created", "running"),
        ("created", "failed"),
        ("created", "cancelled"),
        ("created", "orphaned"),
        ("running", "paused"),
        ("running", "blocked"),
        ("running", "completed"),
        ("running", "failed"),
        ("running", "cancelled"),
        ("running", "orphaned"),
        ("paused", "running"),
        ("paused", "cancelled"),
        ("paused", "orphaned"),
        ("blocked", "running"),
        ("blocked", "failed"),
        ("blocked", "cancelled"),
        ("blocked", "orphaned"),
    },
    AggregateType.ATTEMPT: {
        ("active", "completed"),
        ("active", "failed"),
        ("active", "orphaned"),
        ("active", "cancelled"),
    },
}

STATES = {
    AggregateType.MISSION: tuple(state.value for state in MissionState),
    AggregateType.PROCESS: tuple(state.value for state in ProcessState),
    AggregateType.WORKER_SESSION: tuple(state.value for state in WorkerSessionState),
    AggregateType.ATTEMPT: tuple(state.value for state in AttemptState),
}


@pytest.mark.parametrize("aggregate_type", tuple(AggregateType))
def test_ordinary_transition_graph_is_an_exhaustive_cross_product(
    aggregate_type: AggregateType,
) -> None:
    expected = EXPECTED_ORDINARY_EDGES[aggregate_type]
    observed: set[tuple[str, str]] = set()

    for source in STATES[aggregate_type]:
        assert allowed_targets(aggregate_type, source).issuperset(
            target for edge_source, target in expected if edge_source == source
        )
        for target in STATES[aggregate_type]:
            edge = (source, target)
            if edge in expected:
                require_transition(aggregate_type, source, target)
                observed.add(edge)
            else:
                with pytest.raises(ProtocolViolation):
                    require_transition(aggregate_type, source, target)

    assert observed == expected


DECISION_HASH = "a" * 64


def initial_authorization() -> ContractAuthorization:
    return ContractAuthorization(
        human_principal="human:maintainer",
        decision_source_hash=DECISION_HASH,
        current_revision=0,
        proposed_revision=1,
        attempts_created=0,
        attempt_ceiling=3,
        invalidates_prior_routing=False,
        invalidates_prior_evidence=False,
    )


def amendment() -> ContractAuthorization:
    return ContractAuthorization(
        human_principal="human:maintainer",
        decision_source_hash=DECISION_HASH,
        current_revision=2,
        proposed_revision=3,
        attempts_created=2,
        attempt_ceiling=3,
        invalidates_prior_routing=True,
        invalidates_prior_evidence=True,
    )


def test_initial_authorization_and_every_amendment_edge_require_human_context() -> None:
    require_transition(
        AggregateType.MISSION,
        MissionState.DRAFT,
        MissionState.AUTHORIZED,
        context=initial_authorization(),
    )
    for source in (
        MissionState.AUTHORIZED,
        MissionState.FAILED,
        MissionState.NON_CONVERGENT,
    ):
        require_transition(
            AggregateType.MISSION,
            source,
            MissionState.AUTHORIZED,
            context=amendment(),
        )


@pytest.mark.parametrize(
    ("context", "message"),
    (
        (replace(amendment(), human_principal=""), "human principal"),
        (replace(amendment(), decision_source_hash="not-a-digest"), "source hash"),
        (replace(amendment(), proposed_revision=2), "next contract revision"),
        (replace(amendment(), attempts_created=True), "attempts_created"),
        (replace(amendment(), attempt_ceiling=1), "below attempts"),
        (replace(amendment(), invalidates_prior_routing=False), "routing"),
        (replace(amendment(), invalidates_prior_evidence=False), "evidence"),
    ),
)
def test_contract_amendment_rejects_incomplete_or_unsafe_context(
    context: ContractAuthorization,
    message: str,
) -> None:
    with pytest.raises(ProtocolViolation, match=message):
        require_transition(
            AggregateType.MISSION,
            MissionState.FAILED,
            MissionState.AUTHORIZED,
            context=context,
        )


def test_callers_cannot_supply_their_own_allowed_from_set() -> None:
    with pytest.raises(TypeError, match="allowed_from"):
        require_transition(  # type: ignore[call-arg]
            AggregateType.MISSION,
            MissionState.DRAFT,
            MissionState.DELIVERED,
            allowed_from={MissionState.DRAFT},
        )


def retry_context(kind: RetryKind) -> MissionRetry:
    common = {
        "kind": kind,
        "attempts_created": 3,
        "attempt_ceiling": 3,
        "previous_authority_epoch_id": "AE-OLD",
        "new_authority_epoch_id": "AE-NEW",
        "creates_new_session": True,
    }
    if kind is RetryKind.SESSION:
        return MissionRetry(
            **common,
            current_attempt_state=AttemptState.ACTIVE,
            subordinate_state="failed",
            same_attempt=True,
            same_worktree=True,
            creates_new_attempt=False,
            creates_new_process=False,
            creates_new_worktree=False,
            creates_new_branch=False,
        )
    if kind is RetryKind.PROCESS:
        return MissionRetry(
            **common,
            current_attempt_state=AttemptState.ACTIVE,
            subordinate_state="failed",
            same_attempt=True,
            same_worktree=True,
            creates_new_attempt=False,
            creates_new_process=True,
            creates_new_worktree=False,
            creates_new_branch=False,
        )
    if kind is RetryKind.ATTEMPT:
        return MissionRetry(
            **(common | {"attempts_created": 2}),
            current_attempt_state=AttemptState.FAILED,
            subordinate_state="failed",
            same_attempt=False,
            same_worktree=False,
            creates_new_attempt=True,
            creates_new_process=True,
            creates_new_worktree=True,
            creates_new_branch=True,
        )
    if kind is RetryKind.EXPIRED_WORKER_RECOVERY:
        return MissionRetry(
            **common,
            current_attempt_state=AttemptState.ORPHANED,
            subordinate_state="orphaned",
            same_attempt=True,
            same_worktree=True,
            creates_new_attempt=False,
            creates_new_process=False,
            creates_new_worktree=False,
            creates_new_branch=False,
        )
    return MissionRetry(
        **common,
        current_attempt_state=AttemptState.COMPLETED,
        subordinate_state="completed",
        same_attempt=True,
        same_worktree=True,
        creates_new_attempt=False,
        creates_new_process=True,
        creates_new_worktree=False,
        creates_new_branch=False,
        human_rejection_source_hash=DECISION_HASH,
    )


@pytest.mark.parametrize("kind", tuple(RetryKind))
def test_failed_mission_retry_paths_preserve_the_required_identity(
    kind: RetryKind,
) -> None:
    require_transition(
        AggregateType.MISSION,
        MissionState.FAILED,
        MissionState.DISPATCHED,
        context=retry_context(kind),
    )


@pytest.mark.parametrize(
    ("context", "message"),
    (
        (
            replace(
                retry_context(RetryKind.SESSION),
                new_authority_epoch_id="AE-OLD",
            ),
            "new authority epoch",
        ),
        (
            replace(
                retry_context(RetryKind.SESSION),
                creates_new_process=True,
            ),
            "same process",
        ),
        (
            replace(
                retry_context(RetryKind.PROCESS),
                creates_new_process=False,
            ),
            "new process",
        ),
        (
            replace(
                retry_context(RetryKind.ATTEMPT),
                attempts_created=3,
            ),
            "attempt ceiling",
        ),
        (
            replace(
                retry_context(RetryKind.ATTEMPT),
                creates_new_worktree=False,
            ),
            "new attempt, worktree, branch, process, and session",
        ),
        (
            replace(
                retry_context(RetryKind.EXPIRED_WORKER_RECOVERY),
                current_attempt_state=AttemptState.ACTIVE,
            ),
            "orphaned attempt",
        ),
        (
            replace(
                retry_context(RetryKind.HUMAN_REJECTED_REWORK),
                human_rejection_source_hash=None,
            ),
            "human rejection",
        ),
    ),
)
def test_mission_retry_rejects_identity_or_ceiling_violations(
    context: MissionRetry,
    message: str,
) -> None:
    with pytest.raises(ProtocolViolation, match=message):
        require_transition(
            AggregateType.MISSION,
            MissionState.FAILED,
            MissionState.DISPATCHED,
            context=context,
        )


@pytest.mark.parametrize(
    "source",
    (MissionState.DISPATCHED, MissionState.IMPLEMENTING, MissionState.VERIFYING),
)
def test_non_convergent_requires_the_final_attempt_to_have_failed(
    source: MissionState,
) -> None:
    final_failure = AttemptFailure(
        current_attempt_state=AttemptState.FAILED,
        attempts_created=3,
        attempt_ceiling=3,
    )
    require_transition(
        AggregateType.MISSION,
        source,
        MissionState.NON_CONVERGENT,
        context=final_failure,
    )

    with pytest.raises(ProtocolViolation, match="ceiling has not been reached"):
        require_transition(
            AggregateType.MISSION,
            source,
            MissionState.NON_CONVERGENT,
            context=replace(final_failure, attempts_created=2),
        )
    with pytest.raises(ProtocolViolation, match="attempt must have actually failed"):
        require_transition(
            AggregateType.MISSION,
            source,
            MissionState.NON_CONVERGENT,
            context=replace(final_failure, current_attempt_state=AttemptState.ACTIVE),
        )


def test_final_attempt_failure_cannot_leave_mission_merely_failed() -> None:
    with pytest.raises(ProtocolViolation, match="must become non_convergent"):
        require_transition(
            AggregateType.MISSION,
            MissionState.IMPLEMENTING,
            MissionState.FAILED,
            context=AttemptFailure(
                current_attempt_state=AttemptState.FAILED,
                attempts_created=3,
                attempt_ceiling=3,
            ),
        )

    require_transition(
        AggregateType.MISSION,
        MissionState.IMPLEMENTING,
        MissionState.FAILED,
        context=AttemptFailure(
            current_attempt_state=AttemptState.FAILED,
            attempts_created=2,
            attempt_ceiling=3,
        ),
    )


def process_recovery() -> ProcessRecovery:
    return ProcessRecovery(
        retry_kind=RetryKind.SESSION,
        previous_authority_epoch_id="AE-OLD",
        new_authority_epoch_id="AE-NEW",
        same_process=True,
        same_attempt=True,
        same_worktree=True,
        creates_new_worker_session=True,
    )


def test_running_process_reset_requires_typed_session_recovery() -> None:
    require_transition(
        AggregateType.PROCESS,
        ProcessState.RUNNING,
        ProcessState.CREATED,
        context=process_recovery(),
    )

    with pytest.raises(ProtocolViolation, match="requires ProcessRecovery context"):
        require_transition(
            AggregateType.PROCESS,
            ProcessState.RUNNING,
            ProcessState.CREATED,
            context=object(),
        )


@pytest.mark.parametrize("source", (ProcessState.FAILED, ProcessState.ORPHANED))
def test_terminal_process_can_only_reset_for_session_retry_with_new_epoch(
    source: ProcessState,
) -> None:
    require_transition(
        AggregateType.PROCESS,
        source,
        ProcessState.CREATED,
        context=process_recovery(),
    )

    for invalid, message in (
        (replace(process_recovery(), retry_kind=RetryKind.PROCESS), "session retry"),
        (
            replace(process_recovery(), new_authority_epoch_id="AE-OLD"),
            "new authority epoch",
        ),
        (replace(process_recovery(), same_process=False), "same process"),
        (
            replace(process_recovery(), creates_new_worker_session=False),
            "new worker session",
        ),
    ):
        with pytest.raises(ProtocolViolation, match=message):
            require_transition(
                AggregateType.PROCESS,
                source,
                ProcessState.CREATED,
                context=invalid,
            )


def expired_attempt_recovery() -> AttemptRecovery:
    return AttemptRecovery(
        retry_kind=RetryKind.EXPIRED_WORKER_RECOVERY,
        previous_authority_epoch_id="AE-OLD",
        new_authority_epoch_id="AE-NEW",
        same_attempt=True,
        same_worktree=True,
        creates_new_process=False,
        creates_new_worker_session=True,
    )


def rejected_work_recovery() -> AttemptRecovery:
    return AttemptRecovery(
        retry_kind=RetryKind.HUMAN_REJECTED_REWORK,
        previous_authority_epoch_id="AE-OLD",
        new_authority_epoch_id="AE-NEW",
        same_attempt=True,
        same_worktree=True,
        creates_new_process=True,
        creates_new_worker_session=True,
        human_rejection_source_hash=DECISION_HASH,
    )


def test_orphaned_attempt_reopens_only_as_same_attempt_recovery() -> None:
    require_transition(
        AggregateType.ATTEMPT,
        AttemptState.ORPHANED,
        AttemptState.ACTIVE,
        context=expired_attempt_recovery(),
    )
    for invalid, message in (
        (
            replace(expired_attempt_recovery(), retry_kind=RetryKind.ATTEMPT),
            "expired-worker recovery",
        ),
        (
            replace(expired_attempt_recovery(), same_attempt=False),
            "same attempt",
        ),
        (
            replace(expired_attempt_recovery(), same_worktree=False),
            "same worktree",
        ),
    ):
        with pytest.raises(ProtocolViolation, match=message):
            require_transition(
                AggregateType.ATTEMPT,
                AttemptState.ORPHANED,
                AttemptState.ACTIVE,
                context=invalid,
            )


def test_completed_attempt_reopens_only_for_human_rejected_new_process_rework() -> None:
    require_transition(
        AggregateType.ATTEMPT,
        AttemptState.COMPLETED,
        AttemptState.ACTIVE,
        context=rejected_work_recovery(),
    )
    for invalid, message in (
        (
            replace(rejected_work_recovery(), retry_kind=RetryKind.SESSION),
            "human-rejected rework",
        ),
        (
            replace(rejected_work_recovery(), creates_new_process=False),
            "new process",
        ),
        (
            replace(rejected_work_recovery(), human_rejection_source_hash=None),
            "human rejection",
        ),
    ):
        with pytest.raises(ProtocolViolation, match=message):
            require_transition(
                AggregateType.ATTEMPT,
                AttemptState.COMPLETED,
                AttemptState.ACTIVE,
                context=invalid,
            )


@pytest.mark.parametrize(
    ("aggregate_type", "terminal_states"),
    (
        (AggregateType.MISSION, {MissionState.DELIVERED, MissionState.CANCELLED}),
        (AggregateType.PROCESS, {ProcessState.COMPLETED, ProcessState.CANCELLED}),
        (
            AggregateType.WORKER_SESSION,
            {
                WorkerSessionState.COMPLETED,
                WorkerSessionState.FAILED,
                WorkerSessionState.CANCELLED,
                WorkerSessionState.ORPHANED,
            },
        ),
        (AggregateType.ATTEMPT, {AttemptState.FAILED, AttemptState.CANCELLED}),
    ),
)
def test_hard_terminal_states_have_no_outgoing_edges(
    aggregate_type: AggregateType,
    terminal_states: set[MissionState | ProcessState | WorkerSessionState | AttemptState],
) -> None:
    for state in STATES[aggregate_type]:
        assert is_terminal_state(aggregate_type, state) is (state in terminal_states)
        if state in terminal_states:
            assert allowed_targets(aggregate_type, state) == frozenset()


def test_reserved_event_vocabulary_is_versioned_owned_and_immutable() -> None:
    assert EVENT_ENVELOPE_VERSION == 1
    assert EVENT_PAYLOAD_SCHEMA_VERSION == 1
    assert isinstance(RESERVED_EVENT_SPECS, MappingProxyType)
    assert frozenset(RESERVED_EVENT_SPECS) == RESERVED_EVENT_KINDS
    assert {
        "journal.initialized",
        "schema.upgraded",
        "gate.admitted",
        "gate.rejected",
        "effect.intent",
        "effect.completed",
        "effect.failed",
        "mission.transitioned",
        "process.transitioned",
        "worker_session.transitioned",
        "attempt.transitioned",
        "evidence.recorded",
        "approval.decided",
        "authority.renewed",
    } <= RESERVED_EVENT_KINDS

    for kind, spec in RESERVED_EVENT_SPECS.items():
        require_reserved_event(
            kind,
            next(iter(spec.aggregate_types)),
            payload_schema_version=EVENT_PAYLOAD_SCHEMA_VERSION,
        )
        assert is_reserved_event_kind(kind)

    with pytest.raises(TypeError):
        RESERVED_EVENT_SPECS["mission.bypassed"] = RESERVED_EVENT_SPECS[  # type: ignore[index]
            "mission.transitioned"
        ]


def test_reserved_event_owner_and_payload_version_are_closed() -> None:
    require_reserved_event(
        "mission.transitioned",
        EventAggregateType.MISSION,
        payload_schema_version=1,
    )
    with pytest.raises(ProtocolViolation, match="owned by"):
        require_reserved_event(
            "mission.transitioned",
            EventAggregateType.PROCESS,
            payload_schema_version=1,
        )
    with pytest.raises(ProtocolViolation, match="payload schema version"):
        require_reserved_event(
            "mission.transitioned",
            EventAggregateType.MISSION,
            payload_schema_version=2,
        )
    with pytest.raises(ProtocolViolation, match="not reserved"):
        require_reserved_event(
            "custom.observed",
            EventAggregateType.PROCESS,
            payload_schema_version=1,
        )


def test_generic_append_rejects_reserved_and_malformed_event_kinds() -> None:
    require_generic_event_kind("adapter.observed")
    assert not is_reserved_event_kind("adapter.observed")

    with pytest.raises(ProtocolViolation, match="reserved"):
        require_generic_event_kind("approval.decided")
    with pytest.raises(ProtocolViolation, match="namespace"):
        require_generic_event_kind("Not A Kind")


def valid_evidence_record() -> dict[str, object]:
    return {
        "record_schema_version": EVIDENCE_RECORD_SCHEMA_VERSION,
        "mission_id": "M-1234",
        "mission_revision": 7,
        "contract_revision_id": "C-1234",
        "contract_hash": "1" * 64,
        "attempt_id": "A-0003",
        "process_id": "P-5678",
        "worker_session_id": "S-9012",
        "git_head": "2" * 40,
        "index_digest": "3" * 64,
        "worktree_manifest_digest": "4" * 64,
        "verifier_spec_digest": "5" * 64,
        "command_hash": "6" * 64,
        "started_at": "2026-07-23T12:00:00+00:00",
        "ended_at": "2026-07-23T12:01:00+00:00",
        "exit_status": 0,
        "output_artifacts": ["7" * 64, "8" * 64],
    }


def test_evidence_vocabulary_exactly_binds_the_governance_inputs() -> None:
    record = valid_evidence_record()
    assert set(record) == EVIDENCE_RECORD_REQUIRED_FIELDS
    validate_evidence_record(record)

    for field in EVIDENCE_RECORD_REQUIRED_FIELDS:
        incomplete = record.copy()
        del incomplete[field]
        with pytest.raises(ProtocolViolation, match="missing fields"):
            validate_evidence_record(incomplete)

    extended = record | {"claim": "tests passed"}
    with pytest.raises(ProtocolViolation, match="unknown fields"):
        validate_evidence_record(extended)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("record_schema_version", 2, "schema version"),
        ("mission_revision", True, "mission_revision"),
        ("contract_hash", "not-a-digest", "contract_hash"),
        ("git_head", "not-a-commit", "git_head"),
        ("output_artifacts", ["not-a-digest"], "output_artifacts"),
        ("output_artifacts", [{"digest": "7" * 64}], "output_artifacts"),
        ("started_at", "yesterday", "started_at"),
        ("ended_at", "2026-07-23T11:00:00+00:00", "precedes"),
    ),
)
def test_evidence_vocabulary_rejects_malformed_binding_fields(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ProtocolViolation, match=message):
        validate_evidence_record(valid_evidence_record() | {field: value})


def valid_approval_decision(decision: str = "approved") -> dict[str, object]:
    return {
        "decision_schema_version": APPROVAL_DECISION_SCHEMA_VERSION,
        "mission_id": "M-1234",
        "expected_mission_revision": 7,
        "evidence_digest": "9" * 64,
        "decision": decision,
        "human_principal": "human:maintainer",
        "decision_source_hash": DECISION_HASH,
    }


def test_approval_vocabulary_names_evidence_decision_and_human_provenance() -> None:
    assert frozenset({"approved", "rejected"}) == APPROVAL_DECISIONS
    for decision in APPROVAL_DECISIONS:
        payload = valid_approval_decision(decision)
        assert set(payload) == APPROVAL_DECISION_REQUIRED_FIELDS
        validate_approval_decision(payload)

    for field in APPROVAL_DECISION_REQUIRED_FIELDS:
        incomplete = valid_approval_decision()
        del incomplete[field]
        with pytest.raises(ProtocolViolation, match="missing fields"):
            validate_approval_decision(incomplete)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("decision_schema_version", 2, "schema version"),
        ("expected_mission_revision", False, "expected_mission_revision"),
        ("evidence_digest", "not-a-digest", "evidence_digest"),
        ("decision", "self_approved", "decision"),
        ("decision", ["approved"], "decision"),
        ("human_principal", "", "human_principal"),
        ("decision_source_hash", "not-a-digest", "decision_source_hash"),
    ),
)
def test_approval_vocabulary_rejects_unattributed_or_unbound_decisions(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ProtocolViolation, match=message):
        validate_approval_decision(valid_approval_decision() | {field: value})
