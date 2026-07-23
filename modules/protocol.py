"""Closed protocol vocabulary and transition validators for aOS v2.

The protocol module is deliberately pure.  It owns the legal state graph and
validates the context required by exceptional edges; journal transactions own
admission, persistence, and projection updates.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class ProtocolViolation(ValueError):
    """A proposed protocol operation is outside the closed contract."""


class AggregateType(StrEnum):
    MISSION = "mission"
    PROCESS = "process"
    WORKER_SESSION = "worker_session"
    ATTEMPT = "attempt"


class MissionState(StrEnum):
    DRAFT = "draft"
    AUTHORIZED = "authorized"
    DISPATCHED = "dispatched"
    IMPLEMENTING = "implementing"
    VERIFYING = "verifying"
    AWAITING_APPROVAL = "awaiting_approval"
    DELIVERED = "delivered"
    FAILED = "failed"
    NON_CONVERGENT = "non_convergent"
    CANCELLED = "cancelled"


class ProcessState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"


class WorkerSessionState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"


class AttemptState(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    ORPHANED = "orphaned"
    CANCELLED = "cancelled"


class RetryKind(StrEnum):
    SESSION = "session"
    PROCESS = "process"
    ATTEMPT = "attempt"
    EXPIRED_WORKER_RECOVERY = "expired_worker_recovery"
    HUMAN_REJECTED_REWORK = "human_rejected_rework"


class EventAggregateType(StrEnum):
    JOURNAL = "journal"
    OPERATION = "operation"
    MISSION = "mission"
    CONTRACT_REVISION = "contract_revision"
    ATTEMPT = "attempt"
    PROCESS = "process"
    WORK_ORDER = "work_order"
    WORKER_SESSION = "worker_session"
    EVIDENCE = "evidence"
    APPROVAL = "approval"
    AUTHORITY_EPOCH = "authority_epoch"
    LEASE = "lease"
    MIGRATION = "migration"


@dataclass(frozen=True, slots=True)
class ContractAuthorization:
    """Human-attributed context for initial authorization or amendment."""

    human_principal: str
    decision_source_hash: str
    current_revision: int
    proposed_revision: int
    attempts_created: int
    attempt_ceiling: int
    invalidates_prior_routing: bool
    invalidates_prior_evidence: bool


@dataclass(frozen=True, slots=True)
class MissionRetry:
    """Identity and authority facts for ``failed -> dispatched``."""

    kind: RetryKind
    current_attempt_state: AttemptState
    subordinate_state: str
    attempts_created: int
    attempt_ceiling: int
    previous_authority_epoch_id: str
    new_authority_epoch_id: str
    same_attempt: bool
    same_worktree: bool
    creates_new_attempt: bool
    creates_new_process: bool
    creates_new_session: bool
    creates_new_worktree: bool
    creates_new_branch: bool
    human_rejection_source_hash: str | None = None


@dataclass(frozen=True, slots=True)
class AttemptFailure:
    """Attempt-count facts used to select ``failed`` or ``non_convergent``."""

    current_attempt_state: AttemptState
    attempts_created: int
    attempt_ceiling: int


@dataclass(frozen=True, slots=True)
class ProcessRecovery:
    """Facts required to reset a failed/orphaned process for a new session."""

    retry_kind: RetryKind
    previous_authority_epoch_id: str
    new_authority_epoch_id: str
    same_process: bool
    same_attempt: bool
    same_worktree: bool
    creates_new_worker_session: bool


@dataclass(frozen=True, slots=True)
class AttemptRecovery:
    """Facts required for one of the two same-attempt recovery edges."""

    retry_kind: RetryKind
    previous_authority_epoch_id: str
    new_authority_epoch_id: str
    same_attempt: bool
    same_worktree: bool
    creates_new_process: bool
    creates_new_worker_session: bool
    human_rejection_source_hash: str | None = None


@dataclass(frozen=True, slots=True)
class EventKindSpec:
    """Ownership and revision semantics for one reserved event kind."""

    aggregate_types: frozenset[EventAggregateType]
    advances_aggregate_revision: bool
    requires_admission: bool
    payload_schema_versions: frozenset[int] = frozenset({1})


_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
_GIT_COMMIT_RE: Final = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_EVENT_KIND_RE: Final = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\Z")

EVENT_ENVELOPE_VERSION: Final = 1
EVENT_PAYLOAD_SCHEMA_VERSION: Final = 1
EVIDENCE_RECORD_SCHEMA_VERSION: Final = 1
APPROVAL_DECISION_SCHEMA_VERSION: Final = 1

EVIDENCE_RECORD_REQUIRED_FIELDS: Final = frozenset(
    {
        "record_schema_version",
        "mission_id",
        "mission_revision",
        "contract_revision_id",
        "contract_hash",
        "attempt_id",
        "process_id",
        "worker_session_id",
        "git_head",
        "index_digest",
        "worktree_manifest_digest",
        "verifier_spec_digest",
        "command_hash",
        "started_at",
        "ended_at",
        "exit_status",
        "output_artifacts",
    }
)
APPROVAL_DECISION_REQUIRED_FIELDS: Final = frozenset(
    {
        "decision_schema_version",
        "mission_id",
        "expected_mission_revision",
        "evidence_digest",
        "decision",
        "human_principal",
        "decision_source_hash",
    }
)
APPROVAL_DECISIONS: Final = frozenset({"approved", "rejected"})


def _event_spec(
    aggregate_type: EventAggregateType,
    *,
    advances_revision: bool = True,
    requires_admission: bool = True,
) -> EventKindSpec:
    return EventKindSpec(
        aggregate_types=frozenset({aggregate_type}),
        advances_aggregate_revision=advances_revision,
        requires_admission=requires_admission,
    )


RESERVED_EVENT_SPECS: Final = MappingProxyType(
    {
        "journal.initialized": _event_spec(
            EventAggregateType.JOURNAL,
            requires_admission=False,
        ),
        "schema.upgraded": _event_spec(
            EventAggregateType.MIGRATION,
            requires_admission=False,
        ),
        "gate.admitted": _event_spec(
            EventAggregateType.OPERATION,
            advances_revision=False,
            requires_admission=False,
        ),
        "gate.rejected": _event_spec(
            EventAggregateType.OPERATION,
            advances_revision=False,
            requires_admission=False,
        ),
        "effect.intent": _event_spec(
            EventAggregateType.OPERATION,
            advances_revision=False,
        ),
        "effect.completed": _event_spec(
            EventAggregateType.OPERATION,
            advances_revision=False,
        ),
        "effect.failed": _event_spec(
            EventAggregateType.OPERATION,
            advances_revision=False,
        ),
        "mission.created": _event_spec(EventAggregateType.MISSION),
        "mission.transitioned": _event_spec(EventAggregateType.MISSION),
        "mission.reconciled": _event_spec(EventAggregateType.MISSION),
        "contract.authorized": _event_spec(EventAggregateType.CONTRACT_REVISION),
        "contract.amended": _event_spec(EventAggregateType.CONTRACT_REVISION),
        "attempt.created": _event_spec(EventAggregateType.ATTEMPT),
        "attempt.transitioned": _event_spec(EventAggregateType.ATTEMPT),
        "attempt.reconciled": _event_spec(EventAggregateType.ATTEMPT),
        "process.created": _event_spec(EventAggregateType.PROCESS),
        "process.transitioned": _event_spec(EventAggregateType.PROCESS),
        "process.reconciled": _event_spec(EventAggregateType.PROCESS),
        "work_order.created": _event_spec(EventAggregateType.WORK_ORDER),
        "work_order.revised": _event_spec(EventAggregateType.WORK_ORDER),
        "worker_session.created": _event_spec(EventAggregateType.WORKER_SESSION),
        "worker_session.transitioned": _event_spec(EventAggregateType.WORKER_SESSION),
        "worker_session.reconciled": _event_spec(EventAggregateType.WORKER_SESSION),
        "checkpoint.recorded": _event_spec(EventAggregateType.PROCESS),
        "evidence.recorded": _event_spec(EventAggregateType.EVIDENCE),
        "approval.decided": _event_spec(EventAggregateType.APPROVAL),
        "authority.claimed": _event_spec(EventAggregateType.AUTHORITY_EPOCH),
        "authority.renewed": _event_spec(EventAggregateType.AUTHORITY_EPOCH),
        "authority.released": _event_spec(EventAggregateType.AUTHORITY_EPOCH),
        "authority.revoked": _event_spec(EventAggregateType.AUTHORITY_EPOCH),
        "authority.expired": _event_spec(EventAggregateType.AUTHORITY_EPOCH),
        "authority.reconciled": _event_spec(EventAggregateType.AUTHORITY_EPOCH),
        "authority.legacy_closed": EventKindSpec(
            aggregate_types=frozenset(
                {EventAggregateType.AUTHORITY_EPOCH, EventAggregateType.LEASE}
            ),
            advances_aggregate_revision=True,
            requires_admission=True,
        ),
        "migration.boundary": _event_spec(
            EventAggregateType.MIGRATION,
            requires_admission=False,
        ),
        "migration.cleanup_completed": _event_spec(
            EventAggregateType.MIGRATION,
            advances_revision=False,
        ),
    }
)
RESERVED_EVENT_KINDS: Final = frozenset(RESERVED_EVENT_SPECS)


_MISSION_EDGES = {
    "draft": frozenset({"authorized", "cancelled"}),
    "authorized": frozenset({"authorized", "dispatched", "cancelled"}),
    "dispatched": frozenset({"implementing", "failed", "non_convergent", "cancelled"}),
    "implementing": frozenset({"verifying", "failed", "non_convergent", "cancelled"}),
    "verifying": frozenset({"awaiting_approval", "failed", "non_convergent", "cancelled"}),
    "awaiting_approval": frozenset({"delivered", "failed", "cancelled"}),
    "failed": frozenset({"dispatched", "authorized", "cancelled"}),
    "non_convergent": frozenset({"authorized", "cancelled"}),
    "delivered": frozenset(),
    "cancelled": frozenset(),
}

_PROCESS_EDGES = {
    "created": frozenset({"running", "failed", "cancelled", "orphaned"}),
    "running": frozenset({"paused", "blocked", "completed", "failed", "cancelled", "orphaned"}),
    "paused": frozenset({"running", "cancelled", "orphaned"}),
    "blocked": frozenset({"running", "failed", "cancelled", "orphaned"}),
    "failed": frozenset({"created"}),
    "orphaned": frozenset({"created"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}

_WORKER_SESSION_EDGES = {
    "created": frozenset({"running", "failed", "cancelled", "orphaned"}),
    "running": frozenset({"paused", "blocked", "completed", "failed", "cancelled", "orphaned"}),
    "paused": frozenset({"running", "cancelled", "orphaned"}),
    "blocked": frozenset({"running", "failed", "cancelled", "orphaned"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "orphaned": frozenset(),
}

_ATTEMPT_EDGES = {
    "active": frozenset({"completed", "failed", "orphaned", "cancelled"}),
    "orphaned": frozenset({"active"}),
    "completed": frozenset({"active"}),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

LEGAL_TRANSITIONS: Final = MappingProxyType(
    {
        AggregateType.MISSION: MappingProxyType(_MISSION_EDGES),
        AggregateType.PROCESS: MappingProxyType(_PROCESS_EDGES),
        AggregateType.WORKER_SESSION: MappingProxyType(_WORKER_SESSION_EDGES),
        AggregateType.ATTEMPT: MappingProxyType(_ATTEMPT_EDGES),
    }
)

_CONTEXTUAL_EDGES: Final = frozenset(
    {
        (AggregateType.MISSION, "draft", "authorized"),
        (AggregateType.MISSION, "authorized", "authorized"),
        (AggregateType.MISSION, "failed", "authorized"),
        (AggregateType.MISSION, "non_convergent", "authorized"),
        (AggregateType.MISSION, "failed", "dispatched"),
        (AggregateType.MISSION, "dispatched", "non_convergent"),
        (AggregateType.MISSION, "implementing", "non_convergent"),
        (AggregateType.MISSION, "verifying", "non_convergent"),
        (AggregateType.PROCESS, "failed", "created"),
        (AggregateType.PROCESS, "orphaned", "created"),
        (AggregateType.ATTEMPT, "orphaned", "active"),
        (AggregateType.ATTEMPT, "completed", "active"),
    }
)


def _aggregate(aggregate_type: AggregateType | str) -> AggregateType:
    try:
        return AggregateType(aggregate_type)
    except ValueError as exc:
        raise ProtocolViolation(f"unknown aggregate type: {aggregate_type!r}") from exc


def allowed_targets(
    aggregate_type: AggregateType | str,
    current_state: str,
) -> frozenset[str]:
    """Return protocol-owned targets, including context-gated edges."""

    aggregate = _aggregate(aggregate_type)
    graph = LEGAL_TRANSITIONS[aggregate]
    if current_state not in graph:
        raise ProtocolViolation(f"unknown {aggregate.value} state: {current_state!r}")
    return graph[current_state]


def is_terminal_state(
    aggregate_type: AggregateType | str,
    state: str,
) -> bool:
    """Whether a state has no ordinary or contextual outgoing edge."""

    return not allowed_targets(aggregate_type, state)


def is_reserved_event_kind(kind: object) -> bool:
    """Whether ``kind`` is protected from generic append."""

    return isinstance(kind, str) and kind in RESERVED_EVENT_SPECS


def require_generic_event_kind(kind: object) -> None:
    """Validate an extension event kind without permitting reserved kinds."""

    if not isinstance(kind, str) or not _EVENT_KIND_RE.fullmatch(kind):
        raise ProtocolViolation("event kind must use the lowercase namespace.name form")
    if kind in RESERVED_EVENT_SPECS:
        raise ProtocolViolation(f"event kind {kind!r} is reserved for its constitutional module")


def require_reserved_event(
    kind: object,
    aggregate_type: EventAggregateType | str,
    *,
    payload_schema_version: int,
) -> EventKindSpec:
    """Validate the closed owner/version contract for a reserved event."""

    if not isinstance(kind, str) or kind not in RESERVED_EVENT_SPECS:
        raise ProtocolViolation("event kind is not reserved by this protocol version")
    try:
        owner = EventAggregateType(aggregate_type)
    except ValueError as exc:
        raise ProtocolViolation(f"unknown event aggregate type: {aggregate_type!r}") from exc
    spec = RESERVED_EVENT_SPECS[kind]
    if owner not in spec.aggregate_types:
        expected = ", ".join(sorted(item.value for item in spec.aggregate_types))
        raise ProtocolViolation(
            f"reserved event {kind!r} is owned by aggregate type(s): {expected}"
        )
    if (
        type(payload_schema_version) is not int
        or payload_schema_version not in spec.payload_schema_versions
    ):
        supported = ", ".join(str(version) for version in sorted(spec.payload_schema_versions))
        raise ProtocolViolation(
            f"reserved event {kind!r} does not support payload schema version "
            f"{payload_schema_version!r}; supported: {supported}"
        )
    return spec


def _require_exact_int(name: str, value: object, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ProtocolViolation(f"{name} must be an integer >= {minimum}")
    return value


def _require_contract_authorization(
    source: str,
    context: object,
) -> None:
    if not isinstance(context, ContractAuthorization):
        raise ProtocolViolation("authorization transition requires ContractAuthorization context")
    if (
        not isinstance(context.human_principal, str)
        or not context.human_principal
        or context.human_principal != context.human_principal.strip()
        or len(context.human_principal) > 256
    ):
        raise ProtocolViolation("contract authorization requires a human principal")
    if not isinstance(context.decision_source_hash, str) or not _SHA256_RE.fullmatch(
        context.decision_source_hash
    ):
        raise ProtocolViolation("decision source hash must be canonical SHA-256")
    current_revision = _require_exact_int("current_revision", context.current_revision, minimum=0)
    proposed_revision = _require_exact_int(
        "proposed_revision", context.proposed_revision, minimum=1
    )
    attempts_created = _require_exact_int("attempts_created", context.attempts_created, minimum=0)
    attempt_ceiling = _require_exact_int("attempt_ceiling", context.attempt_ceiling, minimum=1)
    if proposed_revision != current_revision + 1:
        raise ProtocolViolation("proposed revision must be the next contract revision")
    if attempt_ceiling < attempts_created:
        raise ProtocolViolation("attempt ceiling cannot be below attempts already used")

    is_initial = source == MissionState.DRAFT
    if is_initial:
        if current_revision != 0 or attempts_created != 0:
            raise ProtocolViolation(
                "initial authorization requires revision 0 and no prior attempts"
            )
        if (
            type(context.invalidates_prior_routing) is not bool
            or type(context.invalidates_prior_evidence) is not bool
            or context.invalidates_prior_routing
            or context.invalidates_prior_evidence
        ):
            raise ProtocolViolation(
                "initial authorization cannot invalidate prior routing or evidence"
            )
    elif (
        context.invalidates_prior_routing is not True
        or context.invalidates_prior_evidence is not True
    ):
        if context.invalidates_prior_routing is not True:
            raise ProtocolViolation("contract amendment must invalidate prior routing")
        raise ProtocolViolation("contract amendment must invalidate prior evidence")


def _require_boolean(name: str, value: object) -> bool:
    if type(value) is not bool:
        raise ProtocolViolation(f"{name} must be a boolean")
    return value


def _require_distinct_epochs(previous_epoch_id: object, new_epoch_id: object) -> None:
    for name, value in (
        ("previous authority epoch", previous_epoch_id),
        ("new authority epoch", new_epoch_id),
    ):
        if not isinstance(value, str) or not value or value != value.strip() or len(value) > 256:
            raise ProtocolViolation(f"{name} ID is invalid")
    if previous_epoch_id == new_epoch_id:
        raise ProtocolViolation("retry requires a new authority epoch")


def _require_retry_shape(
    context: MissionRetry,
    *,
    same_attempt: bool,
    same_worktree: bool,
    new_attempt: bool,
    new_process: bool,
    new_session: bool,
    new_worktree: bool,
    new_branch: bool,
) -> None:
    observed = {
        "same_attempt": _require_boolean("same_attempt", context.same_attempt),
        "same_worktree": _require_boolean("same_worktree", context.same_worktree),
        "new_attempt": _require_boolean("creates_new_attempt", context.creates_new_attempt),
        "new_process": _require_boolean("creates_new_process", context.creates_new_process),
        "new_session": _require_boolean("creates_new_session", context.creates_new_session),
        "new_worktree": _require_boolean("creates_new_worktree", context.creates_new_worktree),
        "new_branch": _require_boolean("creates_new_branch", context.creates_new_branch),
    }
    expected = {
        "same_attempt": same_attempt,
        "same_worktree": same_worktree,
        "new_attempt": new_attempt,
        "new_process": new_process,
        "new_session": new_session,
        "new_worktree": new_worktree,
        "new_branch": new_branch,
    }
    if observed == expected:
        return
    if context.kind is RetryKind.SESSION:
        raise ProtocolViolation(
            "session retry must keep the same process, attempt, and worktree "
            "while creating only a new session"
        )
    if context.kind is RetryKind.PROCESS:
        raise ProtocolViolation(
            "process retry must create a new process and session in the same attempt and worktree"
        )
    if context.kind is RetryKind.ATTEMPT:
        raise ProtocolViolation(
            "attempt retry must create a new attempt, worktree, branch, process, and session"
        )
    if context.kind is RetryKind.EXPIRED_WORKER_RECOVERY:
        raise ProtocolViolation(
            "expired-worker recovery must preserve the attempt and worktree and "
            "create only a new session"
        )
    raise ProtocolViolation(
        "human-rejected rework requires a new process and session in the same attempt and worktree"
    )


def _require_mission_retry(context: object) -> None:
    if not isinstance(context, MissionRetry):
        raise ProtocolViolation("retry dispatch requires MissionRetry context")
    try:
        kind = RetryKind(context.kind)
        attempt_state = AttemptState(context.current_attempt_state)
    except ValueError as exc:
        raise ProtocolViolation("retry kind or attempt state is invalid") from exc
    attempts_created = _require_exact_int("attempts_created", context.attempts_created, minimum=1)
    attempt_ceiling = _require_exact_int("attempt_ceiling", context.attempt_ceiling, minimum=1)
    if attempts_created > attempt_ceiling:
        raise ProtocolViolation("attempts already exceed the authorized attempt ceiling")
    _require_distinct_epochs(
        context.previous_authority_epoch_id,
        context.new_authority_epoch_id,
    )

    if kind is RetryKind.SESSION:
        if attempt_state is not AttemptState.ACTIVE or context.subordinate_state != "failed":
            raise ProtocolViolation(
                "session retry requires an active attempt after its subordinate failed"
            )
        _require_retry_shape(
            context,
            same_attempt=True,
            same_worktree=True,
            new_attempt=False,
            new_process=False,
            new_session=True,
            new_worktree=False,
            new_branch=False,
        )
    elif kind is RetryKind.PROCESS:
        if attempt_state is not AttemptState.ACTIVE or context.subordinate_state != "failed":
            raise ProtocolViolation(
                "process retry requires an active attempt after its subordinate failed"
            )
        _require_retry_shape(
            context,
            same_attempt=True,
            same_worktree=True,
            new_attempt=False,
            new_process=True,
            new_session=True,
            new_worktree=False,
            new_branch=False,
        )
    elif kind is RetryKind.ATTEMPT:
        if attempt_state is not AttemptState.FAILED:
            raise ProtocolViolation("attempt retry requires the current attempt to be failed")
        if attempts_created >= attempt_ceiling:
            raise ProtocolViolation("attempt retry would exceed the attempt ceiling")
        _require_retry_shape(
            context,
            same_attempt=False,
            same_worktree=False,
            new_attempt=True,
            new_process=True,
            new_session=True,
            new_worktree=True,
            new_branch=True,
        )
    elif kind is RetryKind.EXPIRED_WORKER_RECOVERY:
        if attempt_state is not AttemptState.ORPHANED or context.subordinate_state != "orphaned":
            raise ProtocolViolation(
                "expired-worker recovery requires an orphaned attempt and subordinate"
            )
        _require_retry_shape(
            context,
            same_attempt=True,
            same_worktree=True,
            new_attempt=False,
            new_process=False,
            new_session=True,
            new_worktree=False,
            new_branch=False,
        )
    else:
        if attempt_state is not AttemptState.COMPLETED or context.subordinate_state != "completed":
            raise ProtocolViolation("human-rejected rework requires completed verified work")
        if not isinstance(context.human_rejection_source_hash, str) or not _SHA256_RE.fullmatch(
            context.human_rejection_source_hash
        ):
            raise ProtocolViolation("human-rejected rework requires a human rejection source hash")
        _require_retry_shape(
            context,
            same_attempt=True,
            same_worktree=True,
            new_attempt=False,
            new_process=True,
            new_session=True,
            new_worktree=False,
            new_branch=False,
        )


def _require_attempt_failure(context: object, *, target: str) -> None:
    if not isinstance(context, AttemptFailure):
        raise ProtocolViolation("non-convergence decision requires AttemptFailure context")
    try:
        attempt_state = AttemptState(context.current_attempt_state)
    except ValueError as exc:
        raise ProtocolViolation("attempt state is invalid") from exc
    attempts_created = _require_exact_int("attempts_created", context.attempts_created, minimum=1)
    attempt_ceiling = _require_exact_int("attempt_ceiling", context.attempt_ceiling, minimum=1)
    if attempts_created > attempt_ceiling:
        raise ProtocolViolation("attempts already exceed the authorized attempt ceiling")
    if attempt_state is not AttemptState.FAILED:
        raise ProtocolViolation("the final attempt must have actually failed")
    if target == MissionState.NON_CONVERGENT:
        if attempts_created != attempt_ceiling:
            raise ProtocolViolation(
                "mission cannot be non_convergent while the attempt ceiling has not been reached"
            )
    elif attempts_created == attempt_ceiling:
        raise ProtocolViolation(
            "a failed final attempt must become non_convergent, not merely failed"
        )


def _require_process_recovery(context: object) -> None:
    if not isinstance(context, ProcessRecovery):
        raise ProtocolViolation(
            "process reset requires ProcessRecovery context for a session retry"
        )
    try:
        kind = RetryKind(context.retry_kind)
    except ValueError as exc:
        raise ProtocolViolation("process recovery retry kind is invalid") from exc
    if kind is not RetryKind.SESSION:
        raise ProtocolViolation("a failed or orphaned process resets only for session retry")
    _require_distinct_epochs(
        context.previous_authority_epoch_id,
        context.new_authority_epoch_id,
    )
    same_process = _require_boolean("same_process", context.same_process)
    same_attempt = _require_boolean("same_attempt", context.same_attempt)
    same_worktree = _require_boolean("same_worktree", context.same_worktree)
    creates_session = _require_boolean(
        "creates_new_worker_session", context.creates_new_worker_session
    )
    if not same_process:
        raise ProtocolViolation("session retry must keep the same process")
    if not same_attempt or not same_worktree:
        raise ProtocolViolation("session retry must keep the same attempt and worktree")
    if not creates_session:
        raise ProtocolViolation("session retry must create a new worker session")


def _require_attempt_recovery(source: str, context: object) -> None:
    if not isinstance(context, AttemptRecovery):
        raise ProtocolViolation("attempt reopen requires AttemptRecovery context")
    try:
        kind = RetryKind(context.retry_kind)
    except ValueError as exc:
        raise ProtocolViolation("attempt recovery retry kind is invalid") from exc
    _require_distinct_epochs(
        context.previous_authority_epoch_id,
        context.new_authority_epoch_id,
    )
    same_attempt = _require_boolean("same_attempt", context.same_attempt)
    same_worktree = _require_boolean("same_worktree", context.same_worktree)
    _require_boolean("creates_new_process", context.creates_new_process)
    creates_session = _require_boolean(
        "creates_new_worker_session", context.creates_new_worker_session
    )
    if not same_attempt:
        raise ProtocolViolation("recovery must reopen the same attempt")
    if not same_worktree:
        raise ProtocolViolation("recovery must preserve the same worktree")
    if not creates_session:
        raise ProtocolViolation("recovery must create a new worker session")

    if source == AttemptState.ORPHANED:
        if kind is not RetryKind.EXPIRED_WORKER_RECOVERY:
            raise ProtocolViolation("an orphaned attempt reopens only for expired-worker recovery")
        if context.human_rejection_source_hash is not None:
            raise ProtocolViolation("expired-worker recovery cannot claim a human rejection")
    else:
        if kind is not RetryKind.HUMAN_REJECTED_REWORK:
            raise ProtocolViolation("a completed attempt reopens only for human-rejected rework")
        if not context.creates_new_process:
            raise ProtocolViolation(
                "human-rejected rework must create a new process, never a session retry"
            )
        if not isinstance(context.human_rejection_source_hash, str) or not _SHA256_RE.fullmatch(
            context.human_rejection_source_hash
        ):
            raise ProtocolViolation("human-rejected rework requires a human rejection source hash")


def require_transition(
    aggregate_type: AggregateType | str,
    current_state: str,
    target_state: str,
    *,
    context: object | None = None,
) -> None:
    """Reject any edge not present in the protocol-owned graph."""

    aggregate = _aggregate(aggregate_type)
    graph = LEGAL_TRANSITIONS[aggregate]
    if current_state not in graph:
        raise ProtocolViolation(f"unknown {aggregate.value} state: {current_state!r}")
    if target_state not in graph:
        raise ProtocolViolation(f"unknown {aggregate.value} state: {target_state!r}")
    if target_state not in graph[current_state]:
        raise ProtocolViolation(
            f"illegal {aggregate.value} transition: {current_state!r} -> {target_state!r}"
        )
    if (aggregate, current_state, target_state) in _CONTEXTUAL_EDGES and context is None:
        raise ProtocolViolation(
            f"{aggregate.value} transition {current_state!r} -> {target_state!r} "
            "requires protocol context"
        )
    edge = (aggregate, current_state, target_state)
    if target_state == MissionState.AUTHORIZED and aggregate is AggregateType.MISSION:
        _require_contract_authorization(current_state, context)
    elif edge == (
        AggregateType.MISSION,
        MissionState.FAILED,
        MissionState.DISPATCHED,
    ):
        _require_mission_retry(context)
    elif (aggregate is AggregateType.MISSION and target_state == MissionState.NON_CONVERGENT) or (
        aggregate is AggregateType.MISSION
        and target_state == MissionState.FAILED
        and context is not None
    ):
        _require_attempt_failure(context, target=target_state)
    elif aggregate is AggregateType.PROCESS and target_state == ProcessState.CREATED:
        _require_process_recovery(context)
    elif aggregate is AggregateType.ATTEMPT and target_state == AttemptState.ACTIVE:
        _require_attempt_recovery(current_state, context)
    elif edge in _CONTEXTUAL_EDGES:
        raise ProtocolViolation(
            f"{aggregate.value} transition {current_state!r} -> {target_state!r} "
            "requires its typed protocol context"
        )
    elif context is not None:
        raise ProtocolViolation(
            f"{aggregate.value} transition {current_state!r} -> {target_state!r} "
            "does not accept protocol context"
        )


def _require_closed_record(
    record: object,
    *,
    label: str,
    required_fields: frozenset[str],
) -> Mapping[str, object]:
    if not isinstance(record, Mapping):
        raise ProtocolViolation(f"{label} must be an object")
    field_names = set(record)
    if not all(isinstance(field, str) for field in field_names):
        raise ProtocolViolation(f"{label} field names must be strings")
    missing = required_fields - field_names
    if missing:
        raise ProtocolViolation(f"{label} is missing fields: {', '.join(sorted(missing))}")
    unknown = field_names - required_fields
    if unknown:
        raise ProtocolViolation(f"{label} has unknown fields: {', '.join(sorted(unknown))}")
    return record


def _require_text_field(
    record: Mapping[str, object],
    field: str,
    *,
    maximum: int = 512,
) -> str:
    value = record[field]
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise ProtocolViolation(f"{field} must be nonempty bounded text")
    return value


def _require_digest_field(record: Mapping[str, object], field: str) -> str:
    value = record[field]
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ProtocolViolation(f"{field} must be canonical SHA-256")
    return value


def _require_timestamp_field(
    record: Mapping[str, object],
    field: str,
) -> datetime:
    value = record[field]
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProtocolViolation(f"{field} must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolViolation(f"{field} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProtocolViolation(f"{field} must include a timezone")
    return parsed


def validate_evidence_record(record: object) -> None:
    """Validate the immutable evidence-record schema used by M1+ fixtures."""

    evidence = _require_closed_record(
        record,
        label="evidence record",
        required_fields=EVIDENCE_RECORD_REQUIRED_FIELDS,
    )
    if (
        type(evidence["record_schema_version"]) is not int
        or evidence["record_schema_version"] != EVIDENCE_RECORD_SCHEMA_VERSION
    ):
        raise ProtocolViolation(
            f"evidence record schema version must be {EVIDENCE_RECORD_SCHEMA_VERSION}"
        )
    _require_text_field(evidence, "mission_id")
    _require_exact_int("mission_revision", evidence["mission_revision"], minimum=1)
    _require_text_field(evidence, "contract_revision_id")
    _require_digest_field(evidence, "contract_hash")
    _require_text_field(evidence, "attempt_id")
    _require_text_field(evidence, "process_id")
    _require_text_field(evidence, "worker_session_id")

    git_head = evidence["git_head"]
    if not isinstance(git_head, str) or not _GIT_COMMIT_RE.fullmatch(git_head):
        raise ProtocolViolation("git_head must be a canonical Git object ID")
    for field in (
        "index_digest",
        "worktree_manifest_digest",
        "verifier_spec_digest",
        "command_hash",
    ):
        _require_digest_field(evidence, field)

    started_at = _require_timestamp_field(evidence, "started_at")
    ended_at = _require_timestamp_field(evidence, "ended_at")
    if ended_at < started_at:
        raise ProtocolViolation("evidence ended_at precedes started_at")
    if type(evidence["exit_status"]) is not int:
        raise ProtocolViolation("exit_status must be an integer")

    output_artifacts = evidence["output_artifacts"]
    if not isinstance(output_artifacts, list):
        raise ProtocolViolation("output_artifacts must be a JSON array of SHA-256 digests")
    for digest in output_artifacts:
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ProtocolViolation("output_artifacts must contain only canonical SHA-256 digests")
    if len(output_artifacts) != len(set(output_artifacts)):
        raise ProtocolViolation("output_artifacts cannot contain duplicate digests")


def validate_approval_decision(record: object) -> None:
    """Validate the closed payload vocabulary for ``approval.decided``."""

    decision = _require_closed_record(
        record,
        label="approval decision",
        required_fields=APPROVAL_DECISION_REQUIRED_FIELDS,
    )
    if (
        type(decision["decision_schema_version"]) is not int
        or decision["decision_schema_version"] != APPROVAL_DECISION_SCHEMA_VERSION
    ):
        raise ProtocolViolation(
            f"approval decision schema version must be {APPROVAL_DECISION_SCHEMA_VERSION}"
        )
    _require_text_field(decision, "mission_id")
    _require_exact_int(
        "expected_mission_revision",
        decision["expected_mission_revision"],
        minimum=1,
    )
    _require_digest_field(decision, "evidence_digest")
    if not isinstance(decision["decision"], str) or decision["decision"] not in APPROVAL_DECISIONS:
        raise ProtocolViolation("decision must be one of: approved, rejected")
    _require_text_field(decision, "human_principal", maximum=256)
    _require_digest_field(decision, "decision_source_hash")
